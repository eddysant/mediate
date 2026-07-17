# mediate

Settles your media library's format disputes: a terminal app that recursively
standardizes a media library into compatible, space-efficient formats — and
only parts with an original after the converted file passes a strict
validation checklist (and even then, it goes to the Trash, not oblivion).

| Input | Output | Tool |
|---|---|---|
| JPEG / PNG / TIFF | Lossless WebP (`-metadata all` preserves EXIF/ICC/dates) | `cwebp` |
| HEIC / HEIF (opt-in, macOS) | Lossless WebP via a sips PNG intermediate, EXIF preserved | `sips` + `cwebp` |
| MOV / MKV / AVI / WMV / WebM / … | MP4 (h264 `-crf 18 -preset slow`, AAC 256k, `yuv420p`) | `ffmpeg` |
| Animated GIF | MP4 (`faststart`, even-dimension scale filter) | `ffmpeg` |

Skipped automatically:

- `.webp`, hidden files/directories (`.DS_Store` etc.), static GIFs.
- MP4s already h264/yuv420p/AAC (checked with `ffprobe`).
- **HEVC MP4s** — smaller than h264 and Apple-native; re-encoding them to
  h264 only grows the file (`--reencode-hevc` to force).
- **HEIC/HEIF** — already space-efficient (`--convert-heic` to convert).
- **Live Photo pairs** — a `.mov` next to a same-named still (both halves are
  left alone, since converting either breaks the pairing in Apple Photos;
  `--convert-live-photos` to force).
- **Application bundles** — `*.photoslibrary`, `*.app`, `*.fcpbundle`, etc.
  are never traversed. Converting files inside an Apple Photos library would
  corrupt it, so this cannot be overridden.

## Requirements

- Python ≥ 3.9 (no Python dependencies)
- `cwebp`, `ffmpeg`, `ffprobe` on PATH: `brew install webp ffmpeg`
- HEIC conversion additionally needs macOS (`sips` is built in)

## Usage

```sh
# from the project root, no install needed
python3 -m mediate ~/Pictures/Library --dry-run   # preview only
python3 -m mediate ~/Pictures/Library             # convert; originals -> Trash

# or install the `mediate` command
pip install .
mediate ~/Pictures/Library --dry-run
```

A sensible full-strength invocation for a real library:

```sh
mediate ~/Pictures/Library --only-if-smaller --convert-heic
```

## Name standardization (`--rename` / `--rename-only`)

Besides formats, mediate can settle *naming* disputes. `--rename` runs after
conversion (so fresh `.webp`/`.mp4` outputs are covered too); `--rename-only`
skips conversion entirely. Both respect `--dry-run`.

- Cleanup: underscores/dots/dashes to spaces (digit-dash-digit survives, so
  `2023-01-05` keeps its shape), whitespace collapsed, lowercase words
  title-cased (small words like "of" stay lower unless leading; existing
  capitalization such as `USA` or `McDonald` is respected), extensions
  lowercased, Unicode NFC-normalized.
- Numbering: `photo (1)` → `Photo [1]`, `Bonnie Wright - 2` →
  `Bonnie Wright [1]`, `Cora-Keegan-001` → `Cora Keegan [1]`; `Copy of X` /
  `X - copy` / `X copy 2` markers join the numbering. Every series is
  compacted to start at 1 with gaps closed (`1,2,4` → `1,2,3`); once a series
  reaches double digits, single digits are zero-padded (`[01]`…`[10]`) so
  lexical order equals numeric order. Series are per directory + base name +
  site + extension — different file types count independently. A bare
  space-number (`Terminator 2`) is *not* treated as numbering; only
  `(N)`/`[N]`/dash-`N` forms are.
- Websites in the name move into the tag:
  `Bella-Hadid-TheSpot.com-4` → `Bella Hadid [TheSpot.com 1]` (each site is
  its own numbering series).
- GUID names — and random letter/digit tokens like `ue73up` — take their
  folder's name: `Bella/ue73up.jpg` → `Bella [ue73up].jpg`,
  `Vacation 2019/550e8400-….jpg` → `Vacation 2019 [550e8400-…].jpg`.
- `--date-prefix` prepends the capture date: `2019-06-01 Eddy Sant [01].webp`
  (EXIF via exiftool when installed, video creation_time, else file mtime).
- `--rename-folders` cleans directory names with the same rules.
- Protected: camera counters (`IMG_1234`, `DSC_0001`, `PXL_…`) and
  screenshot/WhatsApp names are left verbatim — nothing human to fix, and
  their dots and digits are data. Names already carrying an unrecognized
  `[…]` tag are left alone, so re-runs are idempotent.
- Live Photo `.mov` halves mirror their still's rename, and `.aae`/`.xmp`
  sidecars follow their media file, so pairings survive.
- A rename never overwrites: collisions (e.g. `eddy_sant.jpg` +
  `eddy.sant.jpg`) keep the loser's old name and log it.
- **Every applied batch is recorded** in `.mediate-renames.json` at the
  library root; `mediate DIR --undo-renames` reverses the most recent batch
  (repeatable, batch by batch).

