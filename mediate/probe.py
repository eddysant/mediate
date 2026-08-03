"""ffprobe helpers for deciding whether a file needs conversion.

Probe results are cached (keyed by path + mtime + size) in the user cache
directory, so re-running over a large already-standardized library doesn't
re-spawn ffprobe for every file."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from .progress import run_ffmpeg_progress

log = logging.getLogger("mediate")

# mp4_status() results
MP4_STANDARD = "standard"          # h264/yuv420p video, aac audio
MP4_HEVC = "hevc"                  # hevc video, aac audio — Apple-native
MP4_NEEDS_CONVERSION = "convert"

_cache: dict = {}
_cache_lock = threading.Lock()
_cache_dirty = False


def _cache_file() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "mediate" / "probe-cache.json"


def load_probe_cache() -> None:
    global _cache
    try:
        _cache = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _cache = {}


def save_probe_cache() -> None:
    if not _cache_dirty:
        return
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unbounded growth guard: a fresh start is cheaper than an LRU.
        data = _cache if len(_cache) <= 200_000 else {}
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        log.debug("could not write probe cache: %s", exc)


def _cached(kind: str, path: Path, compute):
    try:
        st = path.stat()
    except OSError:
        return compute()
    key = f"{kind}:{path}"
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry.get("m") == st.st_mtime and entry.get("s") == st.st_size:
            return entry["v"]
    value = compute()
    global _cache_dirty
    with _cache_lock:
        _cache[key] = {"m": st.st_mtime, "s": st.st_size, "v": value}
        _cache_dirty = True
    return value


def _ffprobe_json(args: list) -> dict | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-of", "json", *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def mp4_status(path: Path) -> str:
    """Classify an .mp4: already standard (h264/yuv420p + AAC), HEVC (more
    space-efficient than h264 and Apple-native, so re-encoding it to h264
    only makes it bigger), or needing conversion. An unreadable file reports
    needs-conversion; the attempt that follows is protected by the
    validation protocol."""
    return _cached("mp4", path, lambda: _mp4_status_uncached(path))


def _mp4_status_uncached(path: Path) -> str:
    data = _ffprobe_json(
        [
            "-show_entries",
            "stream=codec_type,codec_name,pix_fmt:stream_disposition=attached_pic",
            str(path),
        ]
    )
    if data is None:
        return MP4_NEEDS_CONVERSION
    streams = data.get("streams", [])
    video = [
        s for s in streams
        if s.get("codec_type") == "video"
        and not s.get("disposition", {}).get("attached_pic")
    ]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        return MP4_NEEDS_CONVERSION
    if any(s.get("codec_name") != "aac" for s in audio):
        return MP4_NEEDS_CONVERSION
    if all(s.get("codec_name") == "h264" and s.get("pix_fmt") == "yuv420p" for s in video):
        return MP4_STANDARD
    if all(s.get("codec_name") == "hevc" for s in video):
        return MP4_HEVC
    return MP4_NEEDS_CONVERSION


# video_stream_status() results — usable for any container, not just .mp4
STREAM_STANDARD = "standard"       # h264/yuv420p video, aac (or no) audio
STREAM_HEVC = "hevc"               # hevc video, aac (or no) audio
STREAM_NEEDS_CONVERSION = "convert"


def video_stream_status(path: Path) -> str:
    """Classify *any* video file's streams (not just .mp4). Returns
    STREAM_STANDARD when the streams are already h264/yuv420p + AAC and can
    be remuxed into MP4 with ``-c copy``, STREAM_HEVC for HEVC + AAC, or
    STREAM_NEEDS_CONVERSION when re-encoding is required."""
    return _cached("vstream", path, lambda: _video_stream_status_uncached(path))


def _video_stream_status_uncached(path: Path) -> str:
    data = _ffprobe_json(
        [
            "-show_entries",
            "stream=codec_type,codec_name,pix_fmt:stream_disposition=attached_pic",
            str(path),
        ]
    )
    if data is None:
        return STREAM_NEEDS_CONVERSION
    streams = data.get("streams", [])
    video = [
        s for s in streams
        if s.get("codec_type") == "video"
        and not s.get("disposition", {}).get("attached_pic")
    ]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        return STREAM_NEEDS_CONVERSION
    # Audio must be AAC (or absent — some screen recordings have no audio)
    if any(s.get("codec_name") != "aac" for s in audio):
        return STREAM_NEEDS_CONVERSION
    if all(s.get("codec_name") == "h264" and s.get("pix_fmt") == "yuv420p" for s in video):
        return STREAM_STANDARD
    if all(s.get("codec_name") == "hevc" for s in video):
        return STREAM_HEVC
    return STREAM_NEEDS_CONVERSION


# Stream metadata that must survive a video conversion. Values omitted by
# ffprobe are intentionally distinct from explicit "unknown" values.
COLOR_FIELDS = (
    "color_range",
    "color_space",
    "color_transfer",
    "color_primaries",
    "chroma_location",
)
IDENTITY_TAGS = (
    "language",
    "title",
    "name",
    "handler_name",
    "filename",
    "mimetype",
)
DISPOSITION_FIELDS = (
    "default",
    "dub",
    "original",
    "comment",
    "lyrics",
    "karaoke",
    "forced",
    "hearing_impaired",
    "visual_impaired",
    "captions",
    "descriptions",
    "metadata",
    "dependent",
    "still_image",
)


def _selected(source: dict, names) -> dict:
    return {name: source[name] for name in names if name in source}


def _rotation(stream: dict) -> "float | int | None":
    """Read rotation from modern display-matrix side data or legacy tags."""
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            try:
                value = float(side_data["rotation"])
                return int(value) if value.is_integer() else value
            except (TypeError, ValueError):
                pass
    value = stream.get("tags", {}).get("rotate")
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _normalise_stream(stream: dict) -> dict:
    tags = {
        str(name).lower(): value
        for name, value in (stream.get("tags") or {}).items()
    }
    disposition = stream.get("disposition") or {}
    result = {
        "index": stream.get("index"),
        "codec_type": stream.get("codec_type", "unknown"),
        "codec_name": stream.get("codec_name", "unknown"),
        "codec_tag_string": stream.get("codec_tag_string"),
        "duration": stream.get("duration"),
        "tags": _selected(tags, IDENTITY_TAGS),
        "disposition": _selected(disposition, DISPOSITION_FIELDS),
    }
    if stream.get("codec_type") == "video":
        result["pix_fmt"] = stream.get("pix_fmt")
        result["rotation"] = _rotation(stream)
        result["color"] = _selected(stream, COLOR_FIELDS)
        result["attached_pic"] = bool(disposition.get("attached_pic"))
    return result


def _normalise_chapter(chapter: dict) -> dict:
    tags = {
        str(name).lower(): value
        for name, value in (chapter.get("tags") or {}).items()
    }
    return {
        "start_time": chapter.get("start_time"),
        "end_time": chapter.get("end_time"),
        "title": tags.get("title"),
    }


def video_inventory(path: Path) -> Optional[dict]:
    """Return a cached, JSON-serialisable inventory of every video stream.

    The inventory is used both before conversion (to prevent implicit FFmpeg
    stream selection from losing content) and after conversion (to verify the
    streams and metadata that were meant to survive).
    """
    return _cached("vinv2", path, lambda: _video_inventory_uncached(path))


def _video_inventory_uncached(path: Path) -> Optional[dict]:
    data = _ffprobe_json(["-show_streams", "-show_chapters", str(path)])
    if data is None:
        return None
    streams = [_normalise_stream(stream) for stream in data.get("streams", [])]
    return {
        "streams": streams,
        "chapters": [_normalise_chapter(chapter) for chapter in data.get("chapters", [])],
    }


def inventory_streams(inventory: dict, codec_type: str) -> List[dict]:
    return [
        stream for stream in inventory.get("streams", [])
        if stream.get("codec_type") == codec_type
    ]


def primary_video_streams(inventory: dict) -> List[dict]:
    return [
        stream for stream in inventory_streams(inventory, "video")
        if not stream.get("attached_pic")
    ]


def attached_artwork_streams(inventory: dict) -> List[dict]:
    pictures = [
        stream for stream in inventory_streams(inventory, "video")
        if stream.get("attached_pic")
    ]
    image_extensions = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")
    for stream in inventory_streams(inventory, "attachment"):
        tags = stream.get("tags", {})
        mimetype = str(tags.get("mimetype", "")).lower()
        filename = str(tags.get("filename", "")).lower()
        if mimetype.startswith("image/") or filename.endswith(image_extensions):
            pictures.append(stream)
    return pictures


def is_commentary_stream(stream: dict) -> bool:
    if stream.get("disposition", {}).get("comment"):
        return True
    tags = stream.get("tags", {})
    text = " ".join(
        str(tags.get(name, "")) for name in ("title", "name", "handler_name")
    )
    return "commentary" in text.lower()


def _is_chapter_carrier(stream: dict, inventory: dict) -> bool:
    """MOV/MP4 exposes its chapter text table as a generated data stream."""
    return bool(inventory.get("chapters")) and (
        stream.get("codec_type") == "data"
        and stream.get("codec_name") == "bin_data"
        and stream.get("codec_tag_string") == "text"
    )


def stream_removal_risks(inventory: dict) -> List[str]:
    """Describe streams the MP4 standardisation command cannot preserve.

    Audio tracks (including commentary), chapters, rotation, and colour data
    are supported. The returned categories are blocked unless the user opts
    into their removal.
    """
    risks = []
    videos = primary_video_streams(inventory)
    if len(videos) > 1:
        risks.append(f"{len(videos) - 1} additional video track(s)")
    subtitles = inventory_streams(inventory, "subtitle")
    if subtitles:
        codecs = ", ".join(sorted({s.get("codec_name", "unknown") for s in subtitles}))
        risks.append(f"{len(subtitles)} subtitle track(s) ({codecs})")
    artwork = attached_artwork_streams(inventory)
    if artwork:
        risks.append(f"{len(artwork)} attached artwork stream(s)")
    others = [
        stream for stream in inventory.get("streams", [])
        if stream.get("codec_type") not in ("video", "audio", "subtitle")
        and not _is_chapter_carrier(stream, inventory)
        and stream not in artwork
    ]
    if others:
        kinds = ", ".join(sorted({s.get("codec_type", "unknown") for s in others}))
        risks.append(f"{len(others)} unsupported {kinds} stream(s)")
    return risks


def preservation_summary(inventory: dict) -> str:
    """Human-readable preflight summary for logs and dry runs."""
    parts = []
    audio = inventory_streams(inventory, "audio")
    commentary = sum(1 for stream in audio if is_commentary_stream(stream))
    if len(audio) > 1:
        detail = f"{len(audio)} audio tracks"
        if commentary:
            detail += f" ({commentary} commentary)"
        parts.append(detail)
    elif commentary:
        parts.append("1 commentary audio track")
    chapters = inventory.get("chapters", [])
    if chapters:
        parts.append(f"{len(chapters)} chapter(s)")
    videos = primary_video_streams(inventory)
    if videos:
        video = videos[0]
        if video.get("rotation") is not None:
            parts.append(f"rotation {video['rotation']} degrees")
        if video.get("color"):
            parts.append("colour metadata")
    return ", ".join(parts)


def media_duration(path: Path) -> "float | None":
    """Container duration in seconds, or None if unreadable. Cached."""
    return _cached("dur", path, lambda: _media_duration_uncached(path))


def _media_duration_uncached(path: Path) -> "float | None":
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _stderr_tail(stderr: str, lines: int = 3) -> str:
    kept = [line for line in stderr.strip().splitlines() if line.strip()]
    return " | ".join(kept[-lines:]) if kept else "(no stderr)"


def check_video_integrity(path: Path, progress_path: Optional[Path] = None) -> dict:
    """Fully decode every video/audio stream and report strict health."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-map", "0:v?", "-map", "0:a?", "-f", "null", "-",
    ]
    try:
        proc = run_ffmpeg_progress(
            cmd, progress_path or path, media_duration(path), "validating"
        )
    except FileNotFoundError:
        return {"ok": False, "reason": "ffmpeg not found"}
    if proc.returncode != 0 or proc.stderr.strip():
        return {"ok": False, "reason": _stderr_tail(proc.stderr)}
    return {"ok": True, "reason": "ok"}


def video_health(path: Path) -> dict:
    """Cached full-decode health for an existing standardized video."""
    return _cached("health1", path, lambda: check_video_integrity(path))


def mark_video_healthy(path: Path) -> None:
    """Seed the cache after a new output already passed full validation."""
    try:
        stat = path.stat()
    except OSError:
        return
    key = f"health1:{path}"
    global _cache_dirty
    with _cache_lock:
        _cache[key] = {
            "m": stat.st_mtime,
            "s": stat.st_size,
            "v": {"ok": True, "reason": "ok"},
        }
        _cache_dirty = True


def gif_is_animated(path: Path) -> bool:
    """True if the GIF has more than one frame. Probe failures count as
    animated so the file still goes through the (validated) conversion."""
    return _cached("gif", path, lambda: _gif_is_animated_uncached(path))


def _gif_is_animated_uncached(path: Path) -> bool:
    data = _ffprobe_json(
        [
            "-select_streams", "v:0",
            "-count_packets",
            "-show_entries", "stream=nb_read_packets",
            str(path),
        ]
    )
    if data is None:
        return True
    streams = data.get("streams", [])
    if not streams:
        return True
    try:
        return int(streams[0].get("nb_read_packets", 2)) > 1
    except (TypeError, ValueError):
        return True
