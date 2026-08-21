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
APPLE_HEVC_PIXEL_FORMATS = frozenset({"yuv420p", "yuv420p10le"})

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
    return _cached("mp4v4", path, lambda: _mp4_classification_uncached(path))["status"]


def mp4_conversion_reason(path: Path) -> str:
    """Explain why an existing MP4 is not already Apple-compatible."""
    return _cached("mp4v4", path, lambda: _mp4_classification_uncached(path))["reason"]


def _mp4_status_uncached(path: Path) -> str:
    return _mp4_classification_uncached(path)["status"]


def _mp4_classification_uncached(path: Path) -> dict:
    data = _ffprobe_json(
        [
            "-show_entries",
            "stream=codec_type,codec_name,codec_tag_string,pix_fmt:"
            "stream_disposition=attached_pic",
            str(path),
        ]
    )
    if data is None:
        return {
            "status": MP4_NEEDS_CONVERSION,
            "reason": "FFmpeg could not read its stream inventory",
        }
    streams = data.get("streams", [])
    video = [
        s for s in streams
        if s.get("codec_type") == "video"
        and not s.get("disposition", {}).get("attached_pic")
    ]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        return {"status": MP4_NEEDS_CONVERSION, "reason": "it has no playable video track"}
    if any(s.get("codec_name") != "aac" for s in audio):
        codecs = ", ".join(sorted({str(s.get("codec_name") or "unknown") for s in audio}))
        return {
            "status": MP4_NEEDS_CONVERSION,
            "reason": f"audio is {codecs}, not AAC",
        }
    if all(
        s.get("codec_name") == "h264"
        and s.get("pix_fmt") in STANDARD_H264_PIXEL_FORMATS
        and s.get("codec_tag_string") in (None, "avc1")
        for s in video
    ) and all(s.get("codec_tag_string") in (None, "mp4a") for s in audio):
        return {"status": MP4_STANDARD, "reason": "already Apple-compatible"}
    if all(
        s.get("codec_name") == "hevc"
        and s.get("pix_fmt") in APPLE_HEVC_PIXEL_FORMATS
        and s.get("codec_tag_string") in (None, "hvc1")
        for s in video
    ):
        return {"status": MP4_HEVC, "reason": "HEVC is already Apple-native"}
    descriptions = []
    for stream in video:
        descriptions.append(
            "/".join(str(stream.get(name) or "unknown") for name in (
                "codec_name", "pix_fmt", "codec_tag_string"
            ))
        )
    if all(
        stream.get("codec_name") == "h264"
        and stream.get("pix_fmt") in STANDARD_H264_PIXEL_FORMATS
        for stream in video
    ):
        reason = "H.264/AAC tracks need an Apple-compatible MP4 remux"
    elif all(
        stream.get("codec_name") == "hevc"
        and stream.get("pix_fmt") in APPLE_HEVC_PIXEL_FORMATS
        for stream in video
    ):
        reason = "HEVC/AAC tracks need an Apple-compatible hvc1 MP4 remux"
    else:
        reason = "video is " + ", ".join(descriptions) + ", not H.264 8-bit 4:2:0"
    return {"status": MP4_NEEDS_CONVERSION, "reason": reason}


# video_stream_status() results — usable for any container, not just .mp4
STREAM_STANDARD = "standard"       # h264 8-bit 4:2:0, aac (or no) audio
STREAM_COPY_VIDEO = "copy-video"   # compatible h264/hevc video; transcode audio only
STREAM_HEVC = "hevc"               # hevc video, aac (or no) audio
STREAM_NEEDS_CONVERSION = "convert"


def video_stream_status(path: Path) -> str:
    """Classify *any* video file's streams (not just .mp4). Returns
    STREAM_STANDARD when streams are already h264 8-bit 4:2:0 + AAC and can
    be remuxed into MP4 with ``-c copy``, STREAM_HEVC for HEVC + AAC, or
    STREAM_NEEDS_CONVERSION when re-encoding is required."""
    # v3 distinguishes audio-only conversion from a full video re-encode.
    return _cached("vstreamv4", path, lambda: _video_stream_status_uncached(path))


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
    compatible_h264 = all(
        s.get("codec_name") == "h264"
        and s.get("pix_fmt") in STANDARD_H264_PIXEL_FORMATS
        for s in video
    )
    compatible_hevc = all(
        s.get("codec_name") == "hevc"
        and s.get("pix_fmt") in APPLE_HEVC_PIXEL_FORMATS
        for s in video
    )
    if (compatible_h264 or compatible_hevc) and any(
        s.get("codec_name") != "aac" for s in audio
    ):
        return STREAM_COPY_VIDEO
    if any(s.get("codec_name") != "aac" for s in audio):
        return STREAM_NEEDS_CONVERSION
    if compatible_h264:
        return STREAM_STANDARD
    if compatible_hevc:
        return STREAM_HEVC
    return STREAM_NEEDS_CONVERSION


