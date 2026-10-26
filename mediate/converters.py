"""Subprocess-based conversion with the validation/deletion protocol."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .disposal import Disposer
from .macmeta import get_birthtime, set_birthtime
from .proc import run as _run
from .probe import (
    MP4_HEVC,
    MP4_STANDARD,
    STREAM_COPY_VIDEO,
    STREAM_HEVC,
    STREAM_STANDARD,
    gif_is_animated,
    mark_conversion_complete,
    mark_video_healthy,
    media_duration,
    mp4_conversion_reason,
    mp4_status,
    video_health,
    video_stream_status,
    webp_animation_info,
)
from .inventory import (
    COLOR_FIELDS,
    advanced_video_features,
    audio_stream_label,
    inventory_streams,
    is_commentary_stream,
    preservable_artwork_streams,
    preservable_subtitle_streams,
    preservation_summary,
    primary_video_streams,
    stream_removal_risks,
    video_inventory,
)
from .ffmpegcmd import (
    DEFAULT_ENCODE_PRESET,
    DEFAULT_PHOTO_EFFORT,
    _build_command,
    _build_remux_command,
    _build_rotation_command,
)
from .photo import (
    _convert_photo,
    _decode_heic,
    _repair_photo_metadata,
    ffmpeg_can_decode_heic,
)
from .progress import ConversionCancelled, run_ffmpeg_progress
from .scanner import MediaJob
from .safety import SafetyError, SourceSnapshot, ensure_output_capacity
from .transaction import ReplacementTransaction, TransactionError
from .validators import (
    validate_output,
    verify_photo_metadata,
    verify_apple_playback,
    verify_webp_is_lossless,
)
from .videoverify import verify_video_duration, verify_video_streams

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
    output_format: str = "webp"  # "webp" | "avif"
    encode_preset: str = DEFAULT_ENCODE_PRESET
    hardware: bool = False
    photo_effort: str = DEFAULT_PHOTO_EFFORT
    dispose: Optional[Disposer] = None
    dispose_label: str = "delete original"
    transaction_root: Optional[Path] = None


@dataclass
class Outcome:
    status: str
    path: Path
    detail: str = ""
    bytes_saved: int = 0
    # The fixed phrase this file is counted under in the closing summary.
    # A detail that embeds a filename, an exception or a stream list is
    # unique per file and would become a group of one, so those sites set a
    # reason; a detail that is already constant is its own reason.
    reason: str = ""



def intended_output(job: MediaJob, output_format: str = "webp") -> Path:
    """The final path a job will produce, used to detect two inputs that
    map to the same output name before any conversion starts."""
    if job.output:
        return job.output
    if job.kind in ("photo", "heic"):
        ext = ".avif" if output_format == "avif" else ".webp"
    else:
        ext = ".mp4"
    return job.path.with_suffix(ext)


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


def _source_looks_truncated(stderr: str) -> bool:
    truncation_markers = (
        "File ended prematurely",
        "Input file is truncated",
        "input file is truncated",
    )
    return any(marker in stderr for marker in truncation_markers)


def _explain_source_truncation(reason: str, stderr: str) -> str:
    """Replace an output symptom with the irrecoverable source diagnosis."""
    if "duration mismatch" in reason and _source_looks_truncated(stderr):
        return (
            "source file is truncated; missing media cannot be reconstructed "
            f"({reason})"
        )
    return reason


def _convert(
    kind: str,
    src: Path,
    tmp: Path,
    inventory: Optional[dict] = None,
    repair: bool = False,
    copy_video: bool = False,
    output_format: str = "webp",
    encode_preset: str = DEFAULT_ENCODE_PRESET,
    hardware: bool = False,
    photo_effort: str = DEFAULT_PHOTO_EFFORT,
) -> subprocess.CompletedProcess:
    """Run the conversion subprocess(es) for a job. HEIC goes through a
    two-step pipeline: sips (built into macOS, decodes HEVC-compressed
    stills that cwebp cannot read) to a temporary PNG, then the normal
    lossless cwebp/avifenc encode. PNG specifically: sips copies the EXIF block
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
            kind, src, encode_output, inventory,
            repair=repair, copy_video=copy_video,
            encode_preset=encode_preset, hardware=hardware,
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
            rotated = _run(
                _build_rotation_command(
                    encode_output, tmp, inventory, copy_video=copy_video
                )
            )
            return subprocess.CompletedProcess(
                rotated.args,
                rotated.returncode,
                rotated.stdout,
                proc.stderr + rotated.stderr,
            )
        finally:
            encode_output.unlink(missing_ok=True)
    if kind == "photo":
        return _convert_photo(
            src, tmp, output_format=output_format, photo_effort=photo_effort,
        )
    if kind != "heic":
        return _run(_build_command(
            kind, src, tmp, encode_preset=encode_preset, hardware=hardware,
        ))

    png = tmp.with_suffix(".png")
    try:
        decoded = _decode_heic(src, png)
        if decoded.returncode != 0:
            return decoded
        return _run(_build_command(
            "photo", png, tmp, output_format=output_format,
            photo_effort=photo_effort,
        ))
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


