"""Filename standardization: cleanup, numbering, GUID replacement.

Runs as a separate phase from conversion (`--rename` / `--rename-only`).
Rules, in order of application per file:

- GUID stems become "<folder name> [<guid>]" — a GUID says nothing, the
  folder usually says everything.
- "Copy of X" / "X - copy" / "X copy 2" markers are folded into numbering.
- Trailing "(N)" / "[N]" is parsed out; the survivors are renumbered per
  (directory, base, extension) series: gaps closed, zero-padded to the
  width of the largest number once the series reaches double digits, and
  re-emitted as " [N]".
- The base gets cleaned: NFC-normalized, underscores/dots to spaces,
  whitespace collapsed, lowercase words title-cased (small words like
  "of"/"van" stay lower unless leading). Camera counters (IMG_1234,
  DSC_0001, PXL_...) and screenshot/WhatsApp names are left verbatim —
  there is nothing human to fix in them and their dots are data.
- Extensions are lowercased.

Safety: a rename never overwrites. Targets already on disk (other than the
file itself — APFS is case-insensitive) or claimed by another rename cause a
logged skip. Live Photo .mov halves mirror their still's rename instead of
renumbering independently, and .aae/.xmp sidecars follow their media file.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .scanner import BUNDLE_EXTS, GIF_EXTS, HEIC_EXTS, MP4_EXTS, PHOTO_EXTS, VIDEO_EXTS

log = logging.getLogger("mediate")

RENAME_EXTS = PHOTO_EXTS | HEIC_EXTS | GIF_EXTS | VIDEO_EXTS | MP4_EXTS | {".webp"}
SIDECAR_EXTS = {".aae", ".xmp"}

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$|^[0-9a-fA-F]{32}$"
)
NUMBER_RE = re.compile(r"^(?P<base>.*?)[ _]*(?:\((?P<n1>\d+)\)|\[(?P<n2>\d+)\])$")
COPY_OF_RE = re.compile(r"^copy of[ _]+(?P<base>.+)$", re.IGNORECASE)
COPY_PAREN_RE = re.compile(r"^(?P<base>.*?)[ _-]*\(\s*copy\s*(?P<n>\d+)?\s*\)$", re.IGNORECASE)
COPY_TAIL_RE = re.compile(r"^(?P<base>.*?)[ _-]+copy(?:[ _-]*(?P<n>\d+))?$", re.IGNORECASE)
# Camera counters: nothing human-readable to fix, leave the stem verbatim.
PROTECTED_RE = re.compile(
    r"^(?:IMG|DSC[NF]?|MVI|VID|PXL|GOPR|PANO|DJI)[-_]?E?\d+(?:[-_]\d+)*$", re.IGNORECASE
)
# Names whose dots/format are meaningful (timestamps): skip word cleanup.
NO_CLEAN_RE = re.compile(r"^(?:screen ?shot|screenshot|whatsapp)", re.IGNORECASE)

SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "de", "del", "der", "di",
    "for", "in", "la", "le", "nor", "of", "on", "or", "the", "to",
    "van", "von", "with",
}


@dataclass
class ParsedName:
    base: str
    number: Optional[int]
    is_dup: bool  # carried a copy marker


@dataclass
class Rename:
    src: Path
    dst: Path


def parse_stem(stem: str) -> ParsedName:
    dup = False
    m = COPY_OF_RE.match(stem)
    if m:
        stem = m.group("base")
        dup = True
    m = NUMBER_RE.match(stem)
    if m and (m.group("n1") or m.group("n2")):
        return ParsedName(m.group("base"), int(m.group("n1") or m.group("n2")), dup)
    for pattern in (COPY_PAREN_RE, COPY_TAIL_RE):
        m = pattern.match(stem)
        if m:
            n = int(m.group("n")) if m.group("n") else None
            return ParsedName(m.group("base"), n, True)
    return ParsedName(stem, None, dup)


def clean_base(base: str) -> str:
    base = unicodedata.normalize("NFC", base)
    if PROTECTED_RE.match(base) or NO_CLEAN_RE.match(base):
        return base.strip()
    base = re.sub(r"[_.]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" -_.")
    words = base.split(" ")
    out = []
    for i, word in enumerate(words):
        if word.islower():
            if i > 0 and word in SMALL_WORDS:
                out.append(word)
            else:
                out.append(word[:1].upper() + word[1:])
        else:
            out.append(word)  # mixed/upper case is intentional: leave it
    return " ".join(out)


@dataclass
class _Member:
    path: Path
    stem: str
    parsed: ParsedName
    cleaned: str
    ext: str


def _walk_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in sorted(dirnames)
            if not d.startswith(".") and Path(d).suffix.lower() not in BUNDLE_EXTS
        ]
        for name in sorted(filenames):
            if not name.startswith("."):
                yield Path(dirpath) / name


def _assign(groups: Dict[Tuple[Path, str, str], List[_Member]]) -> Iterator[Tuple[_Member, str]]:
    """Yield (member, final_stem) with gap-free, width-padded numbering."""
    for members in groups.values():
        plain = [m for m in members if m.parsed.number is None and not m.parsed.is_dup]
        numbered = sorted(
            (m for m in members if m.parsed.number is not None and not m.parsed.is_dup),
            key=lambda m: (m.parsed.number, m.path.name),
        )
        dups = sorted(
            (m for m in members if m.parsed.is_dup),
            key=lambda m: (m.parsed.number or 0, m.path.name),
        )
        assigned: List[Tuple[_Member, Optional[int]]] = [(m, None) for m in plain]
        seq = 1
        for m in numbered:
            assigned.append((m, seq))
            seq += 1
        if not plain and not numbered and len(dups) == 1:
            # A lone "Copy of X" with no X around: it simply becomes X.
            assigned.append((dups[0], None))
        else:
            for m in dups:
                assigned.append((m, seq))
                seq += 1
        width = 2 if seq - 1 >= 10 else 1
        for m, n in assigned:
            yield m, m.cleaned if n is None else f"{m.cleaned} [{n:0{width}d}]"


def plan_renames(root: Path) -> List[Rename]:
    plans: List[Rename] = []
    groups: Dict[Tuple[Path, str, str], List[_Member]] = {}
    movs: List[_Member] = []
    sidecars: List[Path] = []

    for path in _walk_files(root):
        ext = path.suffix.lower()
        if ext in SIDECAR_EXTS:
            sidecars.append(path)
            continue
        if ext not in RENAME_EXTS:
            continue
        stem = path.name[: -len(path.suffix)] if path.suffix else path.name
        if GUID_RE.match(stem):
            folder = path.parent.name or "media"
            plans.append(Rename(path, path.with_name(f"{folder} [{stem.lower()}]{ext}")))
            continue
        parsed = parse_stem(stem)
        cleaned = clean_base(parsed.base)
        if not cleaned:
            continue
        member = _Member(path, stem, parsed, cleaned, ext)
        if ext == ".mov":
            movs.append(member)  # may mirror a Live Photo still; decided below
        else:
            groups.setdefault((path.parent, cleaned.casefold(), ext), []).append(member)

    final_stems: Dict[Tuple[Path, str], str] = {}
    for member, final_stem in _assign(groups):
        final_stems[(member.path.parent, member.stem)] = final_stem
        plans.append(Rename(member.path, member.path.with_name(final_stem + member.ext)))

    # A .mov sharing dir+stem with a still is (likely) a Live Photo half:
    # mirror the still's rename so the pairing convention survives.
    mov_groups: Dict[Tuple[Path, str, str], List[_Member]] = {}
    for member in movs:
        mirrored = final_stems.get((member.path.parent, member.stem))
        if mirrored is not None:
            plans.append(Rename(member.path, member.path.with_name(mirrored + member.ext)))
        else:
            mov_groups.setdefault(
                (member.path.parent, member.cleaned.casefold(), member.ext), []
            ).append(member)
    for member, final_stem in _assign(mov_groups):
        final_stems[(member.path.parent, member.stem)] = final_stem
        plans.append(Rename(member.path, member.path.with_name(final_stem + member.ext)))

    for sidecar in sidecars:
        stem = sidecar.name[: -len(sidecar.suffix)]
        new_stem = final_stems.get((sidecar.parent, stem))
        if new_stem is not None:
            plans.append(
                Rename(sidecar, sidecar.with_name(new_stem + sidecar.suffix.lower()))
            )

    return [p for p in plans if p.dst.name != p.src.name]


def apply_renames(plans: List[Rename], root: Path, dry_run: bool) -> Tuple[int, int]:
    """Execute the plan without ever overwriting. Returns (renamed, skipped).

    Renames whose target is currently occupied by another file that is itself
    about to move (e.g. closing a numbering gap) wait for that move; anything
    still blocked when no progress can be made is skipped and logged."""
    renamed = skipped = 0
    if dry_run:
        for p in plans:
            log.info("[dry]  %s would rename -> %s", p.src.relative_to(root), p.dst.name)
        return len(plans), 0

    pending = list(plans)
    sources = {p.src for p in pending}
    while pending:
        deferred: List[Rename] = []
        progress = False
        for p in pending:
            occupied = p.dst.exists()
            if occupied:
                try:
                    if p.src.samefile(p.dst):
                        occupied = False  # case-only rename on APFS
                except OSError:
                    pass
            if occupied and p.dst in sources:
                deferred.append(p)  # target will move; try again next round
                continue
            if occupied:
                log.info("[skip] %s target already exists: %s", p.src.relative_to(root), p.dst.name)
                skipped += 1
                sources.discard(p.src)
                progress = True
                continue
            try:
                os.rename(p.src, p.dst)
            except OSError as exc:
                log.error("[FAIL] %s rename failed: %s", p.src.relative_to(root), exc)
                skipped += 1
            else:
                log.info("[ren]  %s -> %s", p.src.relative_to(root), p.dst.name)
                renamed += 1
            sources.discard(p.src)
            progress = True
        if not progress:
            for p in deferred:
                log.info("[skip] %s target occupied: %s", p.src.relative_to(root), p.dst.name)
                skipped += 1
            break
        pending = deferred
    return renamed, skipped
