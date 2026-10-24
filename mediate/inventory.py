"""The normalized picture of what is inside a video file.

Converting a container means choosing which of its streams to carry over,
and FFmpeg's default "best stream" selection quietly drops the rest — an
alternate language track, a commentary, a chapter carrier. So every stream,
stream group and chapter is inventoried first, in a stable JSON-serialisable
shape, and the same inventory is compared again after the encode.

Everything here is either a cached ffprobe read (`video_inventory`,
`heic_image_streams`) or a pure query over an inventory dict, which is what
makes the conversion-safety policy testable without media. The codec-level
"does this need converting at all" classification stays in `probe.py`.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import List, Optional

from .probecache import cached as _cached, ffprobe_json as _ffprobe_json


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


def heic_image_streams(path: Path) -> List[dict]:
    """Still image streams inside a HEIC/HEIF container.

    Cached: a HEIC-heavy library would otherwise re-probe every file on each
    run, and the answer only changes when the file does.
    """
    return _cached("heic-streams1", path, lambda: _heic_image_streams_uncached(path))


def _heic_image_streams_uncached(path: Path) -> List[dict]:
    data = _ffprobe_json([
        "-show_entries", "stream=index,codec_name,width,height", str(path)
    ])
    if data is None:
        return []
    return [
        {
            "index": stream.get("index"),
            "codec_name": stream.get("codec_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
        }
        for stream in data.get("streams", [])
        if stream.get("width") and stream.get("height")
    ]


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


# Container plumbing, not content. A DVD program stream (VOB) carries a
# navigation packet stream describing seek points, angles, and menu buttons;
# broadcast captures (.ts/.m2ts) carry programme-guide and ad-splice
# signalling. All three are meaningful only inside their original container,
# are regenerated rather than transported, and have no MP4 representation —
# so there is nothing for a conversion to lose. Counting them as removable
# content made every ripped DVD unconvertible without --allow-stream-removal,
# a flag that would then have permitted discarding real subtitles too.
CONTAINER_SIGNALLING_CODECS = frozenset({
    "dvd_nav_packet",  # DVD navigation packets (VOB)
    "epg",             # Electronic Programme Guide (MPEG-TS)
    "scte_35",         # SCTE-35 splice messages (MPEG-TS)
})


def _is_container_signalling(stream: dict) -> bool:
    return (
        stream.get("codec_type") == "data"
        and stream.get("codec_name") in CONTAINER_SIGNALLING_CODECS
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
        and not _is_container_signalling(stream)
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