def mark_conversion_complete(source: Path, output: Path) -> None:
    """Remember a validated keep-original conversion across later runs."""
    source_identity = _file_identity(source, strong=True)
    output_identity = _file_identity(output, strong=True)
    if source_identity is None or output_identity is None:
        return
    global _cache_dirty
    with _cache_lock:
        _cache[f"complete:{source.resolve()}"] = {
            "source": source_identity,
            "output": str(output.resolve()),
            "output_identity": output_identity,
        }
        _cache_dirty = True


def completed_conversion_output(source: Path) -> Path | None:
    """Return an intact validated output for an unchanged retained source."""
    with _cache_lock:
        record = _cache.get(f"complete:{source.resolve()}")
    if not record:
        return None
    source_identity = _file_identity(source, strong=True)
    if source_identity != record.get("source"):
        return None
    output = Path(str(record.get("output", "")))
    if _file_identity(output, strong=True) != record.get("output_identity"):
        return None
    return output


# Stream metadata that must survive a video conversion. Values omitted by
# ffprobe are intentionally distinct from explicit "unknown" values.
COLOR_FIELDS = (
    "color_range",
    "color_space",
    "color_transfer",
    "color_primaries",
    "chroma_location",
)
DYNAMIC_RANGE_SIDE_DATA_MARKERS = (
    "mastering display",
    "content light",
    "dolby vision",
    "dovi",
    "hdr10",
    "smpte 2094-50",
    "hdr vivid",
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
    # v3 adds stream groups. These carry relationships such as an LCEVC
    # enhancement layer which cannot be reconstructed from the flat stream
    # list and must not silently disappear during explicit stream mapping.
    return _cached("vinv3", path, lambda: _video_inventory_uncached(path))


_stream_group_support: Optional[bool] = None
_stream_group_support_lock = threading.Lock()


def _ffprobe_supports_stream_groups() -> bool:
    """Capability-check ``-show_stream_groups`` once for FFmpeg 6 support."""
    global _stream_group_support
    with _stream_group_support_lock:
        if _stream_group_support is not None:
            return _stream_group_support
        try:
            proc = subprocess.run(
                ["ffprobe", "-hide_banner", "-h", "full"],
                capture_output=True,
                text=True,
            )
            output = proc.stdout + proc.stderr
            _stream_group_support = (
                proc.returncode == 0 and "-show_stream_groups" in output
            )
        except OSError:
            _stream_group_support = False
        return _stream_group_support


def _normalise_stream_group(group: dict) -> dict:
    return {
        "index": group.get("index"),
        "id": group.get("id"),
        "type": group.get("type") or group.get("group_type") or "unknown",
    }


def _video_inventory_uncached(path: Path) -> Optional[dict]:
    args = ["-show_streams", "-show_chapters"]
    if _ffprobe_supports_stream_groups():
        args.append("-show_stream_groups")
    data = _ffprobe_json([*args, str(path)])
    if data is None:
        return None
    streams = [_normalise_stream(stream) for stream in data.get("streams", [])]
    return {
        "streams": streams,
        "chapters": [_normalise_chapter(chapter) for chapter in data.get("chapters", [])],
        "stream_groups": [
            _normalise_stream_group(group) for group in data.get("stream_groups", [])
        ],
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
        marker in side_data for marker in DYNAMIC_RANGE_SIDE_DATA_MARKERS
    ):
        features.append("HDR/dynamic-range metadata")
    group_types = " ".join(
        str(group.get("type", "")) for group in inventory.get("stream_groups", [])
    ).lower()
    if "lcevc" in side_data or "lcevc" in group_types:
        features.append("LCEVC enhancement layer")
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
    original_audio = [
        stream for stream in inventory_streams(inventory, "audio")
        if stream.get("disposition", {}).get("original")
    ]
    if original_audio:
        risks.append(
            f"{len(original_audio)} audio track original-language disposition(s)"
        )
    groups = inventory.get("stream_groups", [])
    if groups:
        kinds = ", ".join(sorted({
            str(group.get("type") or "unknown") for group in groups
        }))
        risks.append(f"{len(groups)} unsupported stream group(s) ({kinds})")
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


def decoded_stream_starts(path: Path) -> dict[str, float]:
    """Return the first decoded timestamp for each stream.

    Container ``start_time`` values can precede the first decodable audio
    frame (notably in ASF/WMA), so they are not sufficient for A/V sync
    validation after transcoding to a codec with different priming.
    """
    return _cached("decoded-starts", path, lambda: _decoded_stream_starts(path))


def packet_stream_durations(path: Path) -> dict[str, float]:
    """Return the readable packet span for every stream, without decoding.

    Matroska/WebM duration headers can outlive the media when a download is
    cut short.  Scanning packet timestamps tells validation how much media is
    actually recoverable while using constant memory even for long videos.
    """
    return _cached(
        "packet-durations1", path, lambda: _packet_stream_durations(path), strong=True
    )


def _packet_stream_durations(path: Path) -> dict[str, float]:
    command = [
        "ffprobe", "-v", "error", "-show_packets",
        "-show_entries", "packet=stream_index,pts_time,dts_time,duration_time",
        "-of", "compact=p=0:nk=0", str(path),
    ]
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return {}
    bounds: dict[str, list[float]] = {}
    assert proc.stdout is not None
    for line in proc.stdout:
        values = {}
        for field in line.strip().split("|"):
            name, separator, value = field.partition("=")
            if separator:
                values[name] = value
        stream_index = values.get("stream_index")
        timestamp = values.get("pts_time") or values.get("dts_time")
        if stream_index is None or timestamp in (None, "N/A"):
            continue
        try:
            start = float(timestamp)
            duration = float(values.get("duration_time", "0") or 0)
        except ValueError:
            continue
        end = start + max(0.0, duration)
        current = bounds.setdefault(stream_index, [start, end])
        current[0] = min(current[0], start)
        current[1] = max(current[1], end)
    proc.stdout.close()
    proc.wait()
    return {
        stream_index: max(0.0, end - start)
        for stream_index, (start, end) in bounds.items()
    }


def _decoded_stream_starts(path: Path) -> dict[str, float]:
    data = _ffprobe_json([
        "-read_intervals", "%+10",
        "-show_frames",
        "-show_entries", "frame=stream_index,best_effort_timestamp_time,pts_time",
        str(path),
    ])
    starts: dict[str, float] = {}
    for frame in (data or {}).get("frames", []):
        key = str(frame.get("stream_index"))
        if key in starts or key == "None":
            continue
        value = frame.get("best_effort_timestamp_time", frame.get("pts_time"))
        try:
            starts[key] = float(value)
        except (TypeError, ValueError):
            continue
    return starts


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
    stderr = proc.stderr.strip()
    if stderr and proc.returncode == 0:
        lines = [
            line for line in stderr.splitlines()
            if line.strip() 
            and "Referenced QT chapter track not found" not in line
            and "non monotonically increasing dts to muxer" not in line
        ]
        if not lines:
            stderr = ""
    if proc.returncode != 0 or stderr:
        return {"ok": False, "reason": _stderr_tail(stderr)}
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


def webp_animation_info(path: Path) -> dict:
    """Read the lossless VP8X feature byte without decoding the image.

    Static WebPs remain an already-standardized photo format. Animated WebPs
    are candidates for FFmpeg 9's native WebP demuxer, while the other flags
    let the conversion pipeline require explicit permission before discarding
    alpha or container metadata that MP4 cannot represent equivalently.
    """
    return _cached("webpinfo1", path, lambda: _webp_animation_info_uncached(path))


def _webp_animation_info_uncached(path: Path) -> dict:
    info = {
        "animated": False,
        "alpha": False,
        "icc": False,
        "exif": False,
        "xmp": False,
    }
    try:
        with path.open("rb") as handle:
            header = handle.read(21)
    except OSError:
        return info
    if (
        len(header) < 21
        or header[:4] != b"RIFF"
        or header[8:12] != b"WEBP"
        or header[12:16] != b"VP8X"
    ):
        return info
    flags = header[20]
    info.update({
        "animated": bool(flags & 0x02),
        "alpha": bool(flags & 0x10),
        "icc": bool(flags & 0x20),
        "exif": bool(flags & 0x08),
        "xmp": bool(flags & 0x04),
    })
    return info
