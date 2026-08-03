"""Post-conversion validation protocol. All checks must pass before the
original file may be deleted."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Tuple

from .exiftool import exiftool_available, run_exiftool
from .probe import (
    COLOR_FIELDS,
    inventory_streams,
    is_commentary_stream,
    media_duration,
    primary_video_streams,
    video_inventory,
)


def _tail(stderr: str, lines: int = 3) -> str:
    kept = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    return " | ".join(kept[-lines:]) if kept else "(no stderr)"


def validate_output(
    returncode: int,
    stderr: str,
    output_path: Path,
    is_video: bool,
) -> Tuple[bool, str]:
    """Run the validation checklist. Returns (ok, reason)."""
    # 1. Exit code check
    if returncode != 0:
        return False, f"converter exited with code {returncode}: {_tail(stderr)}"

    # 2. Output existence
    if not output_path.exists():
        return False, "output file was not created"

    # 3. Size check
    if output_path.stat().st_size <= 0:
        return False, "output file is 0 bytes"

    # 4. Integrity check (videos only): decode every video and audio stream,
    # not merely FFmpeg's default selections, and require a clean exit plus
    # empty stderr.
    if is_video:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-i", str(output_path),
                "-map", "0:v?", "-map", "0:a?", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or proc.stderr.strip():
            return False, f"integrity check failed: {_tail(proc.stderr)}"

    return True, "ok"


def _exif_date(path: Path) -> str:
    out = run_exiftool(["-s3", "-DateTimeOriginal", str(path)])
    return out.strip() if out else ""


def verify_photo_metadata(src: Path, output: Path) -> Tuple[bool, str]:
    """Guard the spec's 'absolutely critical' EXIF requirement: if the source
    carries a capture date, the WebP must carry the same one. Uses exiftool
    when installed; otherwise falls back to a structural check (source had an
    EXIF block, output WebP must contain an EXIF RIFF chunk)."""
    if exiftool_available():
        src_date = _exif_date(src)
        if src_date and _exif_date(output) != src_date:
            return False, "EXIF DateTimeOriginal not preserved in output"
        return True, "ok"
    ext = src.suffix.lower()
    try:
        head = src.open("rb").read(256 * 1024)
        if (ext in (".jpg", ".jpeg") and b"Exif\x00\x00" in head) or (
            ext == ".png" and b"eXIf" in head
        ):
            if b"EXIF" not in output.read_bytes():
                return False, "source EXIF block missing from output WebP"
    except OSError as exc:
        return False, f"metadata check failed to read files: {exc}"
    return True, "ok"


def verify_video_duration(src: Path, output: Path) -> Tuple[bool, str]:
    """A structurally valid MP4 can still be truncated. If both durations are
    readable they must agree within 1s (or 2% for long videos)."""
    src_dur = media_duration(src)
    out_dur = media_duration(output)
    if src_dur is not None and out_dur is not None:
        if abs(src_dur - out_dur) > max(1.0, src_dur * 0.02):
            return False, f"duration mismatch: source {src_dur:.1f}s vs output {out_dur:.1f}s"
    return True, "ok"


def _normalised_rotation(value):
    if value is None:
        return None
    return float(value) % 360


def _normalised_color(field: str, value):
    if field != "color_range":
        return value
    # H.264's unspecified range decodes as limited/TV range. Encoders commonly
    # omit that default even when the source declared it explicitly.
    if value in (None, "unknown", "tv", "mpeg", "limited"):
        return "limited"
    if value in ("pc", "jpeg", "full"):
        return "full"
    return value


def _stream_identity(stream: dict) -> dict:
    """Metadata whose loss can change the meaning or selection of a track."""
    tags = stream.get("tags", {})
    title = tags.get("title") or tags.get("name")
    if not title and is_commentary_stream(stream):
        title = "Commentary"
    language = tags.get("language")
    if language in (None, "", "und"):
        language = None
    meaningful_dispositions = (
        "dub",
        "original",
        "forced",
        "hearing_impaired",
        "visual_impaired",
        "descriptions",
    )
    return {
        "language": language,
        "title": title,
        "commentary": is_commentary_stream(stream),
        "disposition": {
            name: stream.get("disposition", {}).get(name, 0)
            for name in meaningful_dispositions
        },
    }


def verify_video_streams(
    src: Path,
    output: Path,
    source_inventory: "dict | None" = None,
    allow_stream_removal: bool = False,
) -> Tuple[bool, str]:
    """Verify duration plus all stream/metadata promises made by conversion."""
    ok, reason = verify_video_duration(src, output)
    if not ok:
        return ok, reason

    source = source_inventory if source_inventory is not None else video_inventory(src)
    target = video_inventory(output)
    if source is None or target is None:
        return False, "could not inventory source and output streams"

    source_video = primary_video_streams(source)
    target_video = primary_video_streams(target)
    expected_source_videos = 1 if allow_stream_removal and source_video else len(source_video)
    if expected_source_videos != 1 or len(target_video) != 1:
        return False, (
            f"primary video stream count changed: source {len(source_video)}, "
            f"output {len(target_video)}"
        )

    source_audio = inventory_streams(source, "audio")
    target_audio = inventory_streams(target, "audio")
    if len(source_audio) != len(target_audio):
        return False, (
            f"audio track count changed: source {len(source_audio)}, "
            f"output {len(target_audio)}"
        )
    source_defaults = [
        index for index, stream in enumerate(source_audio)
        if stream.get("disposition", {}).get("default")
    ]
    target_defaults = [
        index for index, stream in enumerate(target_audio)
        if stream.get("disposition", {}).get("default")
    ]
    # MP4 muxers assign the first audio track as default when the source has
    # no default at all. That adds a deterministic selection without changing
    # track content; an explicit source default must remain on the same track.
    allowed_target_defaults = source_defaults if source_defaults else ([] if not source_audio else [0])
    if target_defaults not in (source_defaults, allowed_target_defaults):
        return False, (
            f"default audio selection changed: source {source_defaults}, "
            f"output {target_defaults}"
        )
    for index, (before, after) in enumerate(zip(source_audio, target_audio), 1):
        if _stream_identity(before) != _stream_identity(after):
            return False, f"audio track {index} language/title/disposition metadata changed"
        before_duration = before.get("duration")
        after_duration = after.get("duration")
        if before_duration is not None:
            try:
                before_seconds = float(before_duration)
                after_seconds = float(after_duration)
            except (TypeError, ValueError):
                return False, f"audio track {index} duration could not be verified"
            if abs(before_seconds - after_seconds) > max(0.25, before_seconds * 0.02):
                return False, (
                    f"audio track {index} duration changed: source {before_seconds:.2f}s, "
                    f"output {after_seconds:.2f}s"
                )

    source_chapters = source.get("chapters", [])
    target_chapters = target.get("chapters", [])
    if len(source_chapters) != len(target_chapters):
        return False, (
            f"chapter count changed: source {len(source_chapters)}, "
            f"output {len(target_chapters)}"
        )
    for index, (before, after) in enumerate(zip(source_chapters, target_chapters), 1):
        if before.get("title") != after.get("title"):
            return False, f"chapter {index} title changed"
        try:
            start_delta = abs(float(before["start_time"]) - float(after["start_time"]))
            end_delta = abs(float(before["end_time"]) - float(after["end_time"]))
        except (KeyError, TypeError, ValueError):
            return False, f"chapter {index} timing could not be verified"
        if start_delta > 0.05 or end_delta > 0.05:
            return False, f"chapter {index} timing changed"

    before_video = source_video[0]
    after_video = target_video[0]
    if _normalised_rotation(before_video.get("rotation")) != _normalised_rotation(
        after_video.get("rotation")
    ):
        return False, "video rotation metadata changed"
    for field in COLOR_FIELDS:
        before = before_video.get("color", {}).get(field)
        after = after_video.get("color", {}).get(field)
        if before is not None and _normalised_color(field, before) != _normalised_color(
            field, after
        ):
            return False, f"video {field} metadata changed: source {before}, output {after}"

    return True, "ok"
