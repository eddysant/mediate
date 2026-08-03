"""ffprobe helpers for deciding whether a file needs conversion.

Probe results are cached by filesystem identity; expensive decode-health
entries also include a sampled content fingerprint. Re-running over a large
already-standardized library therefore does not re-spawn ffprobe/ffmpeg for
every unchanged file."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from .progress import run_ffmpeg_progress

log = logging.getLogger("mediate")

# mp4_status() results. yuvj420p is FFmpeg's deprecated name for full-range
# 8-bit yuv420p; both have the same widely supported 4:2:0 sample layout.
STANDARD_H264_PIXEL_FORMATS = frozenset({"yuv420p", "yuvj420p"})

MP4_STANDARD = "standard"          # h264 8-bit 4:2:0 video, aac audio
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


def _sample_fingerprint(path: Path) -> str | None:
    """Hash small samples from across a file without rereading large media."""
    try:
        size = path.stat().st_size
        digest = hashlib.blake2b(digest_size=16)
        with path.open("rb") as handle:
            for offset in sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)}):
                handle.seek(offset)
                digest.update(offset.to_bytes(8, "little"))
                digest.update(handle.read(65536))
        return digest.hexdigest()
    except OSError:
        return None


def _file_identity(path: Path, strong: bool = False) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    identity = {
        "m": st.st_mtime_ns,
        "c": st.st_ctime_ns,
        "s": st.st_size,
        "i": st.st_ino,
        "d": st.st_dev,
    }
    if strong:
        identity["f"] = _sample_fingerprint(path)
    return identity


def _cached(kind: str, path: Path, compute, strong: bool = False):
    identity = _file_identity(path, strong=strong)
    if identity is None:
        return compute()
    key = f"{kind}:{path}"
    with _cache_lock:
        entry = _cache.get(key)
        if entry and all(entry.get(name) == value for name, value in identity.items()):
            return entry["v"]
    value = compute()
    global _cache_dirty
    with _cache_lock:
        _cache[key] = {**identity, "v": value}
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
    """Classify an .mp4: already standard (h264 8-bit 4:2:0 + AAC), HEVC (more
    space-efficient than h264 and Apple-native, so re-encoding it to h264
    only makes it bigger), or needing conversion. An unreadable file reports
    needs-conversion; the attempt that follows is protected by the
    validation protocol."""
    # Versioned key invalidates classifications made before full-range
    # yuvj420p was recognised as 8-bit 4:2:0 compatible.
    return _cached("mp4v2", path, lambda: _mp4_status_uncached(path))


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
    if all(
        s.get("codec_name") == "h264"
        and s.get("pix_fmt") in STANDARD_H264_PIXEL_FORMATS
        for s in video
    ):
        return MP4_STANDARD
    if all(s.get("codec_name") == "hevc" for s in video):
        return MP4_HEVC
    return MP4_NEEDS_CONVERSION


# video_stream_status() results — usable for any container, not just .mp4
STREAM_STANDARD = "standard"       # h264 8-bit 4:2:0, aac (or no) audio
STREAM_HEVC = "hevc"               # hevc video, aac (or no) audio
STREAM_NEEDS_CONVERSION = "convert"


def video_stream_status(path: Path) -> str:
    """Classify *any* video file's streams (not just .mp4). Returns
    STREAM_STANDARD when streams are already h264 8-bit 4:2:0 + AAC and can
    be remuxed into MP4 with ``-c copy``, STREAM_HEVC for HEVC + AAC, or
    STREAM_NEEDS_CONVERSION when re-encoding is required."""
    return _cached("vstreamv2", path, lambda: _video_stream_status_uncached(path))


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
    if all(
        s.get("codec_name") == "h264"
        and s.get("pix_fmt") in STANDARD_H264_PIXEL_FORMATS
        for s in video
    ):
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
SIMPLE_SUBTITLE_CODECS = {"mov_text", "subrip", "srt", "text", "webvtt"}
MP4_ARTWORK_CODECS = {"mjpeg", "png"}
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
        "profile": stream.get("profile"),
        "start_time": stream.get("start_time"),
        "duration": stream.get("duration"),
        "tags": _selected(tags, IDENTITY_TAGS),
        "disposition": _selected(disposition, DISPOSITION_FIELDS),
    }
    if stream.get("codec_type") == "video":
        result["width"] = stream.get("width")
        result["height"] = stream.get("height")
        result["pix_fmt"] = stream.get("pix_fmt")
        result["bits_per_raw_sample"] = stream.get("bits_per_raw_sample")
        result["field_order"] = stream.get("field_order")
        result["avg_frame_rate"] = stream.get("avg_frame_rate")
        result["r_frame_rate"] = stream.get("r_frame_rate")
        result["sample_aspect_ratio"] = stream.get("sample_aspect_ratio")
        result["rotation"] = _rotation(stream)
        result["color"] = _selected(stream, COLOR_FIELDS)
        result["attached_pic"] = bool(disposition.get("attached_pic"))
        result["side_data_types"] = sorted({
            str(item.get("side_data_type"))
            for item in stream.get("side_data_list", [])
            if item.get("side_data_type")
        })
    elif stream.get("codec_type") == "audio":
        result["sample_rate"] = stream.get("sample_rate")
        result["channels"] = stream.get("channels")
        result["channel_layout"] = stream.get("channel_layout")
        result["sample_fmt"] = stream.get("sample_fmt")
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


def preservable_subtitle_streams(inventory: dict) -> List[dict]:
    """Text subtitle streams MP4 can carry safely as mov_text."""
    return [
        stream for stream in inventory_streams(inventory, "subtitle")
        if stream.get("codec_name") in SIMPLE_SUBTITLE_CODECS
    ]


def preservable_artwork_streams(inventory: dict) -> List[dict]:
    """Already-decoded cover streams MP4 can retain without extraction."""
    return [
        stream for stream in attached_artwork_streams(inventory)
        if stream.get("codec_type") == "video"
        and stream.get("codec_name") in MP4_ARTWORK_CODECS
    ]


def _pixel_depth(stream: dict) -> int | None:
    value = stream.get("bits_per_raw_sample")
    try:
        depth = int(value)
        if depth:
            return depth
    except (TypeError, ValueError):
        pass
    pix_fmt = str(stream.get("pix_fmt") or "")
    for depth in (16, 14, 12, 10, 9):
        if str(depth) in pix_fmt:
            return depth
    return 8 if pix_fmt else None


def _rate_float(value) -> float | None:
    try:
        numerator, denominator = str(value).split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else None
    except (TypeError, ValueError):
        return None


def advanced_video_features(inventory: dict) -> List[str]:
    """Describe picture properties that an ordinary 8-bit encode may damage."""
    videos = primary_video_streams(inventory)
    if not videos:
        return []
    stream = videos[0]
    features: List[str] = []
    depth = _pixel_depth(stream)
    if depth and depth > 8:
        features.append(f"{depth}-bit video")
    transfer = str(stream.get("color", {}).get("color_transfer") or "").lower()
    side_data = " ".join(stream.get("side_data_types", [])).lower()
    if transfer in {"smpte2084", "arib-std-b67"} or any(
        marker in side_data
        for marker in ("mastering display", "content light", "dolby vision", "dovi", "hdr10")
    ):
        features.append("HDR/dynamic-range metadata")
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    if pix_fmt.startswith(("rgba", "argb", "bgra", "abgr", "yuva", "gbrap")):
        features.append("alpha channel")
    field_order = str(stream.get("field_order") or "").lower()
    if field_order not in ("", "unknown", "progressive"):
        features.append(f"interlaced video ({field_order})")
    average_rate = _rate_float(stream.get("avg_frame_rate"))
    nominal_rate = _rate_float(stream.get("r_frame_rate"))
    if average_rate and nominal_rate and abs(average_rate - nominal_rate) > max(0.01, nominal_rate * 0.01):
        features.append("variable frame rate")
    elif average_rate and (average_rate < 10 or average_rate > 120):
        features.append(f"unusual frame rate ({average_rate:.3f} fps)")
    return features


def is_commentary_stream(stream: dict) -> bool:
    if stream.get("disposition", {}).get("comment"):
        return True
    tags = stream.get("tags", {})
    text = " ".join(
        str(tags.get(name, "")) for name in ("title", "name", "handler_name")
    )
    return "commentary" in text.lower()


def audio_stream_label(stream: dict) -> Optional[str]:
    """Return a meaningful audio label across Matroska and MOV/MP4 dialects.

    Older MP4 muxers expose a user track title only as ``handler_name``. They
    also synthesize generic handler names for untitled tracks; those are
    container boilerplate and must not be mistaken for user metadata.
    """
    tags = stream.get("tags", {})
    title = tags.get("title") or tags.get("name")
    if title:
        return str(title)
    handler = str(tags.get("handler_name") or "").strip()
    generic = {
        "soundhandler",
        "audiohandler",
        "audiomediahandler",
        "soundmediahandler",
        "coremediaaudio",
    }
    compact = "".join(character for character in handler.lower() if character.isalnum())
    return None if not handler or compact in generic else handler


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
    preservable_subtitles = preservable_subtitle_streams(inventory)
    subtitles = [
        stream for stream in inventory_streams(inventory, "subtitle")
        if stream not in preservable_subtitles
    ]
    if subtitles:
        codecs = ", ".join(sorted({s.get("codec_name", "unknown") for s in subtitles}))
        risks.append(f"{len(subtitles)} subtitle track(s) ({codecs})")
    preservable_artwork = preservable_artwork_streams(inventory)
    artwork = [
        stream for stream in attached_artwork_streams(inventory)
        if stream not in preservable_artwork
    ]
    if artwork:
        risks.append(f"{len(artwork)} attached artwork stream(s)")
    others = [
        stream for stream in inventory.get("streams", [])
        if stream.get("codec_type") not in ("video", "audio", "subtitle")
        and not _is_chapter_carrier(stream, inventory)
        and stream not in artwork
        and stream not in preservable_artwork
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
    subtitles = preservable_subtitle_streams(inventory)
    if subtitles:
        parts.append(f"{len(subtitles)} compatible subtitle track(s)")
    artwork = preservable_artwork_streams(inventory)
    if artwork:
        parts.append(f"{len(artwork)} compatible artwork stream(s)")
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
    return _cached("health2", path, lambda: check_video_integrity(path), strong=True)


def mark_video_healthy(path: Path) -> None:
    """Seed the cache after a new output already passed full validation."""
    key = f"health2:{path}"
    identity = _file_identity(path, strong=True)
    if identity is None:
        return
    global _cache_dirty
    with _cache_lock:
        _cache[key] = {**identity, "v": {"ok": True, "reason": "ok"}}
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
