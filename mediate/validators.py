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
        layout_ok, layout_reason = verify_mp4_layout(output_path)
        if not layout_ok:
            return False, layout_reason
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
        with src.open("rb") as handle:
            head = handle.read(256 * 1024)
        if (ext in (".jpg", ".jpeg") and b"Exif\x00\x00" in head) or (
            ext == ".png" and b"eXIf" in head
        ):
            with output.open("rb") as handle:
                out_head = handle.read(256 * 1024)
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


# ISOBMFF top-level boxes, read by seeking rather than reading: `mdat` is the
# whole media payload, so walking the headers must never pull it into memory.
_BOX_HEADER = 8
_MAX_BOXES = 64


def _mp4_layout(output: Path) -> "tuple[list[str], str]":
    """The top-level box types of an MP4, in order, plus its ftyp brands."""
    boxes: list[str] = []
    brands = ""
    with output.open("rb") as handle:
        offset = 0
        while len(boxes) < _MAX_BOXES:
            handle.seek(offset)
            header = handle.read(_BOX_HEADER)
            if len(header) < _BOX_HEADER:
                break
            size = int.from_bytes(header[0:4], "big")
            kind = header[4:8].decode("latin1")
            boxes.append(kind)
            if kind == "ftyp":
                # major brand plus the compatible-brands list that follows it.
                brands = handle.read(max(0, size - _BOX_HEADER)).decode(
                    "latin1", "replace"
                )
            if size == 0:  # "to end of file"
                break
            if size == 1:  # 64-bit extended size
                handle.seek(offset + _BOX_HEADER)
                size = int.from_bytes(handle.read(8), "big")
            if size < _BOX_HEADER:
                break
            offset += size
    return boxes, brands


def verify_mp4_layout(output: Path) -> Tuple[bool, str]:
    """Require the two container promises every MP4 command makes.

    `-movflags +faststart` and `-brand mp42` appear in every mp4-producing
    command — encode, remux and the rotation finalizer — and until now
    nothing confirmed either reached the file. Both are cheap to check and
    both matter: without faststart the `moov` index sits after the media, so
    streaming the file over HTTP has to fetch the whole thing before playing,
    and the brand is what tells a player the file follows the MP4 profile
    mediate claims to produce.

    This reads box headers by seeking, so it costs the same on a 4 GB file as
    on a 40 KB one. A missing-moov file is already caught earlier by the
    stream preflight; this check still fails closed on one rather than
    assuming.
    """
    try:
        boxes, brands = _mp4_layout(output)
    except OSError as exc:
        return False, f"could not read the converted MP4's structure: {exc}"
    if "moov" not in boxes:
        return False, "converted MP4 has no moov index"
    if "mdat" in boxes and boxes.index("mdat") < boxes.index("moov"):
        return False, (
            "converted MP4 is not faststart: its moov index follows the "
            "media data"
        )
    if "mp42" not in brands:
        return False, (
            f"converted MP4 does not declare the mp42 brand (got {brands.strip()!r})"
        )
    return True, "ok"


_ICC_SNIFF_MARKERS = {
    ".jpg": b"ICC_PROFILE\0",
    ".jpeg": b"ICC_PROFILE\0",
    ".png": b"iCCP",
    ".tif": b"ICC_PROFILE\0",
    ".tiff": b"ICC_PROFILE\0",
}


def _has_icc_profile(path: Path) -> "bool | None":
    """Whether a file carries an embedded ICC profile, or None if unknown.

    ExifTool answers this for every format mediate handles. Without it, a
    byte sniff covers the common sources: JPEG stores the profile in an APP2
    segment introduced by "ICC_PROFILE\\0", PNG in an `iCCP` chunk, and a
    WebP output in an `ICCP` RIFF chunk. Anything else returns None, which
    the caller treats as "cannot tell" rather than "absent" — guessing
    absent would fail every conversion of a format the sniff does not know.
    """
    if path.suffix.lower() == ".webp":
        try:
            with path.open("rb") as handle:
                return b"ICCP" in _webp_chunks(handle.read(64 * 1024))
        except OSError:
            return None
    if exiftool_available():
        out = run_exiftool(["-s", "-s", "-s", "-ICC_Profile:ProfileDescription", str(path)])
        if out is not None:
            return bool(str(out).strip())
    marker = _ICC_SNIFF_MARKERS.get(path.suffix.lower())
    if marker is None:
        return None
    try:
        with path.open("rb") as handle:
            return marker in handle.read(256 * 1024)
    except OSError:
        return None


