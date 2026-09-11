# mediate

Settles your media library's format disputes: a terminal app that recursively
standardizes photos and videos into compatible, space-efficient formats — and
only parts with an original after the converted file passes a strict
validation checklist. Even then it goes to the Trash, not oblivion.

Stdlib-only Python (≥ 3.9). Everything else is `ffmpeg`, `cwebp`, and friends.

**[Quick start](#quick-start)** · [What it converts](#what-it-converts) ·
[Options](#options) · [Renaming](#name-standardization) ·
[Config](#config-file) · [What it skips](#what-it-skips) ·
[Safety](#safety-protocol) · [How it works](#how-it-works) · [Tests](#tests)

## Quick start

```sh
brew install eddysant/tap/mediate     # installs ffmpeg + webp too
```

Or grab the single-file `mediate.pyz` from the
[latest release](https://github.com/eddysant/mediate/releases/latest), or run
straight from a checkout with no install at all.

```sh
mediate ~/Pictures/Library --dry-run              # see what would happen
mediate ~/Pictures/Library                        # convert; originals -> Trash
```

Always start with `--dry-run`. A good full-strength invocation for a real
library:

```sh
mediate ~/Pictures/Library --only-if-smaller --convert-heic
```

From a checkout, use `python3 -m mediate` in place of `mediate`.

### Requirements

| | |
|---|---|
| **Required** | `ffmpeg`, `ffprobe`, `cwebp` — `brew install ffmpeg webp` |
| FFmpeg 6+ | 9+ only if animated WebPs are present |
| `avifenc` | optional, for `--output-format avif` — `brew install libavif` |
| `jpegtran` | optional, enables lossless recovery of truncated JPEGs — `brew install jpeg-turbo` (bundled in the Homebrew formula) |
| `exiftool` | optional, improves metadata checks, Live Photo detection, and `--date-prefix` |

HEIC conversion needs either macOS (`sips` is built in) or an FFmpeg build
that reads HEIC stills — most do, since HEIC is ISOBMFF and FFmpeg reads it
through the mov/mp4 demuxer.

Before converting anything, mediate checks tool versions and runs a tiny
h264/AAC encode plus a JSON probe. Missing encoders and mismatched FFmpeg
builds fail up front with an installation hint.

## What it converts

| Input | Output | Tool |
|---|---|---|
| JPEG / PNG / TIFF | Lossless WebP, or lossless AVIF with `--output-format avif` | `cwebp` / `avifenc` |
| HEIC / HEIF (opt-in) | Lossless WebP / AVIF via a PNG intermediate, EXIF preserved | `sips` or `ffmpeg`, then `cwebp` / `avifenc` |
| MOV / MKV / AVI / WMV / WebM / ASF / VOB / legacy video | MP4 (h264 `-crf 18 -preset slow`, AAC 256k, `yuv420p`) | `ffmpeg` |
| Animated GIF / animated WebP | MP4 (`faststart`, even-dimension scale filter) | `ffmpeg` |

Legacy coverage includes QuickTime (`.mov`, `.qt`), Windows Media (`.asf`,
`.wmv`, `.dvr-ms`, `.wtv`), DVD and camcorder formats (`.vob`, `.vro`, `.mod`,
`.tod`, `.dv`), RealMedia, Ogg video, MPEG transport and elementary streams,
MXF, 3GPP/3GPP2, Flash video, and common DivX/Xvid extensions. Every format
goes through the same ffprobe preflight and validation pipeline.

## Options

### Everyday

- `--dry-run` — traverse and print what would happen; nothing is written or deleted.
- `--keep-originals` — convert but never touch inputs. A non-standard `foo.mp4`
  becomes `foo.standardized.mp4`, since the original keeps its name.
- `--only-if-smaller` — discard the conversion and keep the original unless the
  output is actually smaller. **Recommended**: lossless WebP is frequently
  *bigger* than a camera JPEG or HEIC, because it must reproduce the source's
  compression noise exactly.
- `--workers N` — concurrent conversions: an integer, or `auto` (the default)
  to use the CPU count, capped at 8 for video-heavy runs.
- `--log-file PATH` — default is `conversion.log` in the target directory. The
  console shows one line per file; the log adds timestamps and full converter
  stderr on failures.
- `--verbose` — show debug output on the console.
- `--no-config` — ignore the config file for this run.
- `--version` — print the version and exit.

### Convert more

- `--convert-heic` — convert HEIC/HEIF, which is skipped by default because it
  is already space-efficient. Pair with `--only-if-smaller`.
- `--reencode-hevc` — convert HEVC MP4s to h264 for non-Apple compatibility,
  accepting the size increase.
- `--convert-live-photos` — convert Live Photo pairs anyway.
- `--output-format {webp,avif}` — photo output format. Both are lossless; AVIF
  is often *larger* than WebP for photos.
- `--validate-existing` — fully decode already-standardized MP4s and repair
  any corruption found. This library-health pass is potentially expensive and
  off by default; newly converted outputs are always validated regardless.

### Accept a loss (explicit opt-ins)

- `--allow-stream-removal` — let MP4 conversion discard incompatible
  subtitles, artwork, extra video tracks, and data/attachment streams.
  Compatible text subtitles and JPEG/PNG cover streams are always preserved.
- `--allow-video-downgrade` — permit 8-bit h264 conversion of
  HDR/high-bit-depth, alpha, interlaced, or variable-frame-rate video.

### Where originals go

Trash by default.

- `--graveyard DIR` — move originals to DIR, mirroring the folder structure.
- `--hard-delete` — permanently delete them (the pre-Trash behavior).

### Renaming

`--rename`, `--rename-only`, `--rename-folders`, `--date-prefix`,
`--undo-renames`, `--plan-file PATH`, `--apply-plan PATH`. See
[Name standardization](#name-standardization).

### Exit codes

`0` nothing failed · `1` a file failed validation · `2` usage error (bad
directory, missing tools).

## Name standardization

Besides formats, mediate can settle *naming* disputes. `--rename` runs after
conversion, so fresh `.webp`/`.mp4` outputs are covered too; `--rename-only`
skips conversion entirely. Both respect `--dry-run`.

```sh
mediate ~/Pictures/Library --rename-only --dry-run   # preview
mediate ~/Pictures/Library --undo-renames            # regret the last batch

# review-then-commit: write the plan, edit the JSON, apply it
mediate ~/Pictures/Library --rename-only --plan-file plan.json
mediate ~/Pictures/Library --apply-plan plan.json
```

**Cleanup** — underscores, dots and dashes become spaces (digit-dash-digit
survives, so `2023-01-05` keeps its shape); whitespace collapses; lowercase
words are title-cased, with small words like "of" staying lower unless
leading; existing capitalization such as `USA` or `McDonald` is respected;
extensions are lowercased; Unicode is NFC-normalized.

**Numbering** — `photo (1)` → `Photo [1]`, `Wren Tally - 2` →
`Wren Tally [1]`, `Tilly-Marsh-001` → `Tilly Marsh [1]`. `Copy of X`,
`X - copy` and `X copy 2` markers join the numbering. Every series compacts to
start at 1 with gaps closed (`1,2,4` → `1,2,3`), and once a series reaches
double digits, single digits are zero-padded (`[01]`…`[10]`) so lexical order
matches numeric order. Series are per directory + base name + site +
extension, so different file types count independently. A bare space-number
(`Terminator 2`) is *not* numbering; only `(N)`, `[N]` and dash-`N` forms are.

**Websites** move into the tag: `Nova-Quinn-Example.com-4` →
`Nova Quinn [Example.com 1]`, each site its own series.

**Meaningless names** take their folder's name — GUIDs and random
letter/digit tokens alike: `Nova/ue73up.jpg` → `Nova [ue73up].jpg`,
`Vacation 2019/550e8400-….jpg` → `Vacation 2019 [550e8400-…].jpg`.

**Left verbatim** — camera counters (`IMG_1234`, `DSC_0001`, `PXL_…`) and
screenshot/WhatsApp names, where there is nothing human to fix and the dots
and digits are data. Names already carrying an unrecognized `[…]` tag are
also left alone, which is what makes re-runs idempotent.

**Options** — `--rename-folders` cleans directory names with the same rules;
`--date-prefix` prepends the capture date (`2019-06-01 Misty Vale [01].webp`)
from EXIF via exiftool, video `creation_time`, or file mtime.

**Safety** — a rename never overwrites. Live Photo `.mov` halves mirror their
still's rename and `.aae`/`.xmp` sidecars follow their media file, so pairings
survive. Files that clean to the same name (`misty_vale.jpg` and
`misty.vale.jpg` both becoming `Misty Vale`) join one numbered series rather
than colliding; whichever already has the final name keeps it. Every applied
batch is recorded in `.mediate-renames.json` at the library root, and
`--undo-renames` reverses the most recent batch, repeatable batch by batch.

## Config file

Default flags live in `~/.config/mediate/config` (or `$MEDIATE_CONFIG`), one
flag per line, `#` comments:

```
# my defaults
--only-if-smaller
--convert-heic
--workers 4
```

They are prepended to every invocation. `--no-config` ignores the file for one
run.

## What it skips

**Already standardized** — static `.webp`, static `.avif`, static GIFs, hidden
files and directories, and MP4s that are already h264/8-bit 4:2:0/AAC
(including FFmpeg's full-range `yuvj420p` alias).

**Would get bigger** — HEVC MP4s are smaller than h264 and play natively on
Apple devices, so re-encoding only grows the file (`--reencode-hevc` to
force). HEIC/HEIF likewise (`--convert-heic`).

**Would break a pairing** — a `.mov` and same-named still whose Apple
`ContentIdentifier` metadata matches are a Live Photo. Both halves are left
alone, since converting either breaks the pairing in Apple Photos
(`--convert-live-photos`).

**Would lose content** — videos whose MP4 conversion would discard
styled/bitmap subtitles, incompatible attachments, extra video angles,
arbitrary data streams, or stream groups such as an LCEVC enhancement layer.
Simple text subtitles become `mov_text` and JPEG/PNG cover streams are kept
automatically; `--allow-stream-removal` opts into dropping the rest.

**Would lose picture semantics** — video that would silently lose HDR
(including SMPTE 2094-50 metadata), high bit depth, alpha, interlacing, or
variable/unusual frame rate in an 8-bit encode. Animated WebPs with alpha or
ICC/EXIF/XMP metadata are covered by the same rule.
`--allow-video-downgrade` is the explicit opt-in.

**Application bundles** — `*.photoslibrary`, `*.app`, `*.fcpbundle` and
friends are never traversed. Converting files inside an Apple Photos library
would corrupt it, so **this cannot be overridden**.

## Safety protocol

### The checklist

An original is disposed of **only** after all of these pass:

1. Converter exit code is `0`.
2. The output file exists.
3. The output file is larger than 0 bytes.
4. **(Videos)** `ffmpeg -v error -i out.mp4 -f null -` exits `0` **and** prints
   nothing to stderr — a full-decode integrity check. FFmpeg reports many
   corruptions on stderr while still exiting 0, so silence is required.
5. **(macOS videos)** AVFoundation — the playback stack behind Quick Look —
   reports the finished MP4 as playable. An FFmpeg-readable file that Apple
   rejects is discarded and the original left untouched.
6. **Metadata survived.** A photo whose source has an EXIF capture date must
   carry the same date in the output. This is what catches cwebp silently
   dropping TIFF metadata.
7. **Streams survived.** Duration matches within 1s/2%, and the full inventory
   is checked: every audio track keeps its duration, language,
   title/commentary, dispositions, channel count and layout, sample rate,
   profile, and A/V start offset; chapters, rotation, colour, aspect ratio,
   field order, frame rate, compatible subtitles, artwork, and HDR side data
   are verified too.

On any failure the partial output is removed, the original is untouched, and
the reason is logged.

### Damaged sources

**Truncated JPEGs** are first run through `jpegtran -copy all`, rebuilding the
JPEG structure while copying compressed coefficients and metadata without
another lossy encode, then retried. The repair happens on a temporary file;
the source is untouched until the validated replacement commits.

**Truncated videos** are different — mediate cannot reconstruct packets that
are absent from the file, but it can salvage the readable prefix. It compares
every source video and audio packet timeline against the repaired output,
keeps the damaged source automatically, and records the salvage so later runs
do not retry it. The result reports both the recoverable duration and the
longer duration the damaged header claimed.

**Containers that understate themselves** are handled too. Concatenated MPEG
program streams — how a ripped DVD's `VTS_01_N.VOB` parts are usually joined —
restart their timestamps at each join, so the container reports only the final
segment. When the output is *longer* than the source claims, mediate measures
the source by decoding it before rejecting a conversion that is probably
correct. A *short* output is still genuine truncation and still fails.

### Existing MP4s

By default these are classified by stream format and skipped without a full
decode. `--validate-existing` turns on the library-health pass: mediate fully
decodes every stream, asks AVFoundation to open each MP4 on macOS, caches the
result by filesystem identity plus sampled content, and repairs damage or
Apple playback rejection. Repair is lossless-first — container, index and
timestamp rebuild — then tolerant re-encoding. Every repaired file still
passes the complete checklist. Damaged HEVC remains opt-in via
`--reencode-hevc`.

An existing MP4 is not automatically a *compatible* MP4, and mediate explains
whether video, pixel format, audio, or codec tags require standardization.
Compatible H.264 or HEVC video is copied bit-for-bit when only its audio needs
conversion, and `hev1` HEVC is losslessly retagged to `hvc1` for Apple
playback instead of spending hours on an unnecessary re-encode. New MP4s write
explicit `avc1`/`hvc1` and `mp4a` tracks, an `mp42` brand, and a fast-start
index.

### Beyond the checklist

- **Originals go to the Trash by default** — per-volume `.Trashes` on macOS so
  external drives aren't copied across volumes, freedesktop trash with
  `.trashinfo` on Linux. Video re-encoding is lossy: once an original is
  hard-deleted that quality is gone forever, so recoverability is the default.
- **Nothing is written under its final name until it is proven.** Conversions
  go to a hidden temp name (`.name.<rand>.part.ext`) and enter a durable
  two-phase replacement transaction only after validation. The original is
  staged, the output installed atomically, and the Trash/graveyard/delete
  policy runs last. On startup, interrupted transactions either restore the
  original or finish a proven installation.
- **FFmpeg's implicit "best stream" selection is never trusted.** Video
  commands explicitly map the primary video plus *all* audio tracks and
  chapters. A rotated video gets a short lossless remux after encoding to
  restore its display matrix before validation.
- **The filesystem is checked before work starts.** The source must be a
  readable regular file with no symlink or hard-link ambiguity, its output
  directory must be writable, and conservative temporary free space must
  exist. Concurrent workers reserve that budget per filesystem so they cannot
  all spend the same free bytes. Device, inode, size and mtime are rechecked
  before disposal, so a moving source is never replaced.
- **The toolchain is checked before media work begins** — FFmpeg, ffprobe,
  libx264, AAC, MP4 muxing, JSON probing, progress and rotation support.
  Unsupported rotation tooling warns and fails affected files safely; core
  failures stop the run with upgrade instructions.
- **Ctrl-C is safe.** It terminates active FFmpeg children, removes partial
  outputs, records queued and running work in `.mediate-run.json`, and
  prioritizes unfinished files on the next run.
- **Output names never collide.** Two inputs mapping to one output (`a.jpg`
  and `a.png` both becoming `a.webp`) are given distinct names before any
  worker starts, so concurrent conversions cannot overwrite each other.
- **Dates are preserved.** The output inherits the original's modification
  time and, on macOS, its creation date via `setattrlist`, so both EXIF-based
  and Finder-based sorting keep working.

## How it works

**Caching.** ffprobe results are cached in `~/Library/Caches/mediate` (or
`$XDG_CACHE_HOME`), keyed by path, nanosecond mtime/ctime, size, device and
inode. Expensive full-decode health entries also carry a sampled BLAKE2
fingerprint. Completed MP4s are reported as one aggregate "already
standardized" count. With `--keep-originals`, each validated source/output
pair is recorded so later runs skip the unchanged source while that exact
output is intact — preventing repeat conversions and duplicate files.

**Progress.** Interactive terminals show one live bar per concurrent encode,
remux and integrity check, with media time, percent, speed and ETA.
Redirected logs get 10% updates and a heartbeat at least once a minute. Files
over 100 MB announce themselves up front.

**exiftool** queries — metadata validation, `--date-prefix`, Live Photo
verification — go through a small pool of persistent `-stay_open` daemons
rather than one process per file.

**Sidecars.** `.aae`/`.xmp` files describe their original, so when an original
is disposed of after conversion, its sidecars travel with it.

**Live Photo detection** finds candidates by same-directory stem naming, then
requires matching Apple `ContentIdentifier` metadata when ExifTool is
available. Without ExifTool the naming-only fallback stays conservative.

**The HEIC PNG intermediate is deliberate.** Both sips and FFmpeg carry EXIF
into PNG and cwebp extracts EXIF from PNG, whereas cwebp silently drops
metadata from TIFF input. Because FFmpeg reads HEIC through the mov/mp4
demuxer, every auxiliary image (thumbnail, depth map) appears as a stream, so
the largest is selected explicitly and the decoded dimensions are checked —
otherwise a thumbnail could quietly replace the photo.

**Failures are isolated.** An unexpected exception in one worker is recorded
as that file's failure and no longer stops collection of the rest of the scan.

**Known limits.** `.tif` inputs whose EXIF matters will fail the metadata
check (cwebp cannot carry TIFF metadata) and stay untouched, by design. On
Windows the Recycle Bin is unsupported, so `--graveyard DIR` or
`--hard-delete` is required.

## Tests

```sh
python3 -m unittest discover tests
```

Alongside the pure-Python tests, the suite generates real media to cover
ASF/VOB, surround and multi-track audio, chapters, subtitle and artwork
preservation, rotation, high-bit-depth blocking, corrupt packets, and missing
MP4 indexes. Those run automatically when `ffmpeg`, `ffprobe` and `libx264`
are available and skip cleanly otherwise.
