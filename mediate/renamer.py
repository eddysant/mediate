"""Filename standardization: cleanup, numbering, GUID/random-token replacement.

Runs as a separate phase from conversion (`--rename` / `--rename-only`).
Rules, in order of application per file:

- GUID stems — and random-looking tokens like "ue73up" (letter/digit soup,
  no separators) — become "<folder name> [<token>]": the token says nothing,
  the folder usually says everything.
- "Copy of X" / "X - copy" / "X copy 2" markers are folded into numbering.
- Trailing numbering is parsed from "(N)", "[N]", "[site N]" or a
  dash-separated "-N" ("Wren Tally - 2", "Tilly-Marsh-001"). A bare
  space-number ("Terminator 2") is NOT numbering. Dash-numbers require a
  non-digit before the dash so dates ("2023-01-05") survive.
- A website token in the stem ("Nova-Quinn-Example.com-4") moves into the
  bracket tag: "Nova Quinn [Example.com 1]".
- Survivors are renumbered per (directory, base, site, extension) series:
  always compacted to start at 1, gaps closed, zero-padded to two digits
  once the series reaches double digits, re-emitted as " [N]"/" [site N]".
  When two or more *unnumbered* files clean to the same base they would all
  target one path, so all but one are pulled into the same series; whichever
  already carries the final name keeps it, so re-runs do not churn.
- The base gets cleaned: NFC-normalized, underscores/dots to spaces, dashes
  to spaces when a letter is adjacent (digit-dash-digit survives: dates),
  whitespace collapsed, lowercase words title-cased (small words like
  "of"/"van" stay lower unless leading). Camera counters (IMG_1234,
  PXL_20230101_123456) and screenshot/WhatsApp names are left verbatim.
- Extensions are lowercased. Stems ending in an unrecognized "[...]" tag
  (e.g. a previously emitted GUID tag) are left alone: idempotence.
- `--date-prefix` prepends the capture date ("2019-06-01 ") from EXIF
  (exiftool if installed), video creation_time (ffprobe), or file mtime.

Safety: a rename never overwrites; collisions are logged skips. Live Photo
.mov halves mirror their still's rename, .aae/.xmp sidecars follow their
media file. Every applied batch is recorded in .mediate-renames.json at the
library root; `--undo-renames` reverses the most recent batch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .exiftool import run_exiftool
from .scanner import BUNDLE_EXTS, GIF_EXTS, HEIC_EXTS, MP4_EXTS, PHOTO_EXTS, VIDEO_EXTS

log = logging.getLogger("mediate")

RENAME_EXTS = PHOTO_EXTS | HEIC_EXTS | GIF_EXTS | VIDEO_EXTS | MP4_EXTS | {".webp"}
SIDECAR_EXTS = {".aae", ".xmp"}
MANIFEST_NAME = ".mediate-renames.json"

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$|^[0-9a-fA-F]{32}$"
)
# Already-standardized tags this tool emits: "[N]" / "[site N]" / "[site]".
TAG_RE = re.compile(r"^(?P<base>.*?)[ ]*\[(?:(?P<site>[^\]\s]+)[ ]+)?(?P<n>\d+)\]$")
TAG_SITE_RE = re.compile(r"^(?P<base>.*?)[ ]*\[(?P<site>[^\]\s]+\.[A-Za-z]{2,})\]$")
BRACKET_TAIL_RE = re.compile(r"\[[^\]]*\]$")
NUM_PAREN_RE = re.compile(r"^(?P<base>.*?)[ _]*\((?P<n>\d+)\)$")
# "(1999)" in a filename is a release year, not Finder's duplicate counter.
# Reading it as a counter renamed "The Matrix (1999)" to "The Matrix [1]" and
# destroyed the year, so this range is excluded from numbering.
YEAR_RANGE = range(1900, 2100)
# Dash-number needs a non-digit, non-space char before the dash, so date
# stems like "2023-01-05" don't lose their day.
NUM_DASH_RE = re.compile(r"^(?P<base>.*?[^\s\d])[ ]*[-–][ ]*(?P<n>\d+)$")
COPY_OF_RE = re.compile(r"^copy of[ _]+(?P<base>.+)$", re.IGNORECASE)
COPY_PAREN_RE = re.compile(r"^(?P<base>.*?)[ _-]*\(\s*copy\s*(?P<n>\d+)?\s*\)$", re.IGNORECASE)
COPY_TAIL_RE = re.compile(r"^(?P<base>.*?)[ _-]+copy(?:[ _-]*(?P<n>\d+))?$", re.IGNORECASE)
# Domain labels here exclude dashes on purpose: in filenames a dash is far
# more likely a separator ("Nova-Quinn-Example.com") than part of a domain.
SITE_RE = re.compile(
    r"(?:(?<=^)|(?<=[-_.\s]))"
    r"(?P<site>(?:[A-Za-z0-9]+\.)+"
    r"(?:com|net|org|io|co|tv|me|cc|xyz|info|biz|site|online|club))"
    r"(?=$|[-_\s])",
    re.IGNORECASE,
)
# Camera counters: nothing human-readable to fix, leave the stem verbatim.
PROTECTED_RE = re.compile(
    r"^(?:IMG|DSC[NF]?|MVI|VID|PXL|GOPR|PANO|DJI)[-_]?E?\d+(?:[-_]\d+)*$", re.IGNORECASE
)
# Names whose dots/format are meaningful (timestamps): skip word cleanup.
NO_CLEAN_RE = re.compile(r"^(?:screen ?shot|screenshot|whatsapp)", re.IGNORECASE)
DATE_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

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
    site: Optional[str] = None
    opaque: bool = False  # ends in a tag we don't understand: leave alone


@dataclass
class Rename:
    src: Path
    dst: Path


def looks_random(stem: str) -> bool:
    """Heuristic for meaningless letter/digit soup like "ue73up": a single
    separator-less token with enough of both and enough letter<->digit
    flips. "photo2023" (one flip) and "4kvideo" (one digit) stay names."""
    if not re.fullmatch(r"[A-Za-z0-9]{6,}", stem) or PROTECTED_RE.match(stem):
        return False
    digits = sum(c.isdigit() for c in stem)
    letters = sum(c.isalpha() for c in stem)
    if digits < 2 or letters < 2:
        return False
    flips = sum(1 for a, b in zip(stem, stem[1:]) if a.isdigit() != b.isdigit())
    return flips >= 2


def parse_stem(stem: str) -> ParsedName:
    dup = False
    # Finder produces "Copy of Copy of X" for a copy of a copy, so strip
    # every leading marker rather than only the outermost one.
    while True:
        m = COPY_OF_RE.match(stem)
        if not m:
            break
        stem = m.group("base")
        dup = True
    number: Optional[int] = None
    site: Optional[str] = None
    m = TAG_RE.match(stem)
    if m:
        number = int(m.group("n"))
        site = m.group("site")
        stem = m.group("base")
    else:
        m = TAG_SITE_RE.match(stem)
        if m:
            site = m.group("site")
            stem = m.group("base")
        elif BRACKET_TAIL_RE.search(stem):
            return ParsedName(stem, None, dup, None, opaque=True)
        else:
            for pattern in (NUM_PAREN_RE, NUM_DASH_RE):
                m = pattern.match(stem)
                if m:
                    candidate = int(m.group("n"))
                    if pattern is NUM_PAREN_RE and candidate in YEAR_RANGE:
                        break  # a year: leave it in the name
                    number = candidate
                    stem = m.group("base")
                    break
            if number is None and not dup:
                for pattern in (COPY_PAREN_RE, COPY_TAIL_RE):
                    m = pattern.match(stem)
                    if m:
                        dup = True
                        number = int(m.group("n")) if m.group("n") else None
                        stem = m.group("base")
                        break
    if site is None:
        m = SITE_RE.search(stem)
        if m:
            site = m.group("site")
            stem = stem[: m.start("site")] + stem[m.end("site"):]
    return ParsedName(stem, number, dup, site)


# A dot directly after a *single* letter belongs to an initialism
# ("R.E.M.", "e.e. cummings"). A dot after a longer run is a separator, so
# "Mr. Smith" and "holiday.photo" still clean up.
INITIAL_DOT_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z])\.")
# Digits either side of a dot are a time or a version, never a separator.
# Only consulted for date-stamped names, where "12.30.45" is a clock.
DIGIT_DOT_RE = re.compile(r"(?<=\d)\.(?=\d)")
# A word that is nothing but single letters and dots is an initialism.
INITIALISM_RE = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]?$")
# Invisible formatting that is almost always scrape residue. U+200C/U+200D
# (the zero-width non-joiner and joiner) are deliberately NOT here: they join
# visible glyphs in Indic scripts and in emoji sequences, so stripping them
# would silently split a family emoji into three people.
INVISIBLE_RE = re.compile("[\u200b\u200e\u200f\u202a-\u202e\u2060\ufeff]")
_KEEP_DOT = "\x00"


def clean_base(base: str) -> str:
    base = unicodedata.normalize("NFC", base)
    base = INVISIBLE_RE.sub("", base)
    if PROTECTED_RE.match(base) or NO_CLEAN_RE.match(base):
        return base.strip()
    # A date-stamped export carries a clock in its dots, so "12.30.45" must
    # not become "12 30 45" — but the words around it still get tidied.
    is_dated = bool(DATE_START_RE.match(base))
    base = INITIAL_DOT_RE.sub(lambda m: m.group(1) + _KEEP_DOT, base)
    if is_dated:
        base = DIGIT_DOT_RE.sub(_KEEP_DOT, base)
    base = re.sub(r"[_.]+", " ", base)
    # Dashes become spaces when a letter is adjacent; digit-dash-digit
    # (dates, ranges) survives.
    base = re.sub(r"(?<=[A-Za-z])[-–]|[-–](?=[A-Za-z])", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" -–_.")
    base = base.replace(_KEEP_DOT, ".")
    words = base.split(" ")
    out = []
    for i, word in enumerate(words):
        if INITIALISM_RE.match(word):
            out.append(word.upper())  # "e.e." -> "E.E.", "R.E.M." unchanged
        elif word.islower():
            if i > 0 and word in SMALL_WORDS:
                out.append(word)
            else:
                out.append(word[:1].upper() + word[1:])
        else:
            out.append(word)  # mixed/upper case is intentional: leave it
    return " ".join(out)


def media_date(path: Path) -> str:
    """Capture date as YYYY-MM-DD: EXIF via exiftool when installed, video
    creation_time via ffprobe, else the file's mtime."""
    out = run_exiftool(["-s3", "-d", "%Y-%m-%d", "-DateTimeOriginal", "-CreateDate", str(path)])
    if out:
        for line in out.splitlines():
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line.strip()):
                return line.strip()
    if path.suffix.lower() in (VIDEO_EXTS | MP4_EXTS) and shutil.which("ffprobe"):
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        stamp = proc.stdout.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", stamp):
            return stamp[:10]
    return date.fromtimestamp(path.stat().st_mtime).isoformat()


