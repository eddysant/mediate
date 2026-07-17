"""Directory traversal and media-file classification."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

from .exiftool import exiftool_available, run_exiftool

log = logging.getLogger("mediate")

# Photos convertible by cwebp.
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# HEIC/HEIF need a macOS `sips` decode step first and are opt-in.
HEIC_EXTS = {".heic", ".heif"}

GIF_EXTS = {".gif"}

# Video containers that always get re-encoded to standard MP4.
VIDEO_EXTS = {
    ".mov", ".mkv", ".avi", ".wmv", ".flv", ".m4v",
    ".mpg", ".mpeg", ".webm", ".3gp", ".mts", ".m2ts",
}

# MP4s are probed first: correctly encoded ones (h264/yuv420p/aac) are skipped.
MP4_EXTS = {".mp4"}

# macOS package directories that look like folders but are application data.
# Descending into these (especially an Apple Photos library) and converting
# or deleting their internal files corrupts them, so they are never traversed.
BUNDLE_EXTS = {
    ".photoslibrary", ".aplibrary", ".migratedphotolibrary", ".photolibrary",
    ".app", ".fcpbundle", ".imovielibrary", ".tvlibrary", ".theater",
}


@dataclass(frozen=True)
class MediaJob:
    path: Path
    kind: str  # "photo" | "heic" | "gif" | "video" | "mp4"


def iter_media(root: Path) -> Iterator[MediaJob]:
    """Yield media files under root, skipping hidden files/dirs, macOS bundle
    packages, and already-standardized formats (.webp is never yielded)."""
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for d in sorted(dirnames):
            if d.startswith("."):
                continue
            if Path(d).suffix.lower() in BUNDLE_EXTS:
                log.warning("[skip] %s: application bundle, not traversed", Path(dirpath) / d)
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            ext = Path(name).suffix.lower()
            path = Path(dirpath) / name
            if ext in PHOTO_EXTS:
                yield MediaJob(path, "photo")
            elif ext in HEIC_EXTS:
                yield MediaJob(path, "heic")
            elif ext in GIF_EXTS:
                yield MediaJob(path, "gif")
            elif ext in VIDEO_EXTS:
                yield MediaJob(path, "video")
            elif ext in MP4_EXTS:
                yield MediaJob(path, "mp4")


def find_live_photo_companions(jobs: List[MediaJob]) -> Dict[Path, Path]:
    """Map each .mov that shares directory + stem with a still image (the
    Live Photo naming convention, e.g. IMG_0001.heic + IMG_0001.mov) to its
    image half. Converting the .mov would break the pairing in Apple Photos."""
    stills: Dict[Tuple[str, str], Path] = {}
    for job in jobs:
        if job.kind in ("photo", "heic"):
            key = (str(job.path.parent), job.path.stem.lower())
            stills.setdefault(key, job.path)
    companions: Dict[Path, Path] = {}
    for job in jobs:
        if job.kind == "video" and job.path.suffix.lower() == ".mov":
            key = (str(job.path.parent), job.path.stem.lower())
            if key in stills:
                companions[job.path] = stills[key]
    return _verify_live_pairs(companions)


def _content_identifier(path: Path) -> str:
    out = run_exiftool(["-s3", "-ContentIdentifier", str(path)])
    return out.strip() if out else ""


def _verify_live_pairs(companions: Dict[Path, Path]) -> Dict[Path, Path]:
    """When exiftool is available, drop stem-pairs whose ContentIdentifiers
    both exist but differ — same name, provably not a Live Photo. Pairs stay
    protected when in doubt (missing exiftool or missing identifiers)."""
    if not companions or not exiftool_available():
        return companions
    verified: Dict[Path, Path] = {}
    for mov, still in companions.items():
        cid_still = _content_identifier(still)
        cid_mov = _content_identifier(mov)
        if cid_still and cid_mov and cid_still != cid_mov:
            log.debug("%s and %s share a name but not a ContentIdentifier: not a Live Photo", still.name, mov.name)
            continue
        verified[mov] = still
    return verified