@dataclass
class Plan:
    """What classify_job decided a job needs, before anything is written."""

    job: MediaJob
    kind: str
    remux: bool = False       # -c copy into MP4 instead of re-encoding
    copy_video: bool = False  # re-encode audio only, keep the picture bit-for-bit
    repair: bool = False
    inventory: Optional[dict] = None


def classify_job(job: MediaJob, opts: Options):
    """Decide how (or whether) to convert a job.

    Every probe-based skip lives here, ahead of any temp file, encode, or
    disposal, so the safety gates can be exercised without touching the
    filesystem beyond reading the source. Returns an Outcome when the job is
    skipped outright, otherwise a Plan describing the work.
    """
    src = job.path
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
                remux = True
        risks = stream_removal_risks(inventory)
        if risks and not opts.allow_stream_removal:
            return Outcome(
                SKIPPED,
                src,
                "stream safety warning: would remove " + "; ".join(risks)
                + " (--allow-stream-removal to permit)",
                reason="stream would be removed (--allow-stream-removal to permit)",
            )
        advanced = advanced_video_features(inventory)
        if advanced and not (remux or copy_video) and not opts.allow_video_downgrade:
            return Outcome(
                SKIPPED, src,
                "advanced video safety warning: 8-bit h264 conversion would change "
                + ", ".join(advanced)
                + " (--allow-video-downgrade to permit)",
                reason="advanced video feature (--allow-video-downgrade to permit)",
            )

    if kind == "gif" and not gif_is_animated(src):
        kind = "photo"
        job = replace(job, kind="photo")

    if kind == "webp":
        webp_info = webp_animation_info(src)
        if not webp_info["animated"]:
            return Outcome(SKIPPED, src, "static WebP (already standardized)")
        metadata = [name.upper() for name in ("icc", "exif", "xmp") if webp_info[name]]
        if metadata and not opts.allow_stream_removal:
            return Outcome(
                SKIPPED,
                src,
                "stream safety warning: animated WebP carries "
                + "/".join(metadata)
                + " metadata that MP4 cannot preserve "
                + "(--allow-stream-removal to permit)",
                reason="animated WebP metadata MP4 cannot keep "
                       "(--allow-stream-removal to permit)",
            )
        if webp_info["alpha"] and not opts.allow_video_downgrade:
            return Outcome(
                SKIPPED,
                src,
                "advanced video safety warning: 8-bit h264 conversion would change "
                "WebP alpha transparency (--allow-video-downgrade to permit)",
            )

    if kind == "heic":
        if not opts.convert_heic:
            return Outcome(SKIPPED, src, "HEIC (already space-efficient; --convert-heic to convert)")
        if sys.platform != "darwin" and not ffmpeg_can_decode_heic():
            return Outcome(
                SKIPPED,
                src,
                "HEIC conversion needs macOS sips or an FFmpeg build that reads "
                "HEIC stills",
            )

    return Plan(
        job=job,
        kind=kind,
        remux=remux,
        copy_video=copy_video,
        repair=repair,
        inventory=inventory,
    )