```sh
mediate ~/Pictures/Library --rename-only --dry-run   # preview the renames
mediate ~/Pictures/Library --undo-renames            # regret the last batch

# review-then-commit: write the plan, edit the JSON, apply it
mediate ~/Pictures/Library --rename-only --plan-file plan.json
mediate ~/Pictures/Library --apply-plan plan.json
```

Options:

- `--dry-run` — traverse and print what would happen; nothing is written or deleted.
- `--keep-originals` — convert but never touch inputs (a non-standard `foo.mp4`
  becomes `foo.standardized.mp4` since the original keeps its name).
- `--only-if-smaller` — discard the conversion and keep the original unless the
  output is actually smaller. Recommended: lossless WebP is frequently *bigger*
  than a camera JPEG or HEIC, because it must reproduce the source's compression
  noise exactly.
- `--reencode-hevc` — convert HEVC MP4s to h264 for non-Apple compatibility,
  accepting the size increase.
- `--convert-heic` — convert HEIC/HEIF to lossless WebP (macOS only).
- `--convert-live-photos` — convert Live Photo pairs anyway.
- `--graveyard DIR` — move originals to DIR (mirroring the folder structure)
  instead of the Trash.
- `--hard-delete` — permanently delete originals (the pre-Trash behavior).
- `--plan-file PATH` / `--apply-plan PATH` — write the proposed renames as
  editable JSON instead of applying, then apply the (possibly hand-edited)
  plan later. Applied plans are recorded for `--undo-renames` like any batch.
- `--workers N` — concurrent conversions (default 2; ffmpeg is already
  multithreaded, so higher values mainly help photo-heavy libraries).
- `--log-file PATH` — detailed log location (default: `conversion.log` inside
  the target directory; console shows one line per file, the log adds
  timestamps and full converter stderr on failures).

Exit code is `0` when nothing failed, `1` if any file failed validation,
`2` for usage errors (bad directory, missing tools).

## Config file

Default flags live in `~/.config/mediate/config` (or `$MEDIATE_CONFIG`) —
one flag per line, `#` comments:

```
# my defaults
--only-if-smaller
--convert-heic
--workers 4
```

They are prepended to every invocation; `--no-config` ignores the file for
one run.

## Safety protocol

An original is disposed of **only** after all of these pass:

1. Converter exit code is `0`.
2. The output file exists.
3. The output file is larger than 0 bytes.
4. (Videos) `ffmpeg -v error -i out.mp4 -f null -` exits `0` **and** prints
   nothing to stderr (full-decode integrity check).
5. Metadata survived: a photo whose source has an EXIF capture date must
   carry the same date in the WebP (exiftool when installed, structural
   EXIF-block check otherwise) — this is what catches e.g. cwebp silently
   dropping TIFF metadata. A video's duration must match the source within
   1s/2%, catching truncated encodes that still decode cleanly.

On any failure the partial output is removed, the original is untouched, and
the reason is logged.

Additional safeguards beyond the checklist:

- **Originals go to the Trash by default** (per-volume `.Trashes` on macOS so
  external drives don't get copied across volumes; freedesktop trash with
  `.trashinfo` on Linux). Video re-encoding is lossy — once an original is
  hard-deleted, that quality is gone forever, so recoverability is the default.
  Use `--graveyard DIR` for a reviewable folder or `--hard-delete` to opt out.
- Conversions write to a hidden temp name (`.name.<rand>.part.ext`) in the
  same directory and are renamed into place only after validation, so a crash
  never leaves a half-written file wearing the final name.
- If the target name already exists (e.g. `a.jpg` and `a.png` both map to
  `a.webp`), the file is skipped and logged rather than overwritten.
- The output inherits the original's modification time **and, on macOS, its
  creation date** (via `setattrlist`), so both EXIF-based and Finder-based
  date sorting keep working.

## Notes

- `.aae`/`.xmp` sidecars describe their original file, so when an original is
  disposed of after conversion, its sidecars travel with it.
- ffprobe results are cached (`~/Library/Caches/mediate` / `$XDG_CACHE_HOME`),
  keyed by path+mtime+size, so re-running over a large already-standardized
  library is near-instant.
- Videos longer than a minute report live encode progress (25/50/75% marks
  via ffmpeg's `-progress` pipe); files over 100 MB additionally announce
  themselves up front.
- exiftool queries (metadata validation, `--date-prefix`, Live Photo
  verification) go through a persistent `-stay_open` daemon — one process
  for the whole run instead of one per file.
- Windows: the Recycle Bin is not supported — mediate requires
  `--graveyard DIR` or `--hard-delete` there.
- MKVs with text subtitle tracks can fail to mux into MP4; those files fail
  validation and the originals are kept (visible in the log).
- `.tif` inputs whose EXIF matters will fail the new metadata check (cwebp
  cannot carry TIFF metadata) and stay untouched — by design.
- Live Photo detection is by naming convention (same directory + stem,
  `.mov` beside a still); unrelated files that happen to share a name are
  skipped too — the log says why, and `--convert-live-photos` overrides.
- The HEIC pipeline uses a PNG intermediate deliberately: sips carries EXIF
  into PNG and cwebp extracts EXIF from PNG, whereas cwebp silently drops
  metadata from TIFF input.

## Tests

```sh
python3 -m unittest discover tests
```
