"""Post-conversion validation protocol. All checks must pass before the
original file may be deleted."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from fractions import Fraction
from typing import Tuple

from .exiftool import exiftool_available, run_exiftool
from .probe import (
    COLOR_FIELDS,
    DYNAMIC_RANGE_SIDE_DATA_MARKERS,
    STANDARD_H264_PIXEL_FORMATS,
    audio_stream_label,
    check_video_integrity,
    decoded_stream_starts,
    inventory_streams,
    is_commentary_stream,
    media_duration,
    packet_stream_durations,
    preservable_artwork_streams,
    preservable_subtitle_streams,
    primary_video_streams,
    video_inventory,
)


def _tail(stderr: str, lines: int = 6) -> str:
    kept = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    return " | ".join(kept[-lines:]) if kept else "(no stderr)"


def validate_output(
    returncode: int,
    stderr: str,
    output_path: Path,
    is_video: bool,
    progress_path: "Path | None" = None,
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
        health = check_video_integrity(output_path, progress_path=progress_path)
        if not health["ok"]:
            return False, f"integrity check failed: {health['reason']}"
        apple_ok, apple_reason = verify_apple_playback(output_path)
        if not apple_ok:
            return False, f"Apple playback check failed: {apple_reason}"

    return True, "ok"


_AVFOUNDATION_CHECK = r'''import AVFoundation
import Foundation
let asset = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[1]))
Task {
    do {
        let playable = try await asset.load(.isPlayable)
        let protected = try await asset.load(.hasProtectedContent)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        if playable && !protected && !tracks.isEmpty { exit(0) }
        FileHandle.standardError.write(
            Data("playable=\(playable), protected=\(protected), videoTracks=\(tracks.count)\n".utf8)
        )
        exit(1)
    } catch {
        FileHandle.standardError.write(Data("\(error)\n".utf8))
        exit(2)
    }
}
dispatchMain()
'''
_apple_check_lock = threading.Lock()


def verify_apple_playback(path: Path) -> Tuple[bool, str]:
    """Require macOS AVFoundation—the stack behind Quick Look—to open output.

    FFmpeg accepting its own MP4 is necessary but not sufficient. On macOS,
    Homebrew already depends on Apple's command-line tools, so use Swift to
    ask AVFoundation whether the finished asset is actually playable.
    """
    if sys.platform != "darwin":
        return True, "not running on macOS"
    swift = shutil.which("swift")
    if not swift:
        return _verify_quicklook_playback(path)
    cache = Path(tempfile.gettempdir()) / f"mediate-swift-cache-{os.getuid()}"
    try:
        with _apple_check_lock:
            proc = subprocess.run(
                [
                    swift,
                    "-module-cache-path", str(cache),
                    "-e", _AVFOUNDATION_CHECK,
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
    except subprocess.TimeoutExpired:
        return False, "AVFoundation timed out while opening the converted MP4"
    except OSError as exc:
        return False, f"AVFoundation checker could not start: {exc}"
    if proc.returncode == 0:
        return True, "ok"
    details = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
    return False, details[-1] if details else "AVFoundation reported the MP4 is not playable"


def _verify_quicklook_playback(path: Path) -> Tuple[bool, str]:
    """Fallback for Macs without the Swift command-line frontend."""
    quicklook = shutil.which("qlmanage")
    if not quicklook:
        return False, "neither AVFoundation nor Quick Look validation is available"
    try:
        with tempfile.TemporaryDirectory(prefix="mediate-quicklook-") as temp:
            proc = subprocess.run(
                [quicklook, "-t", "-s", "64", "-o", temp, str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            made_preview = any(Path(temp).iterdir())
    except subprocess.TimeoutExpired:
        return False, "Quick Look timed out while opening the converted MP4"
    except OSError as exc:
        return False, f"Quick Look checker could not start: {exc}"
    if proc.returncode == 0 and made_preview:
        return True, "ok"
    return False, "Quick Look could not render the converted MP4"


_CAPTURE_DATE_TAGS = (
    "-EXIF:DateTimeOriginal",
    "-XMP:DateTimeOriginal",
    "-XMP-photoshop:DateCreated",
    "-IPTC:DateCreated",
    "-XMP-xmp:CreateDate",
)


def _normalise_capture_date(value) -> str:
    """Compare equivalent EXIF/XMP dates without namespace formatting noise."""
    text = str(value or "").strip()
    match = re.match(
        r"^(\d{4})[:-](\d{2})[:-](\d{2})"
        r"(?:[ T](\d{2}):(\d{2}):(\d{2}))?",
        text,
    )
    if not match:
        return text
    date = "-".join(match.groups()[:3])
    time = match.groups()[3:]
    return date + (" " + ":".join(time) if all(time) else "")


def _photo_capture_dates(path: Path) -> set[str]:
    """Return the highest-priority capture-date tier present in a photo.

    Group-qualified tags avoid ExifTool's Composite DateTimeOriginal, which
    may be synthesized from IPTC and then appear to vanish even when cwebp
    preserved the equivalent XMP DateCreated value.
    """
    out = run_exiftool(["-j", "-G1", "-a", "-s", *_CAPTURE_DATE_TAGS, str(path)])
    try:
        rows = json.loads(out or "[]")
        metadata = rows[0] if rows else {}
    except (json.JSONDecodeError, TypeError, IndexError):
        return set()

    def values(keys) -> set[str]:
        result = set()
        for key, value in metadata.items():
            if not keys(key):
                continue
            items = value if isinstance(value, list) else [value]
            result.update(
                normalised
                for item in items
                if (normalised := _normalise_capture_date(item))
            )
        return result

    # DateTimeOriginal is the strongest capture-time signal. DateCreated is
    # next; generic XMP CreateDate is only a fallback because editors also use
    # it for document creation timestamps.
    tiers = (
        lambda key: key == "ExifIFD:DateTimeOriginal"
        or (key.startswith("XMP-") and key.endswith(":DateTimeOriginal")),
        lambda key: key == "IPTC:DateCreated"
        or (key.startswith("XMP-") and key.endswith(":DateCreated")),
        lambda key: key.startswith("XMP-") and key.endswith(":CreateDate"),
    )
    for tier in tiers:
        found = values(tier)
        if found:
            return found
    return set()


def verify_photo_metadata(src: Path, output: Path) -> Tuple[bool, str]:
    """Require an equivalent capture date to survive in EXIF, XMP, or IPTC."""
    if exiftool_available():
        source_dates = _photo_capture_dates(src)
        if source_dates and source_dates.isdisjoint(_photo_capture_dates(output)):
            return False, "capture date metadata not preserved in output"
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


def _stream_identity(stream: dict, ignored_dispositions=()) -> dict:
    """Metadata whose loss can change the meaning or selection of a track."""
    tags = stream.get("tags", {})
    title = audio_stream_label(stream)
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
            if name not in ignored_dispositions
        },
    }


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate(value):
    try:
        rate = Fraction(str(value))
        return float(rate) if rate.denominator else None
    except (ValueError, ZeroDivisionError):
        return None


def _normalised_layout(value):
    if value in (None, "", "unknown"):
        return None
    return str(value).lower().replace(" ", "")


def _av_offset_tolerance(video: dict) -> float:
    """Allow codec/container rounding up to roughly one low-rate frame.

    Legacy containers such as ASF quantise stream starts differently across
    FFmpeg versions. A frame-aware bound avoids false failures while still
    rejecting sync shifts large enough to be perceptible.
    """
    frame_rate = _rate(video.get("avg_frame_rate"))
    if not frame_rate or frame_rate <= 0:
        return 0.05
    return max(0.05, min(0.15, 1.5 / frame_rate))


def _subtitle_identity(stream: dict) -> dict:
    tags = stream.get("tags", {})
    title = tags.get("title") or tags.get("name") or tags.get("handler_name")
    if str(title or "").lower().replace(" ", "") in {
        "subtitlehandler", "texthandler", "subtitlemediahandler"
    }:
        title = None
    return {
        "language": None if tags.get("language") in (None, "", "und") else tags.get("language"),
        "title": title,
        "forced": bool(stream.get("disposition", {}).get("forced")),
    }


def verify_video_streams(
    src: Path,
    output: Path,
    source_inventory: "dict | None" = None,
    allow_stream_removal: bool = False,
    allow_video_downgrade: bool = False,
    allow_truncated_source: bool = False,
    preserve_video_codec: bool = False,
) -> Tuple[bool, str]:
    """Verify duration plus all stream/metadata promises made by conversion."""
    ok, reason = verify_video_duration(src, output)
    duration_note = "ok"
    if not ok and not allow_truncated_source:
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

    if not ok:
        source_packets = packet_stream_durations(src)
        target_packets = packet_stream_durations(output)
        paired_streams = [
            *zip(source_video, target_video),
            *zip(inventory_streams(source, "audio"), inventory_streams(target, "audio")),
        ]
        recovered = []
        for before, after in paired_streams:
            before_duration = source_packets.get(str(before.get("index")))
            after_duration = target_packets.get(str(after.get("index")))
            if before_duration is None or after_duration is None:
                return False, reason
            if abs(before_duration - after_duration) > max(1.0, before_duration * 0.02):
                return False, reason
            recovered.append(before_duration)
        if not recovered:
            return False, reason
        duration_note = (
            f"recovered all {max(recovered):.1f}s of readable media; "
            f"the damaged source header claimed {media_duration(src):.1f}s"
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
    decoded_starts = None
    for index, (before, after) in enumerate(zip(source_audio, target_audio), 1):
        if after.get("codec_name") != "aac":
            return False, f"audio track {index} output codec is not AAC"
        ignored_dispositions = ("original",) if allow_stream_removal else ()
        if _stream_identity(before, ignored_dispositions) != _stream_identity(
            after, ignored_dispositions
        ):
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
        for field, label in (("channels", "channel count"), ("sample_rate", "sample rate")):
            before_value = before.get(field)
            after_value = after.get(field)
            if before_value is not None and str(before_value) != str(after_value):
                return False, (
                    f"audio track {index} {label} changed: "
                    f"source {before_value} vs output {after_value}"
                )
        before_layout = _normalised_layout(before.get("channel_layout"))
        after_layout = _normalised_layout(after.get("channel_layout"))
        # FFmpeg 8's AAC-in-MP4 probe can omit the textual layout even though
        # the AAC channel configuration and verified channel count are intact.
        # A reported, different layout still fails closed.
        if (
            before_layout is not None
            and after_layout is not None
            and before_layout != after_layout
        ):
            return False, (
                f"audio track {index} channel layout changed: "
                f"source {before.get('channel_layout')} vs output {after.get('channel_layout')}"
            )
        if before.get("codec_name") == after.get("codec_name"):
            before_profile = before.get("profile")
            after_profile = after.get("profile")
            if before_profile not in (None, "unknown") and before_profile != after_profile:
                return False, (
                    f"audio track {index} codec profile changed: "
                    f"source {before_profile} vs output {after_profile}"
                )
        source_video_start = _number(source_video[0].get("start_time"))
        before_start = _number(before.get("start_time"))
        target_video_start = _number(target_video[0].get("start_time"))
        after_start = _number(after.get("start_time"))
        if None not in (source_video_start, before_start, target_video_start, after_start):
            before_offset = before_start - source_video_start
            after_offset = after_start - target_video_start
            tolerance = _av_offset_tolerance(source_video[0])
            if abs(before_offset - after_offset) > tolerance:
                if decoded_starts is None:
                    decoded_starts = (
                        decoded_stream_starts(src),
                        decoded_stream_starts(output),
                    )
                source_starts, target_starts = decoded_starts
                decoded_values = (
                    source_starts.get(str(source_video[0].get("index"))),
                    source_starts.get(str(before.get("index"))),
                    target_starts.get(str(target_video[0].get("index"))),
                    target_starts.get(str(after.get("index"))),
                )
                if None not in decoded_values:
                    before_offset = decoded_values[1] - decoded_values[0]
                    after_offset = decoded_values[3] - decoded_values[2]
                if abs(before_offset - after_offset) > tolerance:
                    return False, (
                        f"audio track {index} A/V start offset changed: source "
                        f"{before_offset:+.3f}s vs output {after_offset:+.3f}s "
                        f"(tolerance {tolerance:.3f}s)"
                    )

    source_subtitles = preservable_subtitle_streams(source)
    target_subtitles = inventory_streams(target, "subtitle")
    if len(source_subtitles) != len(target_subtitles):
        return False, (
            f"compatible subtitle track count changed: source {len(source_subtitles)}, "
            f"output {len(target_subtitles)}"
        )
    source_subtitle_defaults = [
        index for index, stream in enumerate(source_subtitles)
        if stream.get("disposition", {}).get("default")
    ]
    target_subtitle_defaults = [
        index for index, stream in enumerate(target_subtitles)
        if stream.get("disposition", {}).get("default")
    ]
    allowed_subtitle_defaults = (
        source_subtitle_defaults
        if source_subtitle_defaults
        else ([] if not source_subtitles else [0])
    )
    if target_subtitle_defaults not in (source_subtitle_defaults, allowed_subtitle_defaults):
        return False, "default subtitle selection changed"
    for index, (before, after) in enumerate(zip(source_subtitles, target_subtitles), 1):
        if after.get("codec_name") != "mov_text":
            return False, f"subtitle track {index} is not MP4 mov_text"
        if _subtitle_identity(before) != _subtitle_identity(after):
            return False, f"subtitle track {index} language/title/disposition metadata changed"

    source_artwork = preservable_artwork_streams(source)
    target_artwork = preservable_artwork_streams(target)
    if len(source_artwork) != len(target_artwork):
        return False, (
            f"compatible artwork count changed: source {len(source_artwork)}, "
            f"output {len(target_artwork)}"
        )
    for index, (before, after) in enumerate(zip(source_artwork, target_artwork), 1):
        for field in ("codec_name", "width", "height"):
            before_value = before.get(field)
            if before_value is not None and before_value != after.get(field):
                return False, f"artwork stream {index} {field} changed"

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
    if preserve_video_codec and (
        before_video.get("codec_name") != after_video.get("codec_name")
        or before_video.get("pix_fmt") != after_video.get("pix_fmt")
    ):
        return False, (
            "copied video format changed: source "
            f"{before_video.get('codec_name')}/{before_video.get('pix_fmt')} vs output "
            f"{after_video.get('codec_name')}/{after_video.get('pix_fmt')}"
        )
    if not preserve_video_codec and (
        after_video.get("codec_name") != "h264"
        or after_video.get("pix_fmt") not in STANDARD_H264_PIXEL_FORMATS
    ):
        return False, (
            "primary output is not h264/8-bit 4:2:0: "
            f"{after_video.get('codec_name')}/{after_video.get('pix_fmt')}"
        )
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

    if not allow_video_downgrade:
        for field, label in (
            ("field_order", "field order"),
            ("sample_aspect_ratio", "sample aspect ratio"),
        ):
            before = before_video.get(field)
            after = after_video.get(field)
            if before not in (None, "", "unknown", "0:1") and before != after:
                return False, f"video {label} changed: source {before} vs output {after}"
        before_rate = _rate(before_video.get("avg_frame_rate"))
        after_rate = _rate(after_video.get("avg_frame_rate"))
        if before_rate and after_rate and abs(before_rate - after_rate) > max(0.5, before_rate * 0.005):
            return False, (
                f"video average frame rate changed: source {before_rate:.3f} "
                f"vs output {after_rate:.3f}"
            )
        before_side_data = set(before_video.get("side_data_types", []))
        after_side_data = set(after_video.get("side_data_types", []))
        important = {
            value for value in before_side_data
            if any(
                marker in value.lower() for marker in DYNAMIC_RANGE_SIDE_DATA_MARKERS
            )
        }
        if not important.issubset(after_side_data):
            return False, "video HDR/dynamic-range side data changed"

    return True, duration_note