def process_job(job: MediaJob, opts: Options) -> Outcome:
    src = job.path
    try:
        source_snapshot = SourceSnapshot.capture(src)
    except SafetyError as exc:
        return Outcome(
            FAILED, src, f"filesystem safety check failed: {exc}",
            reason="filesystem safety check failed",
        )

    decision = classify_job(job, opts)
    if isinstance(decision, Outcome):
        return decision
    job = decision.job
    kind = decision.kind
    remux = decision.remux
    copy_video = decision.copy_video
    repair = decision.repair
    inventory = decision.inventory

    new_ext = (
        f".{opts.output_format}" if kind in ("photo", "heic") else ".mp4"
    )
    final = intended_output(job, output_format=opts.output_format)

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
        return Outcome(PLANNED, src, detail, reason=f"would {action}")

    try:
        ensure_output_capacity(final.parent, source_snapshot.size)
    except SafetyError as exc:
        return Outcome(
            FAILED, src, f"filesystem safety check failed: {exc}",
            reason="filesystem safety check failed",
        )

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
        convert_options = {
            "repair": stage == "repair-encode",
            "encode_preset": opts.encode_preset,
            "hardware": opts.hardware,
            "photo_effort": opts.photo_effort,
        }
        if stage == "convert" and copy_video:
            convert_options["copy_video"] = True
        if opts.output_format != "webp":
            convert_options["output_format"] = opts.output_format
        return _convert(
            kind if stage == "convert" else "video",
            src,
            tmp,
            inventory,
            **convert_options,
        )

    salvaged_source = False
    salvage_detail = ""

    def validate_attempt(proc, stage: str):
        nonlocal salvaged_source, salvage_detail
        valid, why = validate_output(
            proc.returncode,
            proc.stderr,
            tmp,
            is_video=(new_ext == ".mp4"),
            progress_path=src,
        )
        if not valid:
            return valid, why
        if new_ext in (".webp", ".avif"):
            # The encode claims to be lossless; check the file, not the flag.
            if new_ext == ".webp":
                lossless_ok, lossless_why = verify_webp_is_lossless(tmp)
                if not lossless_ok:
                    return lossless_ok, lossless_why
            metadata_result = verify_photo_metadata(src, tmp)
            if metadata_result[0]:
                return metadata_result
            if _repair_photo_metadata(src, tmp):
                return verify_photo_metadata(src, tmp)
            return metadata_result
        if kind in ("gif", "webp"):
            return verify_video_duration(src, tmp)
        result = verify_video_streams(
            src,
            tmp,
            inventory,
            allow_stream_removal=opts.allow_stream_removal,
            allow_video_downgrade=opts.allow_video_downgrade,
            allow_truncated_source=_source_looks_truncated(proc.stderr),
            preserve_video_codec=(
                stage in ("remux", "repair-remux")
                or (stage == "convert" and copy_video)
            ),
        )
        if not result[0]:
            return False, _explain_source_truncation(result[1], proc.stderr)
        if result[1] != "ok":
            salvaged_source = True
            salvage_detail = result[1]
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
            return Outcome(
            FAILED, src, f"converter not found: {exc}",
            reason="converter not found",
        )
        attempted.append(stage)
        ok, reason = validate_attempt(proc, stage)
        if ok:
            remux = stage == "remux"
            repair = stage.startswith("repair-")
            break
    if not ok:
        log.debug("stderr for %s:\n%s", src, proc.stderr.strip() if proc else "")
        tmp.unlink(missing_ok=True)
        return Outcome(
            FAILED, src, f"validation failed, original kept: {reason}",
            reason="validation failed; original kept",
        )

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
            reason="output not smaller than the original (--only-if-smaller)",
        )

    disposed = ""
    keep_source = opts.keep_originals or salvaged_source
    if not keep_source and opts.dispose is not None:
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
            return Outcome(
            FAILED, src, f"transaction failed; original recovered: {exc}",
            reason="transaction failed; original recovered",
        )
    else:
        os.replace(tmp, final)
        # Preserve the original's timestamps so date-based sorting still works —
        # mtime for everything, plus Finder's creation date on macOS.
        os.utime(final, (src_stat.st_atime, src_stat.st_mtime))
        if src_birthtime is not None:
            set_birthtime(final, src_birthtime)
    if new_ext == ".mp4":
        mark_video_healthy(final)
    if keep_source:
        mark_conversion_complete(src, final)

    outcome_status = REPAIRED if repair or salvaged_source else REMUXED if remux else CONVERTED
    recovery = f", {salvage_detail}; damaged original kept" if salvaged_source else ""
    return Outcome(
        outcome_status,
        src,
        f"-> {final.name} ({_fmt_size(src_stat.st_size)} -> {_fmt_size(new_size)})"
        f"{disposed}{recovery}",
        bytes_saved=src_stat.st_size - new_size,
    )
