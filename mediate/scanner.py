"""Directory traversal and media-file classification."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .exiftool import exiftool_available, run_exiftool
from .probe import webp_animation_info

log = logging.getLogger("mediate")

# Photos convertible by cwebp.
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# HEIC/HEIF need a macOS `sips` decode step first and are opt-in.
HEIC_EXTS = {".heic", ".heif"}

GIF_EXTS = {".gif"}
WEBP_EXTS = {".webp"}

# Video containers that are remuxed when already compatible, otherwise
# re-encoded to standard MP4. Keep this to formats ffprobe can identify from
# the file itself; raw frame dumps such as .yuv need external parameters.
VIDEO_EXTS = {
    # Current/common non-MP4 containers.
    ".mov", ".mkv", ".avi", ".wmv", ".flv", ".m4v", ".webm",
    ".mpg", ".mpeg", ".3gp", ".mts", ".m2ts", ".ts", ".mxf",
    # Legacy QuickTime, Windows Media, RealMedia, Flash, Ogg, and DVD/camcorder.
    ".qt", ".asf", ".dvr-ms", ".wtv", ".rm", ".rmvb", ".f4v",
    ".ogv", ".ogm", ".vob", ".vro", ".mod", ".tod", ".dv", ".3g2",
    # MPEG elementary/program streams and common codec-branded AVI variants.
    ".m1v", ".m2v", ".m2p", ".mpv", ".divx", ".xvid",
}

# MP4s are probed first: compatible h264/8-bit 4:2:0/aac files are skipped.
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
    kind: str  # "photo" | "heic" | "gif" | "webp" | "video" | "mp4"
    output: Optional[Path] = None


def iter_media(root: Path) -> Iterator[MediaJob]:
    """Yield media files under root, skipping hidden files/dirs, macOS bundle
    packages, and already-standardized formats. Static WebPs are skipped;
    animated WebPs are yielded for FFmpeg 9 conversion."""
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
            if path.is_symlink():
                log.warning("[skip] %s: symbolic-link media is outside the safe conversion model", path)
                continue
            if ext in PHOTO_EXTS:
                yield MediaJob(path, "photo")
            elif ext in HEIC_EXTS:
                yield MediaJob(path, "heic")
            elif ext in GIF_EXTS:
                yield MediaJob(path, "gif")
            elif ext in WEBP_EXTS and webp_animation_info(path)["animated"]:
                yield MediaJob(path, "webp")
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
    """Require matching Apple identifiers when ExifTool is available.

    A shared filename is only a candidate, not proof of a Live Photo. Without
    ExifTool we retain the conservative naming fallback because converting a
    real pair would irreversibly break its Apple Photos relationship.
    """
    if not companions or not exiftool_available():
        return companions
    verified: Dict[Path, Path] = {}
    for mov, still in companions.items():
        cid_still = _content_identifier(still)
        cid_mov = _content_identifier(mov)
        if not cid_still or not cid_mov or cid_still != cid_mov:
            log.debug(
                "%s and %s share a name but lack a matching ContentIdentifier: "
                "not a verified Live Photo",
                still.name,
                mov.name,
            )
            continue
        verified[mov] = still
    return verified
