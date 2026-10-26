"""Post-conversion validation protocol. All checks must pass before the
original file may be deleted.

The checks here are the format-agnostic ones — the converter exited cleanly,
the output exists and is not empty, a video decodes without complaint and
AVFoundation will actually open it — plus the photo capture-date comparison.
The per-stream video comparison that justifies deleting a video original
lives in `videoverify.py`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Tuple

from .exiftool import exiftool_available, run_exiftool
from .probe import check_video_integrity


log = logging.getLogger("mediate")

def _tail(stderr: str, lines: int = 6) -> str:
    kept = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    return " | ".join(kept[-lines:]) if kept else "(no stderr)"


def validate_output(
    returncode: int,
    stderr: str,
    output_path: Path,
    is_video: bool,
    progress_path: "Path | None" = None,
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

    # 4. Integrity check (videos only): decode every video and audio stream,
    # not merely FFmpeg's default selections, and require a clean exit plus
    # empty stderr.
    if is_video:
        health = check_video_integrity(output_path, progress_path=progress_path)
        if not health["ok"]:
            return False, f"integrity check failed: {health['reason']}"
        apple_ok, apple_reason = verify_apple_playback(output_path)
        if not apple_ok:
            return False, f"Apple playback check failed: {apple_reason}"

    return True, "ok"


_AVFOUNDATION_CHECK = r'''import AVFoundation
import Foundation
let asset = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[1]))
Task {
    do {
        let playable = try await asset.load(.isPlayable)
        let protected = try await asset.load(.hasProtectedContent)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        if playable && !protected && !tracks.isEmpty { exit(0) }
        FileHandle.standardError.write(
            Data("playable=\(playable), protected=\(protected), videoTracks=\(tracks.count)\n".utf8)
        )
        exit(1)
    } catch {
        FileHandle.standardError.write(Data("\(error)\n".utf8))
        exit(2)
    }
}
dispatchMain()
'''
_apple_check_lock = threading.Lock()


def verify_apple_playback(path: Path) -> Tuple[bool, str]:
    """Require macOS AVFoundation—the stack behind Quick Look—to open output.

    FFmpeg accepting its own MP4 is necessary but not sufficient. On macOS,
    Homebrew already depends on Apple's command-line tools, so use Swift to
    ask AVFoundation whether the finished asset is actually playable.
    """
    if sys.platform != "darwin":
        return True, "not running on macOS"
    swift = shutil.which("swift")
    if not swift:
        return _verify_quicklook_playback(path)
    cache = Path(tempfile.gettempdir()) / f"mediate-swift-cache-{os.getuid()}"
    try:
        with _apple_check_lock:
            proc = subprocess.run(
                [
                    swift,
                    "-module-cache-path", str(cache),
                    "-e", _AVFOUNDATION_CHECK,
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
    except subprocess.TimeoutExpired:
        return False, "AVFoundation timed out while opening the converted MP4"
    except OSError as exc:
        return False, f"AVFoundation checker could not start: {exc}"
    if proc.returncode == 0:
        return True, "ok"
    details = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
    return False, details[-1] if details else "AVFoundation reported the MP4 is not playable"


def _verify_quicklook_playback(path: Path) -> Tuple[bool, str]:
    """Fallback for Macs without the Swift command-line frontend."""
    quicklook = shutil.which("qlmanage")
    if not quicklook:
        return False, "neither AVFoundation nor Quick Look validation is available"
    try:
        with tempfile.TemporaryDirectory(prefix="mediate-quicklook-") as temp:
            proc = subprocess.run(
                [quicklook, "-t", "-s", "64", "-o", temp, str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            made_preview = any(Path(temp).iterdir())
    except subprocess.TimeoutExpired:
        return False, "Quick Look timed out while opening the converted MP4"
    except OSError as exc:
        return False, f"Quick Look checker could not start: {exc}"
    if proc.returncode == 0 and made_preview:
        return True, "ok"
    return False, "Quick Look could not render the converted MP4"


_CAPTURE_DATE_TAGS = (
    "-EXIF:DateTimeOriginal",
    "-XMP:DateTimeOriginal",
    "-XMP-photoshop:DateCreated",
    "-IPTC:DateCreated",
    "-XMP-xmp:CreateDate",
)


def _normalise_capture_date(value) -> str:
    """Compare equivalent EXIF/XMP dates without namespace formatting noise."""
    text = str(value or "").strip()
    match = re.match(
        r"^(\d{4})[:-](\d{2})[:-](\d{2})"
        r"(?:[ T](\d{2}):(\d{2}):(\d{2}))?",
        text,
    )
    if not match:
        return text
    date = "-".join(match.groups()[:3])
    time = match.groups()[3:]
    return date + (" " + ":".join(time) if all(time) else "")


def _photo_capture_dates(path: Path) -> set[str]:
    """Return the highest-priority capture-date tier present in a photo.

    Group-qualified tags avoid ExifTool's Composite DateTimeOriginal, which
    may be synthesized from IPTC and then appear to vanish even when cwebp
    preserved the equivalent XMP DateCreated value.
    """
    out = run_exiftool(["-j", "-G1", "-a", "-s", *_CAPTURE_DATE_TAGS, str(path)])
    try:
        rows = json.loads(out or "[]")
        metadata = rows[0] if rows else {}
    except (json.JSONDecodeError, TypeError, IndexError):
        return set()

    def values(keys) -> set[str]:
        result = set()
        for key, value in metadata.items():
            if not keys(key):
                continue
            items = value if isinstance(value, list) else [value]
            result.update(
                normalised
                for item in items
                if (normalised := _normalise_capture_date(item))
            )
        return result

    # DateTimeOriginal is the strongest capture-time signal. DateCreated is
    # next; generic XMP CreateDate is only a fallback because editors also use
    # it for document creation timestamps.
    tiers = (
        lambda key: key == "ExifIFD:DateTimeOriginal"
        or (key.startswith("XMP-") and key.endswith(":DateTimeOriginal")),
        lambda key: key == "IPTC:DateCreated"
        or (key.startswith("XMP-") and key.endswith(":DateCreated")),
        lambda key: key.startswith("XMP-") and key.endswith(":CreateDate"),
    )
    for tier in tiers:
        found = values(tier)
        if found:
            return found
    return set()


def verify_photo_metadata(src: Path, output: Path) -> Tuple[bool, str]:
    """Require an equivalent capture date to survive in EXIF, XMP, or IPTC."""
    if exiftool_available():
        source_dates = _photo_capture_dates(src)
        if source_dates and source_dates.isdisjoint(_photo_capture_dates(output)):
            return False, "capture date metadata not preserved in output"
        return True, "ok"
    ext = src.suffix.lower()
    try:
        head = src.open("rb").read(256 * 1024)
        if (ext in (".jpg", ".jpeg") and b"Exif\x00\x00" in head) or (
            ext == ".png" and b"eXIf" in head
        ):
            out_head = output.open("rb").read(256 * 1024)
            if b"EXIF" not in out_head and b"Exif" not in out_head:
                return False, "source EXIF block missing from output WebP"
    except OSError as exc:
        return False, f"metadata check failed to read files: {exc}"
    return True, "ok"




def _webp_chunks(data: bytes) -> "list[bytes]":
    """The RIFF chunk ids in a WebP file, in order.

    A still WebP is either a bare `VP8 `/`VP8L` chunk or a `VP8X` extended
    container followed by sibling chunks (`ALPH`, `EXIF`, `ICCP`, and the
    image itself). Walking the sizes is enough to list them; nothing here
    needs to descend into animation frames, because the photo pipeline only
    ever produces stills — an animated WebP source is converted to MP4.
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        return []
    chunks = []
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        chunks.append(chunk_id)
        offset += 8 + size + (size & 1)
    return chunks


def verify_webp_is_lossless(output: Path) -> Tuple[bool, str]:
    """Require a WebP that promised to be lossless to actually be VP8L.

    This exists because the promise was once broken silently. cwebp's
    `-preset` resets every parameter it follows, so `-lossless ... -preset
    photo` produced an ordinary lossy VP8 file; the capture-date check still
    passed, and the original was disposed of. Nothing in the pipeline
    compared the argument list against the file that came out.

    The check reads the container's chunk ids, so it costs one small read
    and cannot itself be fooled by an encoder's exit status.
    """
    try:
        with output.open("rb") as handle:
            chunks = _webp_chunks(handle.read(64 * 1024))
    except OSError as exc:
        return False, f"could not read the converted WebP: {exc}"
    if b"VP8L" in chunks:
        return True, "ok"
    if b"VP8 " in chunks:
        return False, (
            "conversion promised lossless WebP but produced a lossy VP8 "
            "stream; the original has been kept"
        )
    return False, "converted file is not a recognizable WebP still"
