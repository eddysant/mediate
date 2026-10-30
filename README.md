# mediate

Settles your media library's format disputes: a terminal app that recursively
standardizes photos and videos into compatible, space-efficient formats — and
only parts with an original after the converted file passes a strict
validation checklist. Even then it goes to the Trash, not oblivion.

**macOS only.** Stdlib-only Python (≥ 3.10); everything else is `ffmpeg`,
`cwebp`, and friends. The checks that make deleting an original safe are
Apple's — AVFoundation playback validation, `sips` for HEIC stills,
`setattrlist` for Finder's creation date, the per-volume `.Trashes` layout —
so mediate refuses to run anywhere else rather than doing the destructive
part with weaker guarantees.

[Quick start](#quick-start) · [What it converts](#what-it-converts) ·
[Options](#options) · [Renaming](#renaming) · [Config](#config-file) ·
[What it skips](#what-it-skips) · [Is it safe?](#is-it-safe) ·
[Tests](#tests)

## Quick start

```sh
brew install eddysant/tap/mediate     # installs ffmpeg + webp too
```

Or grab the single-file `mediate.pyz` from the
[latest release](https://github.com/eddysant/mediate/releases/latest), or run
straight from a checkout with `python3 -m mediate`.

```sh
mediate ~/Pictures/Library --dry-run   # see what would happen
mediate ~/Pictures/Library             # convert; originals -> Trash
```

Always start with `--dry-run`. A good full-strength invocation for a real
library:

```sh
mediate ~/Pictures/Library --only-if-smaller --convert-heic
```

### Requirements

| | |
|---|---|
| **Required** | `ffmpeg`, `ffprobe`, `cwebp` — `brew install ffmpeg webp` |
| FFmpeg 6+ | 9+ only if animated WebPs are present |
| `avifenc` | optional, for `--output-format avif` — `brew install libavif` |
| `jpegtran` | optional, recovers truncated JPEGs — `brew install jpeg-turbo` (bundled in the Homebrew formula) |
| `exiftool` | optional, improves metadata checks, Live Photo detection, and `--date-prefix` |

HEIC conversion uses `sips`, built into macOS, falling back to FFmpeg. Before
converting anything, mediate checks tool versions and runs a tiny test encode,
so missing encoders fail up front with an installation hint.

## What it converts

| Input | Output | Tool |
|---|---|---|
| JPEG / PNG / TIFF | Lossless WebP, or lossless AVIF with `--output-format avif` | `cwebp` / `avifenc` |
| HEIC / HEIF (opt-in) | Lossless WebP / AVIF via a PNG intermediate, EXIF preserved | `sips` or `ffmpeg`, then `cwebp` / `avifenc` |
| MOV / MKV / AVI / WMV / WebM / ASF / VOB / legacy video | MP4 (h264 `-crf 18 -preset medium`, AAC 256k, `yuv420p`) | `ffmpeg` |
| Animated GIF / animated WebP | MP4 (`faststart`, even-dimension scale filter) | `ffmpeg` |

Legacy coverage includes QuickTime, Windows Media (`.asf`, `.wmv`, `.dvr-ms`,
`.wtv`), DVD and camcorder formats (`.vob`, `.vro`, `.mod`, `.tod`, `.dv`),
RealMedia, Ogg video, MPEG transport and elementary streams, MXF, 3GPP, Flash
video, and common DivX/Xvid extensions — all through the same validation
pipeline.

> **Photos converted before v0.15.0 were not actually lossless.** A stray
> `-preset photo` after `cwebp`'s `-lossless` flag reset the parameter set and
> produced an ordinary lossy file; validation did not notice, so those
> originals were disposed of. Those `.webp` files cannot be recovered, but
> anything still in the Trash or a graveyard is the untouched original.
> Conversions are now checked for a genuine lossless stream and fail closed
> otherwise.

## Options

### Everyday

- `--dry-run` — traverse and print what would happen; nothing is written or deleted.
- `--keep-originals` — convert but never touch inputs. A non-standard `foo.mp4`
  becomes `foo.standardized.mp4`, since the original keeps its name.
- `--only-if-smaller` — keep the original unless the output is actually
  smaller. **Recommended**: lossless WebP is frequently *bigger* than a camera
  JPEG or HEIC, because it must reproduce the source's compression noise
  exactly.
- `--workers N` — concurrent conversions: an integer, or `auto` (the default).
- `--log-file PATH` — default is `conversion.log` in the target directory. The
  console shows one line per file; the log adds timestamps and full converter
  stderr on failures.
- `--verbose` · `--no-config` · `--version`

### Convert more

- `--convert-heic` — convert HEIC/HEIF, skipped by default because it is
  already space-efficient. Pair with `--only-if-smaller`.
- `--reencode-hevc` — convert HEVC MP4s to h264 for non-Apple compatibility,
  accepting the size increase.
- `--convert-live-photos` — convert Live Photo pairs anyway.
- `--output-format {webp,avif}` — photo output format. Both are lossless; AVIF
  is often *larger* than WebP for photos.
- `--validate-existing` — fully decode already-standardized MP4s and repair any
  corruption found. Expensive, so off by default; newly converted outputs are
  always validated regardless.

### Speed

- `--encode-preset PRESET` — libx264 preset, default `medium`. On grainy
  footage `slow` measures both slower *and larger* — its psy settings spend
  bits preserving grain. `veryfast` is where quality starts to fall off.
- `--photo-effort {fast,default,max}` — photos are always lossless, so effort
  trades time against size, never quality. For WebP the default is the sweet
  spot; for AVIF `max` is a real option if size matters more than time.
- `--hardware` — use VideoToolbox. Roughly 5–7x faster, but measurably lower
  quality per byte, which is why it is opt-in. Outputs go through the same
  strict validation either way.

`--workers` barely affects video (x264 already saturates every core) but is the
only parallelism photos have. The numbers behind all of this are in
[AGENTS.md](AGENTS.md#measured-numbers).

### Accept a loss (explicit opt-ins)

- `--allow-stream-removal` — let MP4 conversion discard incompatible subtitles,
  artwork, extra video tracks, and data/attachment streams. Compatible text
  subtitles and JPEG/PNG cover streams are always preserved.
- `--allow-video-downgrade` — permit 8-bit h264 conversion of
  HDR/high-bit-depth, alpha, interlaced, or variable-frame-rate video.

### Where originals go

Trash by default — the file's own volume's `.Trashes`, so a video on an
external drive is a rename rather than a full copy.

- `--graveyard DIR` — move originals to DIR, mirroring the folder structure.
- `--hard-delete` — permanently delete them.

### Exit codes

`0` nothing failed · `1` a file failed validation · `2` usage error (bad
directory, missing tools).

## Renaming

Besides formats, mediate can settle *naming* disputes. `--rename` runs after
conversion, so fresh outputs are covered too; `--rename-only` skips conversion
entirely. Both respect `--dry-run`.

```sh
mediate ~/Pictures/Library --rename-only --dry-run   # preview
mediate ~/Pictures/Library --undo-renames            # regret the last batch

# review-then-commit: write the plan, edit the JSON, apply it
mediate ~/Pictures/Library --rename-only --plan-file plan.json
mediate ~/Pictures/Library --apply-plan plan.json
```

**Cleanup** — underscores, dots and dashes become spaces; whitespace
collapses; lowercase words are title-cased, with small words like "of" staying
lower unless leading; existing capitalization such as `USA` or `McDonald` is
respected; extensions are lowercased; Unicode is NFC-normalized. Meaningful
punctuation is protected: initialisms keep their dots and are uppercased
(`e.e. cummings` → `E.E. Cummings`) while a longer abbreviation is still a
separator (`Mr. Smith` → `Mr Smith`), and a name beginning `YYYY-MM-DD` keeps
the clock in its dots.

**Numbering** — `photo (1)` → `Photo [1]`, `Wren Tally - 2` → `Wren Tally [1]`,
`Tilly-Marsh-001` → `Tilly Marsh [1]`. `Copy of X` markers join the numbering.
Every series compacts to start at 1 with gaps closed, and zero-pads once it
reaches double digits so lexical order matches numeric order. Series are per
directory + base name + site + extension. A bare space-number (`Terminator 2`)
is not numbering, and neither is a parenthesised year — `The Matrix (1999).mp4`
keeps it.

**Websites** move into the tag: `Nova-Quinn-Example.com-4` →
`Nova Quinn [Example.com 1]`.

**Meaningless names** take their folder's name — GUIDs and random tokens alike:
`Nova/ue73up.jpg` → `Nova [ue73up].jpg`.

**Left verbatim** — camera counters (`IMG_1234`, `DSC_0001`, `PXL_…`) and
screenshot/WhatsApp names, where the dots and digits are data. Names already
carrying an unrecognized `[…]` tag are left alone too, which is what makes
re-runs idempotent.

**Options** — `--rename-folders` cleans directory names with the same rules;
`--date-prefix` prepends the capture date (`2019-06-01 Misty Vale [01].webp`).

**Safety** — a rename never overwrites, symlinks are skipped, names are
trimmed to the filesystem's 255-byte limit with the numbering tag kept intact,
and Live Photo halves and `.aae`/`.xmp` sidecars follow their media file so
pairings survive. Every applied batch is recorded in `.mediate-renames.json`
at the library root, and `--undo-renames` reverses the most recent batch,
repeatable batch by batch.

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
files and directories, and MP4s that are already h264/8-bit 4:2:0/AAC.

**Would get bigger** — HEVC MP4s play natively on Apple devices and are
smaller than h264, so re-encoding only grows the file (`--reencode-hevc` to
force). HEIC/HEIF likewise (`--convert-heic`).

**Would break a pairing** — a `.mov` and same-named still whose Apple
`ContentIdentifier` matches are a Live Photo. Converting either half breaks the
pairing in Apple Photos, so both are left alone (`--convert-live-photos`).

**Would lose content** — videos whose MP4 conversion would discard
styled/bitmap subtitles, incompatible attachments, extra video angles, data
streams, or stream groups such as an LCEVC enhancement layer. Simple text
subtitles and JPEG/PNG covers are kept automatically; `--allow-stream-removal`
opts into dropping the rest.

**Would lose picture semantics** — video that would silently lose HDR, high bit
depth, alpha, interlacing, or variable frame rate in an 8-bit encode.
`--allow-video-downgrade` is the explicit opt-in.

**Application bundles** — `*.photoslibrary`, `*.app`, `*.fcpbundle` and friends
are never traversed. Converting files inside an Apple Photos library would
corrupt it, so **this cannot be overridden**.

## Is it safe?

That is the whole point of the tool, so: an original is disposed of **only**
after its replacement passes every one of these.

1. The converter exited `0` and wrote a file larger than 0 bytes.
2. **(Videos)** A full decode — `ffmpeg -v error -i out.mp4 -f null -` — exits
   `0` *and* prints nothing. FFmpeg reports many corruptions on stderr while
   still exiting 0, so silence is required.
3. **(Videos)** AVFoundation, the playback stack behind Quick Look, reports the
   MP4 as playable. An FFmpeg-readable file that Apple rejects is discarded.
4. **(Videos)** The MP4 is faststart and declares the `mp42` brand — both
   requested by the command, and here confirmed to have reached the file.
5. **(Photos) The output really is lossless** — a WebP must contain a `VP8L`
   stream, an AVIF an identity colour matrix. This is what catches an encoder
   that stops honouring `-lossless`, which happened silently before v0.15.0.
6. **Metadata survived** — the EXIF capture date, and any ICC colour profile
   the source carried. A dropped Display P3 profile shifts every colour and
   breaks nothing visibly.
7. **Streams survived** — duration within 1s/2%, plus every audio track's
   language, title, dispositions, channel layout, sample rate and A/V offset,
   and chapters, rotation, colour, aspect ratio, field order, frame rate,
   subtitles, artwork and HDR side data.

On any failure the partial output is removed, the original is untouched, and
the reason is logged.

Around that checklist:

- **Nothing is written under its final name until it is proven.** Conversions
  go to a hidden temp file and enter a durable two-phase replacement only after
  validation. On startup, an interrupted transaction either restores the
  original or finishes a proven installation.
- **Ctrl-C is safe.** It terminates active FFmpeg children, removes partial
  outputs, records queued work in `.mediate-run.json`, and prioritizes
  unfinished files next run.
- **The filesystem is checked first** — readable regular file, no symlink or
  hard-link ambiguity, writable output directory, and enough free space
  (reserved per filesystem so concurrent workers cannot all spend the same
  bytes). Device, inode, size and mtime are rechecked before disposal, so a
  moving source is never replaced.
- **Output names never collide.** Two inputs mapping to one output (`a.jpg` and
  `a.png` both becoming `a.webp`) are given distinct names before any worker
  starts.
- **Dates are preserved** — modification time and Finder's creation date, so
  both EXIF-based and Finder-based sorting keep working.
- **Damaged sources are handled, not guessed at.** Truncated JPEGs are rebuilt
  with `jpegtran -copy all` and retried; truncated videos keep the damaged
  original and report the salvageable duration; containers that understate
  their own length — concatenated DVD VOBs — are measured by decoding before a
  correct conversion is rejected.

The reasoning behind each of these, and the bugs that produced them, is in
[AGENTS.md](AGENTS.md).

## Good to know

- **Progress.** Interactive terminals show one live bar per concurrent encode,
  with media time, percent, speed and ETA. Redirected logs get 10% updates and
  a heartbeat at least once a minute.
- **Caching.** ffprobe results are cached in `~/Library/Caches/mediate`, keyed
  by filesystem identity, so a re-run over a large library does not re-probe
  everything. Expensive decode results carry a content fingerprint too.
- **Sidecars.** `.aae`/`.xmp` files describe their original, so they travel
  with it when it is disposed of.
- **Failures are isolated** — one file's unexpected error is recorded as that
  file's failure and does not stop the rest of the run.
- **Known limit.** `.tif` inputs whose EXIF matters will fail the metadata
  check and stay untouched, by design: cwebp cannot carry TIFF metadata.

## Tests

```sh
python3 -m unittest discover tests
```

Alongside the pure-Python tests, the suite generates real media to cover
ASF/VOB, surround and multi-track audio, chapters, subtitle and artwork
preservation, rotation, high-bit-depth blocking, corrupt packets, and missing
MP4 indexes. Those run automatically when `ffmpeg`, `ffprobe` and `libx264` are
available and skip cleanly otherwise.
