"""CLI entry point: argument parsing, logging setup, worker pool."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__
from .capabilities import check_media_capabilities
from .converters import (
    CONVERTED,
    FAILED,
    PLANNED,
    REPAIRED,
    REMUXED,
    SKIPPED,
    Options,
    Outcome,
    intended_output,
    process_job,
)
from .disposal import GRAVEYARD, HARD, TRASH, make_disposer
from .journal import RunJournal
from .progress import CANCELLATION, LIVE_PROGRESS, ConversionCancelled
from .safety import DiskSpaceReservations, SafetyError
from .scanner import find_live_photo_companions, iter_media
from .transaction import recover_transactions

log = logging.getLogger("mediate")

STATUS_MARKS = {
    CONVERTED: "[ok]",
    REPAIRED: "[fix]",
    REMUXED: "[ok]",
    SKIPPED: "[skip]",
    FAILED: "[FAIL]",
    PLANNED: "[dry]",
}


class ProgressStreamHandler(logging.StreamHandler):
    """Keep ordinary log records from overwriting live progress lines."""

    def emit(self, record) -> None:
        with LIVE_PROGRESS.logging_context():
            super().emit(record)


def _resolve_future(future: Future, job) -> Outcome:
    """Turn an unexpected worker exception into one failed outcome.

    Without this boundary, a single unusual file aborts result collection for
    the entire scan. Remaining files then appear to have been "missed" when
    the command is run again, even though their worker futures were submitted.
    """
    try:
        return future.result()
    except ConversionCancelled:
        return Outcome(SKIPPED, job.path, "cancelled; partial output removed, original kept")
    except Exception as exc:  # process one bad media file without aborting the scan
        log.debug("unexpected worker failure for %s", job.path, exc_info=True)
        return Outcome(FAILED, job.path, f"unexpected processing error: {exc}")


def _filter_standardized_mp4(
    jobs, status_fn, standard_status, health_fn=None, workers=2, validate_health=False
):
    """Separate completed MP4 outputs, optionally full-decoding their health."""
    if health_fn is None:
        from .probe import video_health

        health_fn = video_health

    possible = [
        job for job in jobs
        if job.kind == "mp4" and status_fn(job.path) == standard_status
    ]
    healthy = {job.path for job in possible} if not validate_health else set()
    if possible and validate_health:
        log.info("validating integrity: %d standardized MP4 file(s)", len(possible))
        pool = ThreadPoolExecutor(max_workers=min(max(1, workers), len(possible)))
        futures = {pool.submit(health_fn, job.path): job for job in possible}
        interrupted = False
        try:
            for future in as_completed(futures):
                try:
                    if future.result().get("ok"):
                        healthy.add(futures[future].path)
                except Exception:
                    log.debug(
                        "health check failed for %s", futures[future].path, exc_info=True
                    )
        except KeyboardInterrupt:
            interrupted = True
            CANCELLATION.request()
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            if interrupted:
                LIVE_PROGRESS.clear()
    return [job for job in jobs if job.path not in healthy], len(healthy)


def config_file_path() -> Path:
    override = os.environ.get("MEDIATE_CONFIG")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "mediate" / "config"


def load_config_args() -> list:
    """Default flags from the config file: one flag per line, # comments.
    They are prepended to the command line, so explicit arguments win where
    argparse takes the last value (note: a store_true flag from the config
    cannot be switched off per-run except with --no-config)."""
    path = config_file_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    args = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            args.extend(line.split(None, 1) if line.startswith("--") and " " in line else [line])
    return args


def parse_args(argv=None) -> argparse.Namespace:
    if argv is None:
        argv = sys.argv[1:]
    if "--no-config" not in argv:
        config_args = load_config_args()
        if config_args:
            argv = config_args + list(argv)
    parser = argparse.ArgumentParser(
        prog="mediate",
        description=(
            "Recursively standardize a media library: photos (JPEG/PNG/TIFF) to "
            "lossless WebP via cwebp, videos and animated GIFs to h264/8-bit 4:2:0/AAC "
            "MP4 via ffmpeg. Originals are moved to the Trash only after the "
            "converted file passes a strict validation checklist."
        ),
    )
    parser.add_argument("directory", type=Path, help="target directory to scan recursively")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would happen without converting or deleting anything",
    )
    parser.add_argument(
        "--keep-originals",
        action="store_true",
        help="convert but never touch the original files",
    )
    parser.add_argument(
        "--only-if-smaller",
        action="store_true",
        help="keep the original (and discard the conversion) unless the output is "
        "smaller — lossless WebP can be bigger than a camera JPEG",
    )
    parser.add_argument(
        "--reencode-hevc",
        action="store_true",
        help="re-encode HEVC MP4s to h264 (default: skip them — HEVC is smaller "
        "than h264 and plays natively on Apple devices)",
    )
    parser.add_argument(
        "--convert-heic",
        action="store_true",
        help="convert HEIC/HEIF photos to lossless WebP via macOS sips "
        "(default: skip them; the lossless re-encode of an efficient lossy "
        "format usually grows the file — combine with --only-if-smaller)",
    )
    parser.add_argument(
        "--convert-live-photos",
        action="store_true",
        help="convert .mov files even when a same-named still image sits next to "
        "them (default: skip such pairs, since converting the video half breaks "
        "Live Photo pairing in Apple Photos)",
    )
    parser.add_argument(
        "--allow-stream-removal",
        action="store_true",
        help="allow video conversion to discard streams MP4 standardisation cannot "
        "safely preserve; compatible text subtitles and JPEG/PNG cover streams "
        "are retained automatically",
    )
    parser.add_argument(
        "--allow-video-downgrade",
        action="store_true",
        help="allow 8-bit h264 conversion of HDR/high-bit-depth, alpha, interlaced, "
        "or variable/unusual-frame-rate video (default: warn and skip because "
        "picture semantics may change)",
    )
    parser.add_argument(
        "--validate-existing",
        action="store_true",
        help="fully decode already-standardized MP4 files and attempt repair when "
        "damage is found (default: trust their stream format and skip them)",
    )
    parser.add_argument(
        "--rename",
        action="store_true",
        help="after converting, standardize file names: cleanup + title case, "
        "'(N)' -> '[N]' with gaps closed and zero-padding, GUID names replaced "
        "by the folder name + [guid]",
    )
    parser.add_argument(
        "--rename-only",
        action="store_true",
        help="only standardize file names; no conversions",
    )
    parser.add_argument(
        "--rename-folders",
        action="store_true",
        help="with --rename/--rename-only: also clean directory names",
    )
    parser.add_argument(
        "--date-prefix",
        action="store_true",
        help="with --rename/--rename-only: prefix names with the capture date "
        "(YYYY-MM-DD, from EXIF via exiftool if installed, video metadata, or "
        "file modification time)",
    )
    parser.add_argument(
        "--undo-renames",
        action="store_true",
        help="reverse the most recent rename batch recorded in the library's "
        ".mediate-renames.json, then exit",
    )
    parser.add_argument(
        "--plan-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="with --rename/--rename-only: write the proposed renames to PATH "
        "as editable JSON instead of applying them",
    )
    parser.add_argument(
        "--apply-plan",
        type=Path,
        default=None,
        metavar="PATH",
        help="apply a (possibly hand-edited) plan written by --plan-file, then exit",
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        help="ignore the config file (~/.config/mediate/config)",
    )
    disposal = parser.add_mutually_exclusive_group()
    disposal.add_argument(
        "--graveyard",
        type=Path,
        default=None,
        metavar="DIR",
        help="move originals into DIR (mirroring the folder structure) instead of the Trash",
    )
    disposal.add_argument(
        "--hard-delete",
        action="store_true",
        help="permanently delete originals instead of moving them to the Trash",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        metavar="N",
        help="concurrent conversions (default: 2; ffmpeg is multithreaded on its own, "
        "so high values mostly help photo-heavy libraries)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="detailed log file (default: conversion.log inside the target directory)",
    )
    parser.add_argument("--verbose", action="store_true", help="show debug output on the console")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def setup_logging(log_path: Path, verbose: bool) -> None:
    log.setLevel(logging.DEBUG)

    LIVE_PROGRESS.configure(sys.stdout, enabled=True)
    console = ProgressStreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    log.addHandler(file_handler)


def main(argv=None) -> int:
    args = parse_args(argv)
    CANCELLATION.reset()

    root = args.directory.expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    log_path = args.log_file or (root / "conversion.log")
    setup_logging(log_path, args.verbose)

    recovery = recover_transactions(root, dry_run=args.dry_run)
    for message in recovery.messages:
        level = logging.ERROR if message.startswith("unresolved") else logging.WARNING
        log.log(level, "transaction recovery: %s", message)
    if recovery.completed or recovery.rolled_back:
        qualifier = "would handle" if args.dry_run else "handled"
        log.warning(
            "transaction recovery: %s %d installed and %d incomplete conversion(s)",
            qualifier,
            recovery.completed,
            recovery.rolled_back,
        )
    if recovery.unresolved and not args.dry_run:
        log.error(
            "refusing new work while %d transaction(s) require manual recovery",
            recovery.unresolved,
        )
        return 2

    if args.undo_renames:
        from .renamer import undo_last_batch

        restored = undo_last_batch(root, args.dry_run)
        log.info("names: %d rename(s) %srestored", restored, "would be " if args.dry_run else "")
        return 0

    if args.apply_plan:
        from .renamer import apply_renames, load_plan, record_batch

        try:
            plans = load_plan(root, args.apply_plan)
        except (OSError, ValueError, KeyError) as exc:
            print(f"error: cannot load plan {args.apply_plan}: {exc}", file=sys.stderr)
            return 2
        renamed, skipped, applied = apply_renames(plans, root, args.dry_run)
        if not args.dry_run:
            record_batch(root, applied)
        log.info(
            "plan applied: %d renamed, %d skipped%s",
            renamed, skipped, " (dry run)" if args.dry_run else " (undo with --undo-renames)",
        )
        return 0

    mode = HARD if args.hard_delete else GRAVEYARD if args.graveyard else TRASH
    if sys.platform == "win32" and mode == TRASH and not (args.keep_originals or args.dry_run or args.rename_only):
        print(
            "error: the Windows Recycle Bin is not supported; use --graveyard DIR or --hard-delete",
            file=sys.stderr,
        )
        return 2
    dispose, dispose_label = make_disposer(mode, root, args.graveyard)
    opts = Options(
        dry_run=args.dry_run,
        keep_originals=args.keep_originals,
        only_if_smaller=args.only_if_smaller,
        reencode_hevc=args.reencode_hevc,
        convert_heic=args.convert_heic,
        allow_stream_removal=args.allow_stream_removal,
        allow_video_downgrade=args.allow_video_downgrade,
        dispose=dispose,
        dispose_label=dispose_label,
        transaction_root=root,
    )

    if args.rename_only:
        return run_rename_phase(root, args)

    from .probe import MP4_STANDARD, load_probe_cache, mp4_status, save_probe_cache

    load_probe_cache()
    recognized = list(iter_media(root))
    capability_report = check_media_capabilities(
        require_video=any(job.kind in {"video", "mp4", "gif"} for job in recognized),
        require_photos=any(
            job.kind == "photo" or (job.kind == "heic" and args.convert_heic)
            for job in recognized
        ),
    )
    for name, version in capability_report.versions.items():
        log.debug("toolchain: %s: %s", name, version)
    for warning in capability_report.warnings:
        log.warning("toolchain warning: %s", warning)
    if capability_report.errors:
        for error in capability_report.errors:
            log.error("toolchain error: %s", error)
        if not args.dry_run:
            return 2
        log.warning("dry run continuing despite toolchain errors; probe results may be incomplete")
    # Already-standardized MP4 outputs are recognized media, not unfinished
    # work. Filtering them before the headline count stops repeat runs over a
    # completed library from looking as though files were missed previously.
    try:
        jobs, standardized_count = _filter_standardized_mp4(
            recognized,
            mp4_status,
            MP4_STANDARD,
            workers=args.workers,
            validate_health=args.validate_existing,
        )
    except KeyboardInterrupt:
        log.error("interrupted during integrity validation; originals untouched")
        save_probe_cache()
        return 130
    # Preserve expensive integrity results even if a later conversion is
    # interrupted; newly produced outputs are saved again at the end.
    save_probe_cache()
    run_mode = " (dry run)" if args.dry_run else ""
    log.info("scanning %s%s: %d candidate file(s), log: %s", root, run_mode, len(jobs), log_path)
    if standardized_count:
        log.info("already standardized: %d MP4 file(s)", standardized_count)
    if not args.keep_originals and not args.dry_run:
        log.info("originals: %s after validation", dispose_label)
    if not jobs:
        log.info("nothing to convert")
        save_probe_cache()
        return run_rename_phase(root, args) if args.rename else 0

    # Planning-time skips, resolved before the pool starts:
    # 1. Live Photo pairs — converting the .mov half breaks the pairing.
    # 2. Two inputs mapping to the same output name (a.jpg + a.png -> a.webp):
    #    concurrent workers would both pass the exists() pre-check and the
    #    later rename would clobber.
    companions = {} if args.convert_live_photos else find_live_photo_companions(jobs)
    # Both halves of a pair are protected: converting either one breaks the
    # ContentIdentifier link Apple Photos uses to reunite them.
    protected = {}
    for mov, still in companions.items():
        protected[mov] = f"Live Photo video of {still.name} (--convert-live-photos to convert)"
        protected[still] = f"Live Photo still of {mov.name} (--convert-live-photos to convert)"
    claimed = {}
    runnable = []
    planned_skips = []
    for job in jobs:
        if job.path in protected:
            planned_skips.append(Outcome(SKIPPED, job.path, protected[job.path]))
            continue
        out = intended_output(job)
        if out in claimed:
            planned_skips.append(
                Outcome(SKIPPED, job.path, f"output name collides with {claimed[out].name}")
            )
        else:
            claimed[out] = job.path
            runnable.append(job)

    journal = RunJournal(root, enabled=not args.dry_run)
    runnable, resumed_count = journal.prepare(runnable)
    if resumed_count:
        log.info("resume journal: prioritizing %d interrupted/unfinished file(s)", resumed_count)

    space_reservations = DiskSpaceReservations()

    def process_journaled(job):
        journal.mark(job, "running")
        try:
            source_size = job.path.stat().st_size
            with space_reservations.acquire(
                job.path.parent,
                source_size,
                cancelled=CANCELLATION.requested,
            ):
                outcome = process_job(job, opts)
        except ConversionCancelled:
            journal.mark(job, "interrupted", "cancelled; original kept")
            raise
        except (OSError, SafetyError) as exc:
            outcome = Outcome(FAILED, job.path, f"filesystem safety check failed: {exc}")
        except Exception as exc:
            journal.mark(job, "failed", f"unexpected processing error: {exc}")
            raise
        journal.mark(job, outcome.status, outcome.detail)
        return outcome

    tally = {
        CONVERTED: 0,
        REMUXED: 0,
        REPAIRED: 0,
        SKIPPED: 0,
        FAILED: 0,
        PLANNED: 0,
    }
    bytes_saved = 0
    for outcome in planned_skips:
        tally[outcome.status] += 1
        log.info("%-6s %s %s", STATUS_MARKS[outcome.status], outcome.path.relative_to(root), outcome.detail)
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    futures = {pool.submit(process_journaled, job): job for job in runnable}
    interrupted = False
    try:
        for future in as_completed(futures):
            outcome = _resolve_future(future, futures[future])
            tally[outcome.status] += 1
            bytes_saved += outcome.bytes_saved
            rel = outcome.path.relative_to(root)
            level = logging.ERROR if outcome.status == FAILED else logging.INFO
            log.log(level, "%-6s %s %s", STATUS_MARKS[outcome.status], rel, outcome.detail)
    except KeyboardInterrupt:
        interrupted = True
        CANCELLATION.request()
        for future in futures:
            future.cancel()
        log.error("interrupted; conversions already validated are kept, others untouched")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        LIVE_PROGRESS.clear()
    if interrupted:
        journal.finish(interrupted=True)
        save_probe_cache()
        return 130
    journal.finish(interrupted=False)

    if args.dry_run:
        log.info(
            "dry run complete: %d would be converted, %d skipped",
            tally[PLANNED], tally[SKIPPED],
        )
    else:
        saved_mb = bytes_saved / (1024 * 1024)
        parts = []
        if tally[CONVERTED]:
            parts.append(f"{tally[CONVERTED]} converted")
        if tally[REMUXED]:
            parts.append(f"{tally[REMUXED]} remuxed")
        if tally[REPAIRED]:
            parts.append(f"{tally[REPAIRED]} repaired")
        parts.append(f"{tally[SKIPPED]} skipped")
        parts.append(f"{tally[FAILED]} failed")
        parts.append(f"{saved_mb:.1f} MB saved")
        log.info("done: %s", ", ".join(parts))
    save_probe_cache()
    if args.rename:
        # Rename runs after conversion so freshly produced .webp/.mp4 files
        # get their names standardized in the same pass.
        run_rename_phase(root, args)
    return 1 if tally[FAILED] else 0


def run_rename_phase(root: Path, args: argparse.Namespace) -> int:
    from .renamer import apply_renames, plan_folder_renames, plan_renames, record_batch, write_plan

    dry_run = args.dry_run
    plans = plan_renames(root, date_prefix=args.date_prefix)
    folder_plans = plan_folder_renames(root) if args.rename_folders else []
    if args.plan_file:
        # Files before folders: the same order apply uses, so an edited plan
        # replayed via --apply-plan keeps paths valid.
        write_plan(root, plans + folder_plans, args.plan_file)
        log.info(
            "names: plan with %d rename(s) written to %s — review/edit, then run "
            "mediate %s --apply-plan %s",
            len(plans) + len(folder_plans), args.plan_file, root, args.plan_file,
        )
        return 0
    if not plans and not folder_plans:
        log.info("names: nothing to rename")
        return 0
    renamed, skipped, applied = apply_renames(plans, root, dry_run)
    # Folders move only after every file rename has resolved, so file plan
    # paths stay valid; the manifest keeps that order for a correct undo.
    f_renamed, f_skipped, f_applied = apply_renames(folder_plans, root, dry_run)
    if not dry_run:
        record_batch(root, applied + f_applied)
    if dry_run:
        log.info("names: %d file(s) and %d folder(s) would be renamed", renamed, f_renamed)
    else:
        log.info(
            "names: %d file(s) and %d folder(s) renamed, %d skipped (undo with --undo-renames)",
            renamed, f_renamed, skipped + f_skipped,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