# Practical per-component limit on APFS/HFS+/ext4/NTFS.
NAME_MAX_BYTES = 255
TAG_TAIL_RE = re.compile(r" \[[^\]]*\]$")


def fit_within_name_max(stem: str, ext: str) -> str:
    """Trim a stem's middle so ``stem + ext`` fits the filesystem limit.

    The trailing " [N]" tag is what keeps a series distinct and a leading
    --date-prefix is the whole point of that flag, so both are kept and the
    descriptive middle is shortened. Trimming is done on encoded bytes (the
    limit is bytes, not characters) without splitting a character.
    """
    if len((stem + ext).encode("utf-8")) <= NAME_MAX_BYTES:
        return stem
    match = TAG_TAIL_RE.search(stem)
    tag = match.group(0) if match else ""
    head = stem[: match.start()] if match else stem
    budget = NAME_MAX_BYTES - len((tag + ext).encode("utf-8"))
    if budget <= 0:
        return stem  # tag and extension alone overflow; let the rename fail
    head = head.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip()
    return head + tag if head else stem


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
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                # The converter refuses symlinked media outright; renaming it
                # here would move a link whose target may live anywhere.
                log.debug("[skip] %s: symbolic link, not renamed", path)
                continue
            yield path


def _tag(site: Optional[str], n: Optional[int], width: int) -> str:
    parts = []
    if site:
        parts.append(site)
    if n is not None:
        parts.append(f"{n:0{width}d}")
    return f" [{' '.join(parts)}]" if parts else ""


