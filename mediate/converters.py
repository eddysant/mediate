"""Subprocess-based conversion with the validation/deletion protocol."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .disposal import Disposer
from .exiftool import exiftool_available, run_exiftool
from .macmeta import get_birthtime, set_birthtime
from .probe import (
    MP4_HEVC,
    MP4_STANDARD,
    STREAM_COPY_VIDEO,
    STREAM_HEVC,
    STREAM_STANDARD,
    COLOR_FIELDS,
    advanced_video_features,
    audio_stream_label,
    gif_is_animated,
    inventory_streams,
    is_commentary_stream,
    media_duration,
    mark_video_healthy,
    mark_conversion_complete,
    mp4_conversion_reason,
    mp4_status,
    preservation_summary,
    preservable_artwork_streams,
    preservable_subtitle_streams,
    primary_video_streams,
    stream_removal_risks,
    video_inventory,
    video_health,
    video_stream_status,
)
from .progress import ConversionCancelled, run_ffmpeg_progress
from .scanner import MediaJob
from .safety import SafetyError, SourceSnapshot, ensure_output_capacity
from .transaction import ReplacementTransaction, TransactionError
from .validators import (
    validate_output,
    verify_photo_metadata,
    verify_apple_playback,
    verify_video_duration,
    verify_video_streams,
)

log = logging.getLogger("mediate")

# Outcome statuses
CONVERTED = "converted"
REMUXED = "remuxed"
REPAIRED = "repaired"
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
    allow_stream_removal: bool = False
    allow_video_downgrade: bool = False
    validate_existing: bool = False
    dispose: Optional[Disposer] = None
    dispose_label: str = "delete original"
    transaction_root: Optional[Path] = None


@dataclass
class Outcome:
    status: str
    path: Path
    detail: str = ""
    bytes_saved: int = 0


def _video_mapping_args(inventory: dict) -> List[str]:
    """Map the primary picture and each preservable stream by absolute index."""
    primary = primary_video_streams(inventory)[0]
    primary_index = primary.get("index")
    args = ["-map", f"0:{primary_index}" if primary_index is not None else "0:v:0"]
    for audio_index, stream in enumerate(inventory_streams(inventory, "audio")):
        source_index = stream.get("index")
        args.extend(["-map", f"0:{source_index}" if source_index is not None else f"0:a:{audio_index}"])
        args.extend([
            f"-map_metadata:s:a:{audio_index}",
            f"0:s:a:{audio_index}",
        ])
        tags = stream.get("tags", {})
        language = tags.get("language")
        if language:
            args.extend([f"-metadata:s:a:{audio_index}", f"language={language}"])
        title = audio_stream_label(stream)
        if not title and is_commentary_stream(stream):
            title = "Commentary"
        if title:
            # MOV reports this field as `name` or `handler_name`; Matroska
            # generally reports `title`. Older FFmpeg MP4 muxers only retain
            # handler_name, so write both forms and validate them equivalently.
            args.extend([
                f"-metadata:s:a:{audio_index}", f"title={title}",
                f"-metadata:s:a:{audio_index}", f"handler_name={title}",
            ])
        dispositions = [
            name for name, enabled in stream.get("disposition", {}).items()
            if enabled
        ]
        args.extend([
            f"-disposition:a:{audio_index}",
            "+".join(dispositions) if dispositions else "0",
        ])
    for subtitle_index, stream in enumerate(preservable_subtitle_streams(inventory)):
        source_index = stream.get("index")
        args.extend(["-map", f"0:{source_index}"])
        tags = stream.get("tags", {})
        for name in ("language", "title"):
            if tags.get(name):
                args.extend([
                    f"-metadata:s:s:{subtitle_index}", f"{name}={tags[name]}"
                ])
        dispositions = [
            name for name, enabled in stream.get("disposition", {}).items()
            if enabled
        ]
        args.extend([
            f"-disposition:s:{subtitle_index}",
            "+".join(dispositions) if dispositions else "0",
        ])
    for artwork_index, stream in enumerate(preservable_artwork_streams(inventory), 1):
        source_index = stream.get("index")
        if source_index is None:
            continue
        args.extend(["-map", f"0:{source_index}"])
        args.extend([f"-disposition:v:{artwork_index}", "attached_pic"])
    return args


def _preserved_stream_codec_args(inventory: dict) -> List[str]:
    args: List[str] = []
    if preservable_subtitle_streams(inventory):
        args.extend(["-c:s", "mov_text"])
    for artwork_index, _stream in enumerate(preservable_artwork_streams(inventory), 1):
        args.extend([f"-c:v:{artwork_index}", "copy"])
    return args


def _video_metadata_args(inventory: dict) -> List[str]:
    """Carry source colour declarations onto the primary output video."""
    videos = primary_video_streams(inventory)
    if not videos:
        return []
    video = videos[0]
    args: List[str] = []
    option_names = {
        "color_range": "color_range",
        "color_space": "colorspace",
        "color_transfer": "color_trc",
        "color_primaries": "color_primaries",
        "chroma_location": "chroma_sample_location",
    }
    for field in COLOR_FIELDS:
        value = video.get("color", {}).get(field)
        if value and value != "unknown":
            args.extend([f"-{option_names[field]}:v:0", str(value)])
    return args


def _build_rotation_command(
    input_path: Path,
    output_path: Path,
    inventory: dict,
) -> List[str]:
    """Remux an encoded MP4 while writing an explicit display matrix.

    FFmpeg preserves a display matrix during stream copy, but does not carry
    it through video encoding. Its legacy output `rotate` metadata also has
    container/version-dependent behaviour, so a short second pass applies the
    documented input-side display override and copies the encoded streams.
    """
    rotation = primary_video_streams(inventory)[0].get("rotation")
    return [
        "ffmpeg", "-nostdin", "-y",
        "-display_rotation:v:0", str(rotation),
        "-i", str(input_path),
        "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
        "-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0",
        "-map_chapters", "0",
        "-c", "copy",
        *_video_metadata_args(inventory),
        "-tag:v:0", "avc1", "-tag:a", "mp4a",
        "-brand", "mp42", "-movflags", "+faststart",
        str(output_path),
    ]


def _build_command(
    kind: str,
    input_path: Path,
    output_path: Path,
    inventory: Optional[dict] = None,
    repair: bool = False,
    copy_video: bool = False,
) -> List[str]:
    if kind == "photo":
        return [
            "cwebp", "-lossless", "-metadata", "all", "-preset", "photo",
            str(input_path), "-o", str(output_path),
        ]
    if kind == "video":
        if inventory is None:
            raise ValueError("video conversion requires a stream inventory")
        input_repair = [
            "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        ] if repair else []
        return [
            "ffmpeg", "-nostdin", "-y", *input_repair,
            "-noautorotate", "-i", str(input_path),
            *_video_mapping_args(inventory),
            "-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0",
            "-map_chapters", "0",
            *(
                ["-c:v:0", "copy"]
                if copy_video
                else ["-c:v:0", "libx264", "-preset", "slow", "-crf", "18"]
            ),
            "-c:a", "aac", "-b:a", "256k",
            *_preserved_stream_codec_args(inventory),
            *([] if copy_video else [
                "-pix_fmt", "yuv420p",
                "-vf", (
                    "setpts=N/FRAME_RATE/TB,pad=ceil(iw/2)*2:ceil(ih/2)*2"
                    if repair else "pad=ceil(iw/2)*2:ceil(ih/2)*2"
                ),
            ]),
            *(["-fps_mode", "passthrough"] if repair else []),
            *_video_metadata_args(inventory),
            "-tag:v:0", "avc1", "-tag:a", "mp4a",
            "-brand", "mp42", "-movflags", "+faststart",
            str(output_path),
        ]
    if kind == "gif":
        return [
            "ffmpeg", "-nostdin", "-y", "-i", str(input_path),
            "-tag:v:0", "avc1", "-brand", "mp42", "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(output_path),
        ]
    raise ValueError(f"unknown job kind: {kind}")


def _build_remux_command(
    input_path: Path,
    output_path: Path,
    inventory: dict,
    repair: bool = False,
) -> List[str]:
    """Remux into MP4 with ``-c copy`` — no re-encoding."""
    input_repair = [
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
    ] if repair else []
    return [
        "ffmpeg", "-nostdin", "-y", *input_repair,
        "-noautorotate", "-i", str(input_path),
        *_video_mapping_args(inventory),
        "-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0",
        "-map_chapters", "0",
        "-c", "copy",
        *_preserved_stream_codec_args(inventory),
        *_video_metadata_args(inventory),
        "-tag:v:0", "avc1", "-tag:a", "mp4a",
        "-brand", "mp42", "-movflags", "+faststart",
        str(output_path),
    ]


def intended_output(job: MediaJob) -> Path:
    """The final path a job will produce, used to detect two inputs that
    map to the same output name before any conversion starts."""
    return job.output or job.path.with_suffix(
        ".webp" if job.kind in ("photo", "heic") else ".mp4"
    )


def unique_output_path(preferred: Path, unavailable=()) -> Path:
    """Return an unused GUID-suffixed alternative without overwriting."""
    blocked = set(unavailable)
    if preferred not in blocked and not preferred.exists():
        return preferred
    while True:
        token = uuid.uuid4().hex[:8]
        candidate = preferred.with_name(f"{preferred.stem}.{token}{preferred.suffix}")
        if candidate not in blocked and not candidate.exists():
            return candidate


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


def _convert_photo(src: Path, tmp: Path) -> subprocess.CompletedProcess:
    """Convert a still, losslessly normalising a damaged JPEG if needed."""
    direct = _run(_build_command("photo", src, tmp))
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
        or src.suffix.lower() not in (".jpg", ".jpeg")
        or not decode_failure
    ):
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
        retry = _run(_build_command("photo", repaired, tmp))
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


def _explain_source_truncation(reason: str, stderr: str) -> str:
    """Replace an output symptom with the irrecoverable source diagnosis."""
    truncation_markers = (
        "File ended prematurely",
        "Input file is truncated",
        "input file is truncated",
    )
    if "duration mismatch" in reason and any(
        marker in stderr for marker in truncation_markers
    ):
        return (
            "source file is truncated; missing media cannot be reconstructed "
            f"({reason})"
        )
    return reason


def _repair_photo_metadata(src: Path, output: Path) -> bool:
    """Ask ExifTool to restore WebP-compatible metadata cwebp omitted."""
    if not exiftool_available():
        return False
    result = run_exiftool([
        "-overwrite_original",
        "-TagsFromFile",
        str(src),
        "-EXIF:All",
        "-XMP:All",
        "-ICC_Profile",
        str(output),
    ])
    updated = bool(result and "image files updated" in result.lower())
    if updated:
        log.warning("       %s: restored dropped photo metadata with exiftool", src.name)
    return updated


def _convert(
    kind: str,
    src: Path,
    tmp: Path,
    inventory: Optional[dict] = None,
    repair: bool = False,
    copy_video: bool = False,
) -> subprocess.CompletedProcess:
    """Run the conversion subprocess(es) for a job. HEIC goes through a
    two-step pipeline: sips (built into macOS, decodes HEVC-compressed
    stills that cwebp cannot read) to a temporary PNG, then the normal
    lossless cwebp encode. PNG specifically: sips copies the EXIF block
    into it and cwebp extracts EXIF from PNG — with a TIFF intermediate
    cwebp drops the metadata ("EXIF extraction from TIFF is unsupported")."""
    if kind in ("video", "gif"):
        rotation = None
        encode_output = tmp
        if kind == "video" and inventory is not None:
            rotation = primary_video_streams(inventory)[0].get("rotation")
            if rotation is not None:
                encode_output = tmp.with_name(f".{tmp.name}.encoded.mp4")
        cmd = _build_command(
            kind, src, encode_output, inventory, repair=repair, copy_video=copy_video
        )
        total = media_duration(src)
        log.debug("running: %s", " ".join(cmd))
        proc = run_ffmpeg_progress(cmd, src, total, "repairing" if repair else "encoding")
        if rotation is None:
            return proc
        if proc.returncode != 0:
            encode_output.unlink(missing_ok=True)
            return proc
        try:
            rotated = _run(_build_rotation_command(encode_output, tmp, inventory))
            return subprocess.CompletedProcess(
                rotated.args,
                rotated.returncode,
                rotated.stdout,
                proc.stderr + rotated.stderr,
            )
        finally:
            encode_output.unlink(missing_ok=True)
    if kind == "photo":
        return _convert_photo(src, tmp)
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


def _sidecars_of(src: Path):
    """Sidecar files belonging to src: photo.aae / photo.xmp / photo.jpg.xmp
    (upper- or lowercase — Apple writes .AAE)."""
    seen = set()
    for ext in (".aae", ".AAE", ".xmp", ".XMP"):
        for candidate in (src.with_suffix(ext), Path(str(src) + ext)):
            if candidate.exists() and candidate not in seen:
                seen.add(candidate)
                yield candidate


def process_job(job: MediaJob, opts: Options) -> Outcome:
    src = job.path
    try:
        source_snapshot = SourceSnapshot.capture(src)
    except SafetyError as exc:
        return Outcome(FAILED, src, f"filesystem safety check failed: {exc}")
    kind = job.kind
    remux = False  # set True when we can use -c copy instead of re-encoding
    copy_video = False
    repair = False
    inventory = None

    if kind == "mp4":
        status = mp4_status(src)
        if status == MP4_STANDARD:
            health = video_health(src)
            apple_health = (
                verify_apple_playback(src)
                if opts.validate_existing and health.get("ok")
                else (True, "ok")
            )
            if health.get("ok") and apple_health[0]:
                return Outcome(
                    SKIPPED,
                    src,
                    "already standardized and healthy MP4 (h264/8-bit 4:2:0/aac)",
                )
            if health.get("ok") and not apple_health[0]:
                log.warning(
                    "       %s: Apple playback validation failed (%s); attempting repair",
                    src.name,
                    apple_health[1],
                )
            repair = True
        if status == MP4_HEVC and not opts.reencode_hevc:
            health = video_health(src)
            if not health.get("ok"):
                return Outcome(
                    SKIPPED,
                    src,
                    "damaged HEVC MP4; --reencode-hevc is required to attempt repair",
                )
            return Outcome(
                SKIPPED, src,
                "HEVC MP4 (smaller than h264 and Apple-native; --reencode-hevc to convert anyway)",
            )
        if status not in (MP4_STANDARD, MP4_HEVC):
            log.info("       %s: MP4 needs standardization because %s", src.name, mp4_conversion_reason(src))
        kind = "video"

    if kind == "video":
        inventory = video_inventory(src)
        if inventory is None or not primary_video_streams(inventory):
            return Outcome(SKIPPED, src, "stream preflight failed; original kept")
        # Probe streams independently of their container. Existing MP4s with
        # compatible H.264 but a non-Apple tag can be remuxed, while files
        # whose only incompatible part is audio keep the video bit-for-bit.
        stream_st = video_stream_status(src)
        if not repair:
            if stream_st == STREAM_STANDARD:
                remux = True
            elif stream_st == STREAM_COPY_VIDEO:
                copy_video = True
            elif stream_st == STREAM_HEVC and not opts.reencode_hevc:
                return Outcome(
                    SKIPPED, src,
                    "HEVC streams (smaller than h264 and Apple-native; --reencode-hevc to convert anyway)",
                )
        risks = stream_removal_risks(inventory)
        if risks and not opts.allow_stream_removal:
            return Outcome(
                SKIPPED,
                src,
                "stream safety warning: would remove " + "; ".join(risks)
                + " (--allow-stream-removal to permit)",
            )
        advanced = advanced_video_features(inventory)
        if advanced and not (remux or copy_video) and not opts.allow_video_downgrade:
            return Outcome(
                SKIPPED, src,
                "advanced video safety warning: 8-bit h264 conversion would change "
                + ", ".join(advanced)
                + " (--allow-video-downgrade to permit)",
            )

    if kind == "gif" and not gif_is_animated(src):
        return Outcome(SKIPPED, src, "static GIF (only animated GIFs are converted)")

    if kind == "heic":
        if not opts.convert_heic:
            return Outcome(SKIPPED, src, "HEIC (already space-efficient; --convert-heic to convert)")
        if sys.platform != "darwin":
            return Outcome(SKIPPED, src, "HEIC conversion requires macOS (sips)")

    new_ext = ".webp" if kind in ("photo", "heic") else ".mp4"
    final = intended_output(job)

    # Re-encoding a non-standard .mp4 targets its own name; that only works
    # if the original is removed first, so pick a new name when keeping it.
    if final == src and opts.keep_originals:
        suffix = ".repaired.mp4" if repair else ".standardized.mp4"
        final = src.with_name(src.stem + suffix)

    if final != src and final.exists():
        previous = final
        final = unique_output_path(final)
        log.info("       %s: %s exists; using %s", src.name, previous.name, final.name)

    if opts.dry_run:
        verb = (
            "repair" if repair else "remux" if remux
            else "convert audio and copy video" if copy_video else "convert"
        )
        action = verb if opts.keep_originals else f"{verb} and {opts.dispose_label}"
        detail = f"would {action} -> {final.name}"
        if inventory is not None:
            preserved = preservation_summary(inventory)
            if preserved:
                detail += f"; preserve {preserved}"
            risks = stream_removal_risks(inventory)
            if risks:
                detail += f"; explicitly remove {'; '.join(risks)}"
        return Outcome(PLANNED, src, detail)

    try:
        ensure_output_capacity(final.parent, source_snapshot.size)
    except SafetyError as exc:
        return Outcome(FAILED, src, f"filesystem safety check failed: {exc}")

    # Convert into a temp name in the same directory, then rename into place
    # only after validation, so a crash never leaves a half-written file
    # wearing the final name.
    tmp = final.with_name(f".{final.stem}.{uuid.uuid4().hex[:8]}.part{final.suffix}")
    src_size_pre = src.stat().st_size
    if repair:
        log.info("       repairing %s (%s), damaged packets may be discarded...", src.name, _fmt_size(src_size_pre))
    elif remux:
        log.debug("remuxing %s (%s) with -c copy", src.name, _fmt_size(src_size_pre))
    else:
        # A big video at -preset slow can encode for many minutes: say so.
        started = f"converting {src.name} ({_fmt_size(src_size_pre)}), this may take a while..."
        log.info("       %s", started) if src_size_pre >= 100 * 1024 * 1024 else log.debug("%s", started)
    lossless_repair_possible = bool(
        kind == "video"
        and video_stream_status(src) == STREAM_STANDARD
    )

    def run_attempt(stage: str):
        tmp.unlink(missing_ok=True)
        if stage in ("remux", "repair-remux"):
            tolerant = stage == "repair-remux"
            cmd = _build_remux_command(src, tmp, inventory, repair=tolerant)
            log.debug("running: %s", " ".join(cmd))
            return run_ffmpeg_progress(
                cmd,
                src,
                media_duration(src),
                "repairing" if tolerant else "remuxing",
            )
        convert_options = {"repair": stage == "repair-encode"}
        if stage == "convert" and copy_video:
            convert_options["copy_video"] = True
        return _convert(
            kind if stage == "convert" else "video",
            src,
            tmp,
            inventory,
            **convert_options,
        )

    def validate_attempt(proc):
        valid, why = validate_output(
            proc.returncode,
            proc.stderr,
            tmp,
            is_video=(new_ext == ".mp4"),
            progress_path=src,
        )
        if not valid:
            return valid, why
        if new_ext == ".webp":
            metadata_result = verify_photo_metadata(src, tmp)
            if metadata_result[0]:
                return metadata_result
            if _repair_photo_metadata(src, tmp):
                return verify_photo_metadata(src, tmp)
            return metadata_result
        if kind == "gif":
            return verify_video_duration(src, tmp)
        result = verify_video_streams(
            src,
            tmp,
            inventory,
            allow_stream_removal=opts.allow_stream_removal,
            allow_video_downgrade=opts.allow_video_downgrade,
        )
        if not result[0]:
            return False, _explain_source_truncation(result[1], proc.stderr)
        return result

    first_stage = (
        "repair-remux" if repair and lossless_repair_possible
        else "repair-encode" if repair
        else "remux" if remux
        else "convert"
    )
    attempted = []
    proc = None
    ok = False
    reason = "conversion was not attempted"
    stages = [first_stage]
    if kind == "video":
        if lossless_repair_possible and "repair-remux" not in stages:
            stages.append("repair-remux")
        if "repair-encode" not in stages:
            stages.append("repair-encode")
    for stage in stages:
        if attempted:
            log.warning(
                "       %s: %s failed (%s); trying %s",
                src.name,
                attempted[-1],
                reason,
                "lossless tolerant remux" if stage == "repair-remux" else "tolerant re-encode",
            )
        try:
            proc = run_attempt(stage)
        except ConversionCancelled:
            tmp.unlink(missing_ok=True)
            tmp.with_name(f".{tmp.name}.encoded.mp4").unlink(missing_ok=True)
            raise
        except FileNotFoundError as exc:
            return Outcome(FAILED, src, f"converter not found: {exc}")
        attempted.append(stage)
        ok, reason = validate_attempt(proc)
        if ok:
            remux = stage == "remux"
            repair = stage.startswith("repair-")
            break
    if not ok:
        log.debug("stderr for %s:\n%s", src, proc.stderr.strip() if proc else "")
        tmp.unlink(missing_ok=True)
        return Outcome(FAILED, src, f"validation failed, original kept: {reason}")

    if not source_snapshot.unchanged(src):
        tmp.unlink(missing_ok=True)
        return Outcome(
            FAILED,
            src,
            "source changed during conversion; output discarded and original kept",
        )

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
            transaction = ReplacementTransaction.prepare(
                opts.transaction_root or src.parent,
                src,
                final,
                tmp,
                opts.dispose,
                sidecars=list(_sidecars_of(src)),
                birthtime=src_birthtime,
                expected_source_identity={
                    "device": source_snapshot.device,
                    "inode": source_snapshot.inode,
                    "size": source_snapshot.size,
                    "mtime_ns": source_snapshot.mtime_ns,
                },
            )
            disposed = f", {transaction.commit()}"
        except (OSError, TransactionError) as exc:
            tmp.unlink(missing_ok=True)
            return Outcome(FAILED, src, f"transaction failed; original recovered: {exc}")
    else:
        os.replace(tmp, final)
        # Preserve the original's timestamps so date-based sorting still works —
        # mtime for everything, plus Finder's creation date on macOS.
        os.utime(final, (src_stat.st_atime, src_stat.st_mtime))
        if src_birthtime is not None:
            set_birthtime(final, src_birthtime)
    if new_ext == ".mp4":
        mark_video_healthy(final)
    if opts.keep_originals:
        mark_conversion_complete(src, final)

    outcome_status = REPAIRED if repair else REMUXED if remux else CONVERTED
    return Outcome(
        outcome_status,
        src,
        f"-> {final.name} ({_fmt_size(src_stat.st_size)} -> {_fmt_size(new_size)}){disposed}",
        bytes_saved=src_stat.st_size - new_size,
    )
