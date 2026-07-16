"""ffprobe helpers for deciding whether a file needs conversion."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

# mp4_status() results
MP4_STANDARD = "standard"          # h264/yuv420p video, aac audio
MP4_HEVC = "hevc"                  # hevc video, aac audio — Apple-native
MP4_NEEDS_CONVERSION = "convert"


def _ffprobe_json(args: list) -> dict | None:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args],
        capture_output=True,
        text=True,
    )
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
    data = _ffprobe_json(
        ["-show_entries", "stream=codec_type,codec_name,pix_fmt", str(path)]
    )
    if data is None:
        return MP4_NEEDS_CONVERSION
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
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


def gif_is_animated(path: Path) -> bool:
    """True if the GIF has more than one frame. Probe failures count as
    animated so the file still goes through the (validated) conversion."""
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