def _assign(groups: Dict[Tuple, List[_Member]]) -> Iterator[Tuple[_Member, str]]:
    """Yield (member, final_stem): numbering compacted to start at 1, gaps
    closed, zero-padded once the series reaches double digits."""
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
        assigned: List[Tuple[_Member, Optional[int]]] = []
        seq = 1
        if len(plain) > 1:
            # Several distinct files clean to one name ("a b", "a_b", "a-b").
            # Left unnumbered they would all target the same path and
            # apply_renames — which never overwrites — would skip all but one,
            # reading as "it did nothing". Number the extras instead. The
            # member that already carries the final name keeps it, so a
            # library standardized on an earlier run does not churn.
            plain = sorted(plain, key=lambda m: m.path.name)
            keeper = next(
                (m for m in plain
                 if m.stem == m.cleaned + _tag(m.parsed.site, None, 1)),
                plain[0],
            )
            assigned.append((keeper, None))
            for m in plain:
                if m is not keeper:
                    assigned.append((m, seq))
                    seq += 1
        else:
            assigned.extend((m, None) for m in plain)
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
        # Width is the digit count of the largest number in the series, not a
        # flat 2: a 105-member series padded to 2 puts "[100]" lexically
        # between "[09]" and "[10]", which is exactly what padding exists to
        # prevent.
        width = len(str(max(seq - 1, 1)))
        for m, n in assigned:
            yield m, m.cleaned + _tag(m.parsed.site, n, width)


