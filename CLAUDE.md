# mediate — Architecture Notes

Stdlib-only Python (≥3.9) CLI by Eddy Sant, built with AI assistance. Recursively
standardizes a media library: photos → lossless WebP (`cwebp`), videos/animated
GIFs → h264/yuv420p/AAC MP4 (`ffmpeg`). Originals are disposed of (Trash by
default) **only** after a strict validation checklist. No Python dependencies —
everything is subprocess calls to `cwebp`/`ffmpeg`/`ffprobe` (+ `sips` on macOS).

## Module map (`mediate/`)

| Module | Role |
|---|---|
| `cli.py` | argparse, dual logging (console + `conversion.log`), planning-time skips, ThreadPoolExecutor, summary/exit codes |
| `scanner.py` | `os.walk` traversal → `MediaJob(path, kind)`; kind ∈ photo/heic/gif/video/mp4; Live Photo pairing helper |
| `probe.py` | `ffprobe -of json` helpers: `mp4_status()` (standard/hevc/convert), `gif_is_animated()` |
| `converters.py` | command construction, temp-file protocol, `process_job()` — the whole convert→validate→dispose→rename pipeline |
| `validators.py` | the 4-step checklist (exit code, exists, size > 0, full-decode integrity for videos) |
| `disposal.py` | Trash (macOS per-volume `.Trashes`, freedesktop elsewhere) / `--graveyard DIR` / `--hard-delete` |
| `macmeta.py` | ctypes `setattrlist(2)` to copy the original's birthtime (Finder "date created") onto outputs; no-op off macOS |

## The safety pipeline (order matters)

`process_job` in `converters.py`:

1. Probe-based skips (standard/HEVC mp4, static gif, HEIC without `--convert-heic`).
2. Convert into a **hidden temp name** (`.stem.<rand8>.part.ext`) in the same
   directory — never the final name, so a crash can't leave a half-written file
   looking finished.
3. Validate (`validators.py`). Failure → delete temp, keep original, log stderr.
4. `--only-if-smaller` check (after validation, before disposal).
5. Dispose of the original (**before** `os.replace`, because a re-encoded
   `foo.mp4` targets its own name). Disposal failure → discard temp, FAILED.
6. `os.replace(tmp, final)`, then `os.utime` (mtime) + `set_birthtime` (macOS).

## Gotchas / hard-won details

- **Output-name collisions are resolved before the pool starts** (`cli.py`):
  `a.jpg` + `a.png` both map to `a.webp`; with concurrent workers both would
  pass the `final.exists()` pre-check and the second rename would clobber the
  first *after both originals were disposed*. `intended_output()` claims names
  planning-time; later duplicates become SKIPPED outcomes.
- **HEIC pipeline must use a PNG intermediate** (`_convert` in `converters.py`):
  `sips → PNG → cwebp`. sips copies EXIF into PNG and cwebp extracts it; with a
  TIFF intermediate cwebp prints "EXIF extraction from TIFF is unsupported" and
  silently drops all metadata (the bug that shaped this design). cwebp can't
  read HEIC at all (HEVC-compressed stills, patent-encumbered).
- **Bundle guard is deliberately non-overridable** (`scanner.py`): directories
  with `.photoslibrary`/`.app`/`.fcpbundle`-style suffixes are pruned from
  `os.walk`. `Photos Library.photoslibrary` is *not* hidden — without this,
  pointing mediate at `~/Pictures` would convert/delete Apple Photos' masters.
- **Live Photo protection covers both halves** (`find_live_photo_companions` +
  `cli.py`): a `.mov` sharing dir+stem with a still. Converting *either* half
  breaks the ContentIdentifier pairing, so both are skipped unless
  `--convert-live-photos`. Detection is naming-convention only (no exiftool dep).
- **HEVC MP4s are skipped by default**: re-encoding HEVC→h264 at crf 18 *grows*
  the file (verified 7.6 KB → 11.3 KB on a test clip) and Apple plays HEVC
  natively. `--reencode-hevc` opts into the size hit for non-Apple targets.
- **Validation requires empty stderr, not just exit 0**, on the video integrity
  pass — ffmpeg reports many corruptions on stderr while still exiting 0.
- **Probe failures fail open on purpose**: an unreadable mp4/gif is treated as
  needing conversion; the conversion attempt then fails validation and the
  original is kept. Never fail toward skipping validation.
- All subprocess commands are **argv lists** (no shell), with `-nostdin` on
  every ffmpeg call (it grabs the TTY otherwise) and `-y` (safe: temp names are
  unique and pre-checked).
- Trash on macOS prefers the file's own volume's `.Trashes/<uid>` — moving a
  huge video to home `~/.Trash` from an external drive would be a full copy.
  Note: terminal processes can't *list* `~/.Trash` (TCC), but renames into it work.
- `setattrlist` is the only stable macOS API for setting `ATTR_CMN_CRTIME`;
  also, setting mtime older than birthtime implicitly lowers birthtime, so the
  utime→set_birthtime order matters less than it looks — but keep it anyway.

## Testing

- `python3 -m unittest discover tests` — scanner classification, hidden/bundle
  skips, Live Photo pairing rules. Pure-tmpdir, no media tools needed.
- End-to-end verification is manual but scriptable: generate fixtures with
  ffmpeg lavfi (`testsrc=size=321x239` exercises the odd-dimension GIF filter;
  `sips -s format heic` fabricates HEICs; `exiftool` seeds EXIF), run against a
  scratch dir, assert with `ffprobe`/`exiftool`/`stat -f %SB`. The dry run must
  be checked before the real run — it exercises the planning-time skip logic.
- The Homebrew ffmpeg here has no `libwebp` encoder; make `.webp` fixtures with
  `cwebp`, not ffmpeg.

## Improvement ideas (not yet done)

1. **CI** — a GitHub Actions workflow running the unit tests on push.
2. **Sidecar awareness** — `.xmp`/`.aae` files are orphaned when their media
   converts; could be renamed alongside or flagged.
3. **Resume/skip cache** — remember validated conversions so a re-run over a
   huge library doesn't re-probe every mp4.
4. **`--convert-heic` off macOS** — could fall back to ffmpeg ≥7 HEIC demuxing
   where available instead of hard-requiring sips.
