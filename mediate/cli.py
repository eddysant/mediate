"""CLI entry point: argument parsing, logging setup, worker pool."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__
from .converters import (
    CONVERTED,
    FAILED,
    PLANNED,
    SKIPPED,
    Options,
    Outcome,
    intended_output,
    process_job,
)
from .disposal import GRAVEYARD, HARD, TRASH, make_disposer
from .scanner import find_live_photo_companions, iter_media

log = logging.getLogger("mediate")

REQUIRED_TOOLS = ("cwebp", "ffmpeg", "ffprobe")

STATUS_MARKS = {
    CONVERTED: "[ok]",
    SKIPPED: "[skip]",
    FAILED: "[FAIL]",
    PLANNED: "[dry]",
}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mediate",
        description=(
            "Recursively standardize a media library: photos (JPEG/PNG/TIFF) to "
            "lossless WebP via cwebp, videos and animated GIFs to h264/yuv420p/AAC "
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

    console = logging.StreamHandler(sys.stdout)
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

    root = args.directory.expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing and not args.dry_run and not args.rename_only:
        print(
            f"error: required tool(s) not found on PATH: {', '.join(missing)}\n"
            "install them first, e.g.: brew install webp ffmpeg",
            file=sys.stderr,
        )
        return 2

    log_path = args.log_file or (root / "conversion.log")
    setup_logging(log_path, args.verbose)

    if args.undo_renames:
        from .renamer import undo_last_batch

        restored = undo_last_batch(root, args.dry_run)
        log.info("names: %d rename(s) %srestored", restored, "would be " if args.dry_run else "")
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
        dispose=dispose,
        dispose_label=dispose_label,
    )

    if args.rename_only:
        return run_rename_phase(root, args)

    from .probe import load_probe_cache, save_probe_cache

    load_probe_cache()
    jobs = list(iter_media(root))
    run_mode = " (dry run)" if args.dry_run else ""
    log.info("scanning %s%s: %d candidate file(s), log: %s", root, run_mode, len(jobs), log_path)
    if not args.keep_originals and not args.dry_run:
        log.info("originals: %s after validation", dispose_label)
    if missing:
        log.warning(
            "dry run without %s installed: MP4/GIF probing treats those files as needing conversion",
            ", ".join(missing),
        )
    if not jobs:
        log.info("nothing to convert")
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

    tally = {CONVERTED: 0, SKIPPED: 0, FAILED: 0, PLANNED: 0}
    bytes_saved = 0
    for outcome in planned_skips:
        tally[outcome.status] += 1
        log.info("%-6s %s %s", STATUS_MARKS[outcome.status], outcome.path.relative_to(root), outcome.detail)
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(process_job, job, opts): job for job in runnable}
            for future in as_completed(futures):
                outcome: Outcome = future.result()
                tally[outcome.status] += 1
                bytes_saved += outcome.bytes_saved
                rel = outcome.path.relative_to(root)
                level = logging.ERROR if outcome.status == FAILED else logging.INFO
                log.log(level, "%-6s %s %s", STATUS_MARKS[outcome.status], rel, outcome.detail)
    except KeyboardInterrupt:
        log.error("interrupted; conversions already validated are kept, others untouched")
        return 130

    if args.dry_run:
        log.info(
            "dry run complete: %d would be converted, %d skipped",
            tally[PLANNED], tally[SKIPPED],
        )
    else:
        saved_mb = bytes_saved / (1024 * 1024)
        log.info(
            "done: %d converted, %d skipped, %d failed, %.1f MB saved",
            tally[CONVERTED], tally[SKIPPED], tally[FAILED], saved_mb,
        )
    save_probe_cache()
    if args.rename:
        # Rename runs after conversion so freshly produced .webp/.mp4 files
        # get their names standardized in the same pass.
        run_rename_phase(root, args)
    return 1 if tally[FAILED] else 0


def run_rename_phase(root: Path, args: argparse.Namespace) -> int:
    from .renamer import apply_renames, plan_folder_renames, plan_renames, record_batch

    dry_run = args.dry_run
    plans = plan_renames(root, date_prefix=args.date_prefix)
    folder_plans = plan_folder_renames(root) if args.rename_folders else []
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
