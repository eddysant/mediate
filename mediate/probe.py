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
    return _cached("mp4", path, lambda: _mp4_status_uncached(path))


def _mp4_status_uncached(path: Path) -> str:
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
