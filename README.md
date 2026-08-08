# mediate

Settles your media library's format disputes: a terminal app that recursively
standardizes a media library into compatible, space-efficient formats — and
only parts with an original after the converted file passes a strict
validation checklist (and even then, it goes to the Trash, not oblivion).

| Input | Output | Tool |
|---|---|---|
| JPEG / PNG / TIFF | Lossless WebP (`-metadata all` preserves EXIF/ICC/dates) | `cwebp` |
| HEIC / HEIF (opt-in, macOS) | Lossless WebP via a sips PNG intermediate, EXIF preserved | `sips` + `cwebp` |
| MOV / MKV / AVI / WMV / WebM / ASF / VOB / legacy video | MP4 (h264 `-crf 18 -preset slow`, AAC 256k, `yuv420p`) | `ffmpeg` |
| Animated GIF | MP4 (`faststart`, even-dimension scale filter) | `ffmpeg` |

Skipped automatically:

- `.webp`, hidden files/directories (`.DS_Store` etc.), static GIFs.
- MP4s already h264/8-bit 4:2:0/AAC (including FFmpeg's full-range
  `yuvj420p` alias, checked with `ffprobe`).
- **HEVC MP4s** — smaller than h264 and Apple-native; re-encoding them to
  h264 only grows the file (`--reencode-hevc` to force).
- **HEIC/HEIF** — already space-efficient (`--convert-heic` to convert).
- **Live Photo pairs** — a `.mov` and same-named still whose Apple
  `ContentIdentifier` metadata matches (both halves are left alone, since
  converting either breaks the pairing in Apple Photos;
  `--convert-live-photos` to force).
- Videos whose MP4 conversion would discard styled/bitmap subtitles,
  incompatible attachments, extra video angles, or arbitrary data streams.
  Simple text subtitles become `mov_text` and JPEG/PNG cover streams are
  retained automatically; `--allow-stream-removal` opts into dropping the rest.
- Video that would silently lose HDR/high-bit-depth, alpha, interlacing, or
  variable/unusual-frame-rate semantics during the standard 8-bit encode
  (`--allow-video-downgrade` is the explicit opt-in).
- **Application bundles** — `*.photoslibrary`, `*.app`, `*.fcpbundle`, etc.
  are never traversed. Converting files inside an Apple Photos library would
  corrupt it, so this cannot be overridden.

Legacy coverage includes QuickTime (`.mov`, `.qt`), Windows Media (`.asf`,
`.wmv`, `.dvr-ms`, `.wtv`), DVD/camcorder formats (`.vob`, `.vro`, `.mod`,
`.tod`, `.dv`), RealMedia, Ogg video, MPEG transport/elementary streams, MXF,
3GPP/3GPP2, Flash video, and common DivX/Xvid extensions. Every format still
goes through ffprobe preflight and the same strict validation pipeline.

## Requirements

- Python ≥ 3.9 (no Python dependencies)
- `cwebp`, `ffmpeg`, `ffprobe` on PATH: `brew install webp ffmpeg`
- `jpegtran` from jpeg-turbo is optional for direct installs and enables
  automatic lossless recovery of truncated JPEGs. The Homebrew mediate formula
  includes it: `brew install jpeg-turbo`.
- HEIC conversion additionally needs macOS (`sips` is built in)

Before conversion, mediate checks tool versions and performs a tiny h264/AAC
MP4 encode plus JSON probe. Missing encoders and mismatched FFmpeg builds fail
up front with an installation or upgrade hint.

## Install

```sh
brew install eddysant/tap/mediate     # installs ffmpeg + webp too
```