def plan_renames(root: Path, date_prefix: bool = False) -> List[Rename]:
    plans: List[Rename] = []
    groups: Dict[Tuple, List[_Member]] = {}
    movs: List[_Member] = []
    sidecars: List[Path] = []

    def finalize(path: Path, new_stem: str, ext: str) -> str:
        if date_prefix and not DATE_START_RE.match(new_stem):
            new_stem = f"{media_date(path)} {new_stem}"
        new_stem = fit_within_name_max(new_stem, ext)
        plans.append(Rename(path, path.with_name(new_stem + ext)))
        return new_stem  # what actually got emitted (mirrors/sidecars follow it)

    for path in _walk_files(root):
        ext = path.suffix.lower()
        if ext in SIDECAR_EXTS:
            sidecars.append(path)
            continue
        if ext not in RENAME_EXTS:
            continue
        stem = path.name[: -len(path.suffix)] if path.suffix else path.name
        if GUID_RE.match(stem) or looks_random(stem):
            # The folder name goes through the same cleanup the file would
            # have had; otherwise --rename-folders tidies the directory to
            # "My Holiday Photos" while the files inside still read
            # "my_holiday_photos [<guid>]".
            raw_folder = path.parent.name
            folder = clean_base(raw_folder) or raw_folder or "media"
            finalize(path, f"{folder} [{stem.lower() if GUID_RE.match(stem) else stem}]", ext)
            continue
        parsed = parse_stem(stem)
        if parsed.opaque:
            if path.suffix != ext:  # already standardized: extension case only
                plans.append(Rename(path, path.with_name(stem + ext)))
            continue
        cleaned = clean_base(parsed.base)
        if not cleaned:
            continue
        member = _Member(path, stem, parsed, cleaned, ext)
        if ext == ".mov":
            movs.append(member)  # may mirror a Live Photo still; decided below
        else:
            key = (path.parent, cleaned.casefold(), (parsed.site or "").casefold(), ext)
            groups.setdefault(key, []).append(member)

    final_stems: Dict[Tuple[Path, str], str] = {}
    for member, final_stem in _assign(groups):
        final_stems[(member.path.parent, member.stem)] = finalize(member.path, final_stem, member.ext)

    # A .mov sharing dir+stem with a still is (likely) a Live Photo half:
    # mirror the still's rename so the pairing convention survives.
    mov_groups: Dict[Tuple, List[_Member]] = {}
    for member in movs:
        mirrored = final_stems.get((member.path.parent, member.stem))
        if mirrored is not None:
            plans.append(Rename(member.path, member.path.with_name(mirrored + member.ext)))
        else:
            key = (member.path.parent, member.cleaned.casefold(),
                   (member.parsed.site or "").casefold(), member.ext)
            mov_groups.setdefault(key, []).append(member)
    for member, final_stem in _assign(mov_groups):
        final_stems[(member.path.parent, member.stem)] = finalize(member.path, final_stem, member.ext)

    for sidecar in sidecars:
        stem = sidecar.name[: -len(sidecar.suffix)]
        new_stem = final_stems.get((sidecar.parent, stem))
        if new_stem is not None:
            plans.append(
                Rename(sidecar, sidecar.with_name(new_stem + sidecar.suffix.lower()))
            )

    return [p for p in plans if p.dst.name != p.src.name]


