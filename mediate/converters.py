"""Subprocess-based conversion with the validation/deletion protocol."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .disposal import Disposer
from .macmeta import get_birthtime, set_birthtime
from .probe import MP4_HEVC, MP4_STANDARD, gif_is_animated, mp4_status
from .scanner import MediaJob
from .validators import validate_output

log = logging.getLogger("mediate")

# Outcome statuses
CONVERTED = "converted"
SKIPPED = "skipped"
FAILED = "failed"
PLANNED = "planned"  # dry-run


@dataclass
class Options:
    dry_run: bool = False
    keep_originals: bool = False
    only_if_smaller: bool = False
    reencode_hevc: bool = False
    convert_heic: bool = False
    dispose: Optional[Disposer] = None
    dispose_label: str = "delete original"


@dataclass
class Outcome:
    status: str
    path: Path
    detail: str = ""
    bytes_saved: int = 0


def _build_command(kind: str, input_path: Path, output_path: Path) -> List[str]:
    if kind == "photo":
        return [
            "cwebp", "-lossless", "-metadata", "all", "-preset", "photo",
            str(input_path), "-o", str(output_path),
        ]
    if kind == "video":
        return [
            "ffmpeg", "-nostdin", "-y", "-i", str(input_path),
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-c:a", "aac", "-b:a", "256k",
            "-pix_fmt", "yuv420p",
            "-map_metadata", "0",
            str(output_path),
        ]
    if kind == "gif":
        return [
            "ffmpeg", "-nostdin", "-y", "-i", str(input_path),
            "-movflags", "faststart",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(output_path),
        ]
    raise ValueError(f"unknown job kind: {kind}")


def intended_output(job: MediaJob) -> Path:
    """The final path a job will produce, used to detect two inputs that
    map to the same output name before any conversion starts."""
    return job.path.with_suffix(".webp" if job.kind in ("photo", "heic") else ".mp4")


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def _convert(kind: str, src: Path, tmp: Path) -> subprocess.CompletedProcess:
    """Run the conversion subprocess(es) for a job. HEIC goes through a
    two-step pipeline: sips (built into macOS, decodes HEVC-compressed
    stills that cwebp cannot read) to a temporary PNG, then the normal
    lossless cwebp encode. PNG specifically: sips copies the EXIF block
    into it and cwebp extracts EXIF from PNG — with a TIFF intermediate
    cwebp drops the metadata ("EXIF extraction from TIFF is unsupported")."""
    if kind != "heic":
        return _run(_build_command(kind, src, tmp))

    png = tmp.with_suffix(".png")
    try:
        sips = _run(["sips", "-s", "format", "png", str(src), "--out", str(png)])
        if sips.returncode != 0:
            return sips
        return _run(_build_command("photo", png, tmp))
    finally:
        png.unlink(missing_ok=True)


def process_job(job: MediaJob, opts: Options) -> Outcome:
    src = job.path
    kind = job.kind

    if kind == "mp4":
        status = mp4_status(src)
        if status == MP4_STANDARD:
            return Outcome(SKIPPED, src, "already standardized MP4 (h264/yuv420p/aac)")
        if status == MP4_HEVC and not opts.reencode_hevc:
            return Outcome(
                SKIPPED, src,
                "HEVC MP4 (smaller than h264 and Apple-native; --reencode-hevc to convert anyway)",
            )
        kind = "video"

    if kind == "gif" and not gif_is_animated(src):
        return Outcome(SKIPPED, src, "static GIF (only animated GIFs are converted)")

    if kind == "heic":
        if not opts.convert_heic:
            return Outcome(SKIPPED, src, "HEIC (already space-efficient; --convert-heic to convert)")
        if sys.platform != "darwin":
            return Outcome(SKIPPED, src, "HEIC conversion requires macOS (sips)")

    new_ext = ".webp" if kind in ("photo", "heic") else ".mp4"
    final = src.with_suffix(new_ext)

    # Re-encoding a non-standard .mp4 targets its own name; that only works
    # if the original is removed first, so pick a new name when keeping it.
    if final == src and opts.keep_originals:
        final = src.with_name(src.stem + ".standardized.mp4")

    if final != src and final.exists():
        return Outcome(SKIPPED, src, f"output already exists: {final.name}")

    if opts.dry_run:
        action = "convert" if opts.keep_originals else f"convert and {opts.dispose_label}"
        return Outcome(PLANNED, src, f"would {action} -> {final.name}")

    # Convert into a temp name in the same directory, then rename into place
    # only after validation, so a crash never leaves a half-written file
    # wearing the final name.
    tmp = final.with_name(f".{final.stem}.{uuid.uuid4().hex[:8]}.part{final.suffix}")
    try:
        proc = _convert(kind, src, tmp)
    except FileNotFoundError as exc:
        return Outcome(FAILED, src, f"converter not found: {exc}")

    ok, reason = validate_output(proc.returncode, proc.stderr, tmp, is_video=(new_ext == ".mp4"))
    if not ok:
        log.debug("stderr for %s:\n%s", src, proc.stderr.strip())
        tmp.unlink(missing_ok=True)
        return Outcome(FAILED, src, f"validation failed, original kept: {reason}")

    src_stat = src.stat()
    src_birthtime = get_birthtime(src)
    new_size = tmp.stat().st_size

    if opts.only_if_smaller and new_size >= src_stat.st_size:
        tmp.unlink(missing_ok=True)
        return Outcome(
            SKIPPED, src,
            f"output not smaller ({_fmt_size(src_stat.st_size)} -> {_fmt_size(new_size)}), original kept",
        )

    disposed = ""
    if not opts.keep_originals and opts.dispose is not None:
        try:
            disposed = f", {opts.dispose(src)}"
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            return Outcome(FAILED, src, f"could not dispose of original ({exc}); conversion discarded")
    os.replace(tmp, final)
    # Preserve the original's timestamps so date-based sorting still works —
    # mtime for everything, plus Finder's creation date on macOS.
    os.utime(final, (src_stat.st_atime, src_stat.st_mtime))
    if src_birthtime is not None:
        set_birthtime(final, src_birthtime)

    return Outcome(
        CONVERTED,
        src,
        f"-> {final.name} ({_fmt_size(src_stat.st_size)} -> {_fmt_size(new_size)}){disposed}",
        bytes_saved=src_stat.st_size - new_size,
    )
