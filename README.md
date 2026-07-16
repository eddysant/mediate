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
- `--workers N` — concurrent conversions (default 2; ffmpeg is already
  multithreaded, so higher values mainly help photo-heavy libraries).
- `--log-file PATH` — detailed log location (default: `conversion.log` inside
  the target directory; console shows one line per file, the log adds
  timestamps and full converter stderr on failures).

Exit code is `0` when nothing failed, `1` if any file failed validation,
`2` for usage errors (bad directory, missing tools).

## Safety protocol

An original is disposed of **only** after all of these pass:

1. Converter exit code is `0`.
2. The output file exists.
3. The output file is larger than 0 bytes.
4. (Videos) `ffmpeg -v error -i out.mp4 -f null -` exits `0` **and** prints
   nothing to stderr (full-decode integrity check).

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

- MKVs with text subtitle tracks can fail to mux into MP4; those files fail
  validation and the originals are kept (visible in the log).
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