def verify_photo_color_profile(src: Path, output: Path) -> Tuple[bool, str]:
    """Require an embedded colour profile to survive the conversion.

    A wide-gamut photo — every Display P3 iPhone still — renders with
    visibly wrong colour once its ICC profile is gone, and nothing about the
    file looks broken afterwards. That is the same shape of silent loss as
    the lossless bug, so it gets the same treatment: the promise is checked
    against the file instead of assumed from the argument list.

    Only a *lost* profile fails. A source with no profile is untagged sRGB
    by convention and an output with none is equally correct, and a format
    neither ExifTool nor the byte sniff can read returns "cannot tell",
    which passes rather than blocking a conversion on ignorance.
    """
    source_has = _has_icc_profile(src)
    if not source_has:  # False or None: nothing to preserve, or cannot tell
        return True, "ok"
    output_has = _has_icc_profile(output)
    if output_has is None:
        return True, "ok"
    if output_has:
        return True, "ok"
    return False, (
        "source colour profile (ICC) was not preserved in the output; "
        "wide-gamut photos would render with shifted colour"
    )


# A lossless AVIF must carry RGB samples through an identity matrix: the
# colour transform itself is lossy otherwise, whatever the quantizer does.
# avifenc writes identity (0) for --lossless and a real matrix (1/5/6/9) for
# every lossy encode, which is the discriminator this check reads.
_IDENTITY_MATRIX = 0


def verify_avif_is_lossless(output: Path) -> Tuple[bool, str]:
    """Require a lossless-promising AVIF to at least be structurally capable
    of being one.

    This is weaker than its WebP counterpart and deliberately so: WebP says
    `VP8L` or it does not, whereas "lossless AVIF" is a conjunction of
    conditions spread across the AV1 sequence header. What is cheap to read
    is the `colr`/`nclx` box, and it carries the one condition no lossless
    encode can do without — an identity matrix, so RGB survives the
    colour transform — plus full range. Measured on real avifenc output:
    `--lossless` wrote matrix 0, the same source at `-q 60` wrote matrix 6.

    So this proves a *necessary* condition, not a sufficient one. It catches
    an encoder that quietly stopped honouring `--lossless` — the failure
    that actually happened on the WebP side — and it does not catch a
    hypothetical identity-matrix encode with a lossy quantizer.

    A missing `colr` box fails closed. Being unable to confirm the promise
    that justifies deleting the original is not a reason to delete it, and
    the message names the box so a future format change is diagnosable
    rather than silent.
    """
    try:
        with output.open("rb") as handle:
            header = handle.read(256 * 1024)
    except OSError as exc:
        return False, f"could not read the converted AVIF: {exc}"
    # The colour box lives in the metadata region ahead of the media payload,
    # so stopping at `mdat` keeps a byte scan from matching inside pixels.
    media = header.find(b"mdat")
    searchable = header[:media] if media > 0 else header
    marker = searchable.find(b"colrnclx")
    if marker < 0:
        return False, (
            "converted AVIF declares no colr/nclx colour box, so its "
            "lossless encoding cannot be confirmed; the original has been kept"
        )
    fields = searchable[marker + 8:marker + 15]
    if len(fields) < 7:
        return False, "converted AVIF has a truncated colr box"
    matrix = int.from_bytes(fields[4:6], "big")
    full_range = bool(fields[6] & 0x80)
    if matrix != _IDENTITY_MATRIX:
        return False, (
            f"conversion promised lossless AVIF but wrote matrix "
            f"coefficients {matrix} rather than identity, which is a lossy "
            "colour transform; the original has been kept"
        )
    if not full_range:
        return False, (
            "conversion promised lossless AVIF but wrote limited range, "
            "which clips sample values; the original has been kept"
        )
    return True, "ok"