or grab the single-file `mediate.pyz` from the
[latest release](https://github.com/eddysant/mediate/releases/latest), or run
from a checkout with no install at all.

## Usage

```sh
python3 -m mediate ~/Pictures/Library --dry-run   # from the project root
mediate ~/Pictures/Library --dry-run              # brew / pip install .
mediate ~/Pictures/Library                        # convert; originals -> Trash
mediate ~/Pictures/Library --validate-existing    # optional health scan + repair
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
- Numbering: `photo (1)` → `Photo [1]`, `Wren Tally - 2` →
  `Wren Tally [1]`, `Tilly-Marsh-001` → `Tilly Marsh [1]`; `Copy of X` /
  `X - copy` / `X copy 2` markers join the numbering. Every series is
  compacted to start at 1 with gaps closed (`1,2,4` → `1,2,3`); once a series
  reaches double digits, single digits are zero-padded (`[01]`…`[10]`) so
  lexical order equals numeric order. Series are per directory + base name +
  site + extension — different file types count independently. A bare
  space-number (`Terminator 2`) is *not* treated as numbering; only
  `(N)`/`[N]`/dash-`N` forms are.
- Websites in the name move into the tag:
  `Nova-Quinn-Example.com-4` → `Nova Quinn [Example.com 1]` (each site is
  its own numbering series).
- GUID names — and random letter/digit tokens like `ue73up` — take their
  folder's name: `Nova/ue73up.jpg` → `Nova [ue73up].jpg`,
  `Vacation 2019/550e8400-….jpg` → `Vacation 2019 [550e8400-…].jpg`.
- `--date-prefix` prepends the capture date: `2019-06-01 Misty Vale [01].webp`
  (EXIF via exiftool when installed, video creation_time, else file mtime).
- `--rename-folders` cleans directory names with the same rules.
- Protected: camera counters (`IMG_1234`, `DSC_0001`, `PXL_…`) and
  screenshot/WhatsApp names are left verbatim — nothing human to fix, and
  their dots and digits are data. Names already carrying an unrecognized
  `[…]` tag are left alone, so re-runs are idempotent.
- Live Photo `.mov` halves mirror their still's rename, and `.aae`/`.xmp`
  sidecars follow their media file, so pairings survive.
- A rename never overwrites: collisions (e.g. `misty_vale.jpg` +
  `misty.vale.jpg`) keep the loser's old name and log it.
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
- `--allow-stream-removal` — allow MP4 conversion to discard incompatible
  subtitles, artwork, extra video tracks, and data/attachment streams.
  Compatible text subtitles and JPEG/PNG cover streams are always preserved.
- `--allow-video-downgrade` — explicitly permit 8-bit h264 conversion of
  HDR/high-bit-depth, alpha, interlaced, or variable/unusual-frame-rate video.
- `--validate-existing` — fully decode already-standardized MP4s and attempt
  repair when corruption is found. This potentially expensive library-health
  pass is off by default; newly converted outputs are always validated.
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
   dropping TIFF metadata.
6. A video's duration matches within 1s/2%, and its full stream inventory is
   checked: every audio track retains duration, language, title/commentary,
   dispositions, channel count/layout, sample rate, profile, and A/V start
   offset; chapters, rotation, colour, aspect ratio, field order, frame rate,
   compatible subtitles, artwork, and HDR side data are also verified.

On any failure the partial output is removed, the original is untouched, and
the reason is logged.

When cwebp/libjpeg reports a truncated JPEG, mediate first runs the source
through `jpegtran -copy all`. This rebuilds the JPEG structure while copying
compressed image coefficients and metadata without another lossy encode, then
retries the normal conversion and validation. The repair is performed on a
temporary file; the source remains untouched until the validated replacement
transaction commits.

Video containers that end prematurely are different: mediate can preserve the
decodable prefix, but it cannot reconstruct packets that are absent from the
file. If the encoded duration is shorter than the declared source duration,
the run reports the source as truncated and keeps it rather than presenting a
partial video as a complete repair.

By default, already-standardized MP4s are classified by stream format and
skipped without a full decode. `--validate-existing` enables the library-health
pass: mediate fully decodes every video and audio stream, caches the result
using filesystem identity plus sampled content, and repairs detected damage.
Repair is lossless-first (container/index/timestamp rebuild), then tolerant
re-encoding. Every repaired file still passes the complete checklist above.
Damaged HEVC remains opt-in through `--reencode-hevc`.

Additional safeguards beyond the checklist:

- **Originals go to the Trash by default** (per-volume `.Trashes` on macOS so
  external drives don't get copied across volumes; freedesktop trash with
  `.trashinfo` on Linux). Video re-encoding is lossy — once an original is
  hard-deleted, that quality is gone forever, so recoverability is the default.
  Use `--graveyard DIR` for a reviewable folder or `--hard-delete` to opt out.
- Conversions write to a hidden temp name (`.name.<rand>.part.ext`) and enter a
  durable two-phase replacement transaction only after validation. The
  original is staged locally, the output is installed atomically, and the
  recorded Trash/graveyard/delete policy runs last. On startup, interrupted
  transactions either restore the original or finish a proven installation.
- Video commands explicitly map the primary video plus **all** audio tracks
  and chapters. FFmpeg's implicit “best stream” selection is never trusted.
  A rotated video gets a short lossless remux after encoding to restore its
  display matrix before validation.
- Before work starts, the source must be a readable regular file with no
  symlink or hard-link ambiguity, its output directory must be writable, and
  conservative temporary free space must be available. Concurrent workers
  reserve that budget per filesystem, so they cannot all spend the same free
  bytes; work waits when another conversion holds the budget. Device/inode/
  size/mtime are checked again before disposal so a moving source is never
  replaced.
- FFmpeg, ffprobe, libx264, AAC, MP4 muxing, JSON probing, progress support,
  and rotation support are checked before media work begins. Unsupported
  rotation tooling warns and fails affected files safely; core failures stop
  the run with upgrade instructions.
- Ctrl-C terminates active FFmpeg children, removes partial outputs, records
  queued/running work in `.mediate-run.json`, and prioritizes matching
  unfinished files on the next run.
- If the target name already exists (e.g. `a.jpg` and `a.png` both map to
  `a.webp`), the file is skipped and logged rather than overwritten.
- The output inherits the original's modification time **and, on macOS, its
  creation date** (via `setattrlist`), so both EXIF-based and Finder-based
  date sorting keep working.

## Notes

- `.aae`/`.xmp` sidecars describe their original file, so when an original is
  disposed of after conversion, its sidecars travel with it.
- ffprobe results are cached (`~/Library/Caches/mediate` / `$XDG_CACHE_HOME`),
  keyed by path, nanosecond mtime/ctime, size, device, and inode. Expensive
  optional full-decode health entries also include a sampled BLAKE2 fingerprint.
  Completed MP4s are reported as one aggregate “already standardized” count.
- An unexpected exception in one worker is recorded as that file's failure;
  it no longer stops collection of the rest of the scan and makes untouched
  files appear only on the next run.
- Interactive terminals show one live progress bar for every concurrent video
  encode, remux, and integrity check, including media time, percent, speed, and
  ETA. Redirected/non-interactive logs receive 10% updates and a heartbeat at
  least once per minute. Files over 100 MB also announce themselves up front.
- exiftool queries (metadata validation, `--date-prefix`, Live Photo
  verification) go through a persistent `-stay_open` daemon — one process
  for the whole run instead of one per file.
- Windows: the Recycle Bin is not supported — mediate requires
  `--graveyard DIR` or `--hard-delete` there.
- Simple text subtitles are converted to MP4 `mov_text`; JPEG/PNG cover streams
  are copied. Styled/bitmap subtitles and incompatible attachments remain
  blocked unless `--allow-stream-removal` makes their removal deliberate.
- `.tif` inputs whose EXIF matters will fail the new metadata check (cwebp
  cannot carry TIFF metadata) and stay untouched — by design.
- Live Photo detection uses same-directory/stem naming to find candidates,
  then requires matching Apple `ContentIdentifier` metadata when ExifTool is
  available. Without ExifTool, the naming-only fallback remains conservative.
- The HEIC pipeline uses a PNG intermediate deliberately: sips carries EXIF
  into PNG and cwebp extracts EXIF from PNG, whereas cwebp silently drops
  metadata from TIFF input.

## Tests

```sh
python3 -m unittest discover tests
```

The suite includes generated-media integration tests for ASF/VOB, surround and
multiple audio tracks, chapters, subtitle/artwork preservation, rotation,
high-bit-depth blocking, corrupt packets, and missing MP4 indexes.
They run automatically when `ffmpeg`, `ffprobe`, and `libx264` are available,
and otherwise skip without making the pure-Python tests unavailable.
