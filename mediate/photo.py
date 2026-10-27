"""Decoding and encoding one still image.

Photos take a different route from video: cwebp (or avifenc) encodes the
final file, but neither reads HEIC, and the decoders that do have a habit of
losing the EXIF block on the way. So the pipeline is a chain of fallbacks —
a PNG intermediate for HEIC, sips or jpegtran to rescue a source another
tool chokes on, exiftool to put back whatever a decoder dropped — each step
chosen because a simpler one silently lost metadata or picked the wrong
image out of the container.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .exiftool import exiftool_available, run_exiftool
from .ffmpegcmd import DEFAULT_PHOTO_EFFORT, _build_command
from .proc import run as _run

log = logging.getLogger("mediate")


def _jpegtran_path() -> Optional[str]:
    """Find jpegtran, including Homebrew's keg-only jpeg-turbo install.

    Homebrew deliberately does not link jpeg-turbo's command-line programs
    into its global bin directory.  Deriving the prefix from cwebp keeps the
    standalone app useful without spawning Homebrew during a conversion.
    """
    found = shutil.which("jpegtran")
    if found:
        return found
    prefixes = []
    cwebp = shutil.which("cwebp")
    if cwebp:
        prefixes.append(Path(cwebp).parent.parent)
    prefixes.extend((Path("/opt/homebrew"), Path("/usr/local")))
    seen = set()
    for prefix in prefixes:
        candidate = prefix / "opt" / "jpeg-turbo" / "bin" / "jpegtran"
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _convert_photo(
    src: Path,
    tmp: Path,
    output_format: str = "webp",
    photo_effort: str = DEFAULT_PHOTO_EFFORT,
) -> subprocess.CompletedProcess:
    """Convert a still, losslessly normalising a damaged JPEG if needed."""
    direct = _run(_build_command(
        "photo", src, tmp, output_format=output_format,
        photo_effort=photo_effort,
    ))
    # avifenc error messages differ from cwebp; only trigger repair for cwebp failures
    if output_format == "avif":
        return direct
    decode_failure = any(
        marker in direct.stderr
        for marker in (
            "Cannot read input picture file",
            "Could not process file",
            "`jpegtran -copy all` MAY be able to process this file",
        )
    )
    if (
        direct.returncode == 0
        or (src.suffix.lower() not in (".jpg", ".jpeg", ".gif") and not decode_failure)
    ):
        return direct

    if src.suffix.lower() == ".gif" and decode_failure:
        # _run raises FileNotFoundError when sips is absent, which
        # process_job reports as "converter not found: sips" — blaming a tool
        # the user never asked for and discarding the real cwebp diagnostic.
        if not shutil.which("sips"):
            direct.stderr += (
                "\nThe GIF decode fallback needs macOS sips, which is not "
                "available here; the original cwebp failure stands."
            )
            return direct
        png = tmp.with_suffix(".png")
        try:
            sips = _run(["sips", "-s", "format", "png", str(src), "--out", str(png)])
            if sips.returncode != 0:
                direct.stderr += f"\nsips GIF decode fallback failed: {sips.stderr}"
                return direct
            retry = _run(_build_command(
                "photo", png, tmp, output_format=output_format,
                photo_effort=photo_effort,
            ))
            if retry.returncode == 0:
                log.warning("       %s: decoded GIF with sips before WebP conversion", src.name)
            else:
                retry.stderr = direct.stderr + "\nsips decoded the GIF to PNG, but cwebp still rejected it:\n" + retry.stderr
            return retry
        finally:
            png.unlink(missing_ok=True)

    if src.suffix.lower() not in (".jpg", ".jpeg") or not decode_failure:
        return direct

    jpegtran = _jpegtran_path()
    if jpegtran is None:
        direct.stderr += (
            "\nAutomatic lossless JPEG repair is unavailable: install jpeg-turbo "
            "(`brew install jpeg-turbo`) and retry."
        )
        return direct

    repaired = tmp.with_name(f".{tmp.name}.jpegtran.jpg")
    try:
        repaired.unlink(missing_ok=True)
        normalise = _run([
            jpegtran, "-copy", "all", "-outfile", str(repaired), str(src),
        ])
        # jpegtran commonly returns 2 for a truncated input even after it has
        # emitted a complete, usable JPEG.  Treat the repaired artifact—not the
        # warning exit—as authoritative and let cwebp plus validation decide.
        if not repaired.is_file() or repaired.stat().st_size == 0:
            detail = normalise.stderr.strip() or "jpegtran produced no usable output"
            return subprocess.CompletedProcess(
                normalise.args,
                normalise.returncode or 1,
                direct.stdout + normalise.stdout,
                direct.stderr + f"\nLossless jpegtran repair failed: {detail}",
            )
        tmp.unlink(missing_ok=True)
        retry = _run(_build_command(
            "photo", repaired, tmp, output_format=output_format,
            photo_effort=photo_effort,
        ))
        if retry.returncode == 0:
            log.warning(
                "       %s: repaired truncated JPEG losslessly with jpegtran before conversion",
                src.name,
            )
        else:
            normalise_detail = normalise.stderr.strip()
            if normalise_detail:
                normalise_detail = f"\njpegtran diagnostic: {normalise_detail}"
            retry.stderr = (
                direct.stderr
                + normalise_detail
                + "\njpegtran normalised the JPEG, but cwebp still rejected it:\n"
                + retry.stderr
            )
        return retry
    finally:
        repaired.unlink(missing_ok=True)


def _heic_primary_stream(src: Path) -> Optional[dict]:
    """The largest still image inside a HEIC.

    HEIC is ISOBMFF, so FFmpeg opens it with the mov/mp4 demuxer and a file
    carrying a thumbnail or depth map presents several video streams. Picking
    by area avoids silently converting the thumbnail.
    """
    from .inventory import heic_image_streams

    streams = heic_image_streams(src)
    if not streams:
        return None
    return max(
        streams,
        key=lambda stream: (stream.get("width") or 0) * (stream.get("height") or 0),
    )


def ffmpeg_can_decode_heic() -> bool:
    """Whether this FFmpeg build can read HEIC stills."""
    from .capabilities import ffmpeg_heic_support

    return ffmpeg_heic_support()


def _decode_heic(src: Path, png: Path) -> subprocess.CompletedProcess:
    """Decode a HEIC still to PNG, preferring sips.

    PNG specifically: both decoders copy the EXIF block into it and cwebp
    extracts EXIF from PNG, where a TIFF intermediate silently loses all
    metadata. FFmpeg reads the same file through its mov/mp4 demuxer and is
    the fallback when sips is missing or chokes on a particular file; any
    metadata it drops is restored afterwards by _repair_photo_metadata via
    ExifTool.
    """
    if shutil.which("sips"):
        sips = _run(["sips", "-s", "format", "png", str(src), "--out", str(png)])
        if sips.returncode == 0:
            return sips
        log.debug("sips could not decode %s (%s); trying ffmpeg", src.name, sips.stderr.strip())
        png.unlink(missing_ok=True)

    stream = _heic_primary_stream(src)
    if stream is None:
        return subprocess.CompletedProcess(
            ["ffprobe", str(src)], 1, "",
            "no decodable still image found in HEIC container",
        )
    index = stream.get("index")
    decoded = _run([
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src),
        "-map", f"0:{index}" if index is not None else "0:v:0",
        "-frames:v", "1", str(png),
    ])
    if decoded.returncode != 0:
        return decoded
    # A HEIC's auxiliary streams are a real hazard here: if the wrong one were
    # decoded the conversion would quietly replace the photo with a thumbnail,
    # and no later check compares photo dimensions.
    expected = (stream.get("width"), stream.get("height"))
    actual = _png_dimensions(png)
    if None not in expected and actual is not None and actual != expected:
        return subprocess.CompletedProcess(
            decoded.args, 1, decoded.stdout,
            decoded.stderr + (
                f"\nHEIC decode produced {actual[0]}x{actual[1]} but the primary "
                f"image is {expected[0]}x{expected[1]}; refusing to replace the "
                "photo with an auxiliary image"
            ),
        )
    return decoded


def _png_dimensions(path: Path) -> Optional[tuple]:
    """Read a PNG's IHDR dimensions without decoding the image."""
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return (
        int.from_bytes(header[16:20], "big"),
        int.from_bytes(header[20:24], "big"),
    )


def _repair_photo_metadata(src: Path, output: Path) -> bool:
    """Ask ExifTool to restore WebP/AVIF-compatible metadata the encoder omitted."""
    if not exiftool_available():
        return False
    result = run_exiftool([
        "-overwrite_original",
        "-TagsFromFile",
        str(src),
        "-EXIF:All",
        "-XMP:All",
        "-ICC_Profile",
        "-xmp:DateCreated<IPTC:DateCreated",
        str(output),
    ])
    updated = bool(result and "image files updated" in result.lower())
    if updated:
        log.warning("       %s: restored dropped photo metadata with exiftool", src.name)
    return updated
