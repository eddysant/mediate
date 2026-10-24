"""Deciding whether a file needs converting, and whether it is intact.

Codec/container classification (`mp4_status`, `video_stream_status`),
durations, and decode-health checks live here. Every answer goes through
`probecache`, so a re-run over a large already-standardized library does not
re-spawn ffprobe — or, for `--validate-existing`, re-decode — per unchanged
file. The per-stream inventory those decisions sometimes need is built in
`inventory.py`.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from .probecache import (
    cached as _cached,
    ffprobe_json as _ffprobe_json,
    file_identity,
    recall,
    remember,
)
from .progress import run_ffmpeg_progress

log = logging.getLogger("mediate")

# mp4_status() results. yuvj420p is FFmpeg's deprecated name for full-range
# 8-bit yuv420p; both have the same widely supported 4:2:0 sample layout.
STANDARD_H264_PIXEL_FORMATS = frozenset({"yuv420p", "yuvj420p"})
APPLE_HEVC_PIXEL_FORMATS = frozenset({"yuv420p", "yuv420p10le"})

MP4_STANDARD = "standard"          # h264 8-bit 4:2:0 video, aac audio
MP4_HEVC = "hevc"                  # hevc video, aac audio — Apple-native
MP4_NEEDS_CONVERSION = "convert"

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
    source_identity = file_identity(source, strong=True)
    output_identity = file_identity(output, strong=True)
    if source_identity is None or output_identity is None:
        return
    remember(f"complete:{source.resolve()}", {
        "source": source_identity,
        "output": str(output.resolve()),
        "output_identity": output_identity,
    })


def completed_conversion_output(source: Path) -> Path | None:
    """Return an intact validated output for an unchanged retained source."""
    record = recall(f"complete:{source.resolve()}")
    if not record:
        return None
    if file_identity(source, strong=True) != record.get("source"):
        return None
    output = Path(str(record.get("output", "")))
    if file_identity(output, strong=True) != record.get("output_identity"):
        return None
    return output


# Stream metadata that must survive a video conversion. Values omitted by
# ffprobe are intentionally distinct from explicit "unknown" values.

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


def _decoded_duration_uncached(path: Path) -> "float | None":
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-map", "0:v:0", "-f", "null", "-",
    ]
    try:
        proc = run_ffmpeg_progress(cmd, path, media_duration(path), "measuring")
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        name, separator, value = line.strip().partition("=")
        if separator and name == "out_time_us":
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                return None
            return seconds if seconds > 0 else None
    return None


def decoded_duration(path: Path) -> "float | None":
    """Measure a file's real length by decoding it, or None if unreadable.

    A container's declared duration can be badly wrong. Concatenated MPEG
    program streams (how a ripped DVD's VTS_01_N.VOB parts are usually
    joined) restart their timestamps at each join, so ffprobe reports only
    the final segment — 3.9s for a 10s file — and packet spans are fooled the
    same way. Decoding is the only reliable measure, so this is cached and
    used as a fallback, never on the normal path.
    """
    return _cached(
        "decoded-duration1", path, lambda: _decoded_duration_uncached(path), strong=True
    )


def video_health(path: Path) -> dict:
    """Cached full-decode health for an existing standardized video."""
    return _cached("health2", path, lambda: check_video_integrity(path), strong=True)


def mark_video_healthy(path: Path) -> None:
    """Seed the cache after a new output already passed full validation."""
    identity = file_identity(path, strong=True)
    if identity is None:
        return
    remember(f"health2:{path}", {**identity, "v": {"ok": True, "reason": "ok"}})


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
