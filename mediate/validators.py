"""Post-conversion validation protocol. All checks must pass before the
original file may be deleted."""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple


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

    # 4. Integrity check (videos only): decode the whole file and require a
    # clean exit AND empty stderr.
    if is_video:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(output_path), "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or proc.stderr.strip():
            return False, f"integrity check failed: {_tail(proc.stderr)}"

    return True, "ok"


@lru_cache(maxsize=1)
def _has_exiftool() -> bool:
    return shutil.which("exiftool") is not None


def _exif_date(path: Path) -> str:
    proc = subprocess.run(
        ["exiftool", "-s3", "-DateTimeOriginal", str(path)],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def verify_photo_metadata(src: Path, output: Path) -> Tuple[bool, str]:
    """Guard the spec's 'absolutely critical' EXIF requirement: if the source
    carries a capture date, the WebP must carry the same one. Uses exiftool
    when installed; otherwise falls back to a structural check (source had an
    EXIF block, output WebP must contain an EXIF RIFF chunk)."""
    if _has_exiftool():
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


def _duration(path: Path) -> Optional[float]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def verify_video_duration(src: Path, output: Path) -> Tuple[bool, str]:
    """A structurally valid MP4 can still be truncated. If both durations are
    readable they must agree within 1s (or 2% for long videos)."""
    src_dur = _duration(src)
    out_dur = _duration(output)
    if src_dur is not None and out_dur is not None:
        if abs(src_dur - out_dur) > max(1.0, src_dur * 0.02):
            return False, f"duration mismatch: source {src_dur:.1f}s vs output {out_dur:.1f}s"
    return True, "ok"