def plan_folder_renames(root: Path) -> List[Rename]:
    """Clean directory names with the same base rules, deepest first so
    each rename's path is unaffected by its ancestors' pending renames."""
    dirs: List[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        # Symlinked directories are excluded for the same reason symlinked
        # files are: renaming one moves a pointer whose target may live
        # anywhere, including outside the library.
        dirnames[:] = [
            d for d in sorted(dirnames)
            if not d.startswith(".")
            and Path(d).suffix.lower() not in BUNDLE_EXTS
            and not (Path(dirpath) / d).is_symlink()
        ]
        dirs.extend(Path(dirpath) / d for d in dirnames)
    plans = []
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        if BRACKET_TAIL_RE.search(d.name):
            continue
        cleaned = clean_base(d.name)
        if cleaned and cleaned != d.name:
            plans.append(Rename(d, d.with_name(cleaned)))
    return plans


def apply_renames(plans: List[Rename], root: Path, dry_run: bool) -> Tuple[int, int, List[Rename]]:
    """Execute the plan without ever overwriting. Returns
    (renamed, skipped, applied-in-order) — the applied list feeds the manifest.

    Renames whose target is currently occupied by another file that is itself
    about to move (e.g. closing a numbering gap) wait for that move; anything
    still blocked when no progress can be made is skipped and logged."""
    renamed = skipped = 0
    applied: List[Rename] = []
    if dry_run:
        for p in plans:
            log.info("[dry]  %s would rename -> %s", p.src.relative_to(root), p.dst.name)
        return len(plans), 0, []

    pending = list(plans)
    sources = {p.src for p in pending}
    while pending:
        deferred: List[Rename] = []
        progress = False
        # "Some other pending source lies inside p.src" is exactly "p.src is a
        # strict ancestor of a pending source", so the ancestors are collected
        # once per round and tested by lookup. Scanning every source for every
        # plan instead is quadratic: measured at ~12s for 1,600 renames and
        # extrapolating to over three hours for a 50k-file library, before a
        # single file is moved.
        source_ancestors = {
            ancestor for source in sources for ancestor in source.parents
        }
        for p in pending:
            if p.src in source_ancestors:
                deferred.append(p)
                continue
            
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
                applied.append(p)
            sources.discard(p.src)
            progress = True
        if not progress:
            for p in deferred:
                log.info("[skip] %s target occupied: %s", p.src.relative_to(root), p.dst.name)
                skipped += 1
            break
        pending = deferred
    return renamed, skipped, applied


def write_plan(root: Path, plans: List[Rename], path: Path) -> None:
    """Serialize a rename plan for review/editing before applying."""
    path.write_text(
        json.dumps(
            {
                "root": str(root),
                "renames": [
                    {"from": str(p.src.relative_to(root)), "to": str(p.dst.relative_to(root))}
                    for p in plans
                ],
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def load_plan(root: Path, path: Path) -> List[Rename]:
    """Load an (possibly hand-edited) plan. Every path must be relative and
    stay inside root — a plan file is user input, not trusted state."""
    data = json.loads(path.read_text(encoding="utf-8"))
    plans: List[Rename] = []
    for entry in data.get("renames", []):
        src_rel, dst_rel = Path(entry["from"]), Path(entry["to"])
        for rel in (src_rel, dst_rel):
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"plan entry escapes the library root: {rel}")
        plans.append(Rename(root / src_rel, root / dst_rel))
    return plans


def _manifest_path(root: Path) -> Path:
    return root / MANIFEST_NAME


def _write_manifest(path: Path, data: dict) -> None:
    """Replace the manifest atomically.

    This file is what makes renames reversible, so a crash or a full disk
    mid-write must not be able to truncate it — same temp-file + os.replace
    protocol the journal, transactions, and the probe cache use.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def record_batch(root: Path, applied: List[Rename]) -> None:
    if not applied:
        return
    path = _manifest_path(root)
    data = {"batches": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    data.setdefault("batches", []).append({
        "time": datetime.now().isoformat(timespec="seconds"),
        "renames": [
            {"from": str(p.src.relative_to(root)), "to": str(p.dst.relative_to(root))}
            for p in applied
        ],
    })
    _write_manifest(path, data)


def undo_last_batch(root: Path, dry_run: bool) -> int:
    """Reverse the most recent rename batch. Returns count of restores."""
    path = _manifest_path(root)
    if not path.exists():
        log.info("no rename manifest at %s — nothing to undo", path)
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("cannot read rename manifest: %s", exc)
        return 0
    batches = data.get("batches", [])
    if not batches:
        log.info("rename manifest is empty — nothing to undo")
        return 0
    batch = batches[-1]
    log.info("undoing rename batch from %s (%d rename(s))", batch.get("time"), len(batch["renames"]))
    restored = 0
    failed = 0
    for entry in reversed(batch["renames"]):
        original = root / entry["from"]
        current = root / entry["to"]
        if dry_run:
            log.info("[dry]  would restore %s -> %s", entry["to"], original.name)
            restored += 1
            continue
        if not current.exists():
            log.info("[skip] %s no longer exists", entry["to"])
            continue
        occupied = original.exists()
        if occupied:
            try:
                if current.samefile(original):
                    occupied = False
            except OSError:
                pass
        if occupied:
            log.info("[skip] cannot restore %s: %s already exists", entry["to"], original.name)
            continue
        try:
            os.rename(current, original)
        except OSError as exc:
            # One unrestorable entry must not abort the rest of the batch
            # with a traceback and leave it half reversed.
            log.error("[FAIL] cannot restore %s: %s", entry["to"], exc)
            failed += 1
            continue
        log.info("[ren]  %s -> %s (restored)", entry["to"], original.name)
        restored += 1
    if not dry_run:
        if failed:
            # Keep the batch recorded so the remainder can be retried once
            # the cause is cleared; dropping it would strand those files.
            log.error(
                "%d entr(y/ies) could not be restored; keeping the batch in %s "
                "so --undo-renames can be retried",
                failed, path.name,
            )
        else:
            batches.pop()
            _write_manifest(path, data)
    return restored
