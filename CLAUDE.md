# mediate — Architecture Notes

Stdlib-only Python (≥3.9) CLI by Misty Vale, built with AI assistance. Recursively
standardizes a media library: photos → lossless WebP (`cwebp`), videos/animated
GIFs → h264/yuv420p/AAC MP4 (`ffmpeg`). Originals are disposed of (Trash by
default) **only** after a strict validation checklist. No Python dependencies —
everything is subprocess calls to `cwebp`/`ffmpeg`/`ffprobe` (+ `sips` on macOS).

## Module map (`mediate/`)

| Module | Role |
|---|---|
| `cli.py` | argparse, dual logging (console + `conversion.log`), planning-time skips, ThreadPoolExecutor, summary/exit codes |
| `scanner.py` | `os.walk` traversal → `MediaJob(path, kind)`; kind ∈ photo/heic/gif/video/mp4; Live Photo pairing helper |
| `probe.py` | cached `ffprobe -of json` helpers: codec/remux classification plus a normalized inventory of all streams, chapters, track identity, rotation, colour, and artwork |
| `converters.py` | command construction, stream-safety policy, temp-file protocol, `process_job()`; includes `-c copy` remuxing for compatible containers and a rotation display-matrix finalizer |
| `validators.py` | exit/existence/size/full-decode checks plus photo metadata and video duration/stream/track/chapter/rotation/colour verification |
| `progress.py` | concurrent FFmpeg progress plus cooperative cancellation and child termination |
| `safety.py` | source snapshots, link/readability policy, output writability and aggregate per-filesystem free-space reservations |
| `journal.py` | atomic `.mediate-run.json` state and interrupted-job prioritization |
| `transaction.py` | durable two-phase validated-output replacement plus startup rollback/completion recovery |
| `capabilities.py` | FFmpeg/cwebp version, encoder, progress, rotation, smoke-encode, and ffprobe JSON preflight |
| `disposal.py` | serializable Trash (macOS per-volume `.Trashes`, freedesktop elsewhere) / graveyard / hard-delete policy |
| `macmeta.py` | ctypes `setattrlist(2)` to copy the original's birthtime (Finder "date created") onto outputs; no-op off macOS |
| `exiftool.py` | persistent `exiftool -stay_open` daemon behind `run_exiftool(args)` (thread-safe, atexit-stopped, one-shot fallback); all exiftool queries go through it |
| `renamer.py` | `--rename`/`--rename-only` phase: stem parsing (paren/bracket/dash numbers, copy markers, `[site N]` tags, websites), cleanup + title case, per-(dir, base, site, ext) series renumbering compacted to 1 with gap-closing and zero-padding, GUID/random-token→folder-name, `--date-prefix`, `--rename-folders`, manifest + `--undo-renames`, never-overwrite apply loop |

## The safety pipeline (order matters)

`process_job` in `converters.py`:

1. Probe-based skips (standard/HEVC mp4, static gif, HEIC without `--convert-heic`).
1a. Before altering a video, inventory every stream and chapter. All audio
    tracks (including commentary) are mapped; chapters, rotation, and colour
    are preserved. Compatible text subtitles and cover streams are mapped;
    incompatible streams cause a safety skip unless `--allow-stream-removal`.
    HDR/high-bit-depth, alpha, interlaced, and unusual/VFR video is blocked
    before an 8-bit encode unless `--allow-video-downgrade`.
1b. For non-MP4 video containers (MOV/MKV/etc): probe streams via
    `video_stream_status()`. If streams are already h264/yuv420p + AAC, set
    the remux flag so `_build_remux_command()` (`-c copy`) is used instead of
    the full re-encode. HEVC streams in non-MP4 containers are skipped
    (same as HEVC MP4s) unless `--reencode-hevc`.
2. Convert into a **hidden temp name** (`.stem.<rand8>.part.ext`) in the same
   directory — never the final name, so a crash can't leave a half-written file
   looking finished.
3. Validate (`validators.py`). Failure → delete temp, keep original, log stderr.
4. Metadata verification: photos must keep their EXIF `DateTimeOriginal`.
   Videos must keep duration within 1s/2%, every audio track's duration/language/
   title/commentary/dispositions/channel layout/sample rate/profile/A-V offset,
   chapters and timings, rotation, compatible subtitle/art streams, and advanced
   picture metadata. This is what stops cwebp's silent TIFF metadata drop and FFmpeg's
   default “best stream” selection from quietly losing an alternate track.
5. `--only-if-smaller` check (after validation, before disposal).
6. Write a durable local transaction manifest, atomically stage the original,
   atomically install the validated output, and restore timestamps. A crash
   before installation rolls back on startup; a crash after a proven install
   finishes the recorded disposal policy.
7. Dispose of the staged original and its sidecars using the serializable
   Trash/graveyard/hard-delete policy, then remove the manifest.

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
- **Stream-inventory failures fail closed**: a video is skipped because it is
  unsafe to alter without knowing what it contains. Narrow codec/GIF probes
  still fail open into conversion/validation where no destructive stream
  selection can occur before the full video preflight.
- **Completed MP4s are filtered before the candidate count** (`cli.py`) and
  their probe cache is saved even when that leaves no work. A single worker
  exception becomes one FAILED outcome instead of aborting result collection;
  together these prevent repeat runs from appearing to discover missed files.
- **Legacy container coverage is intentionally extension-based but probe-safe**:
  ASF/VOB/QuickTime/RealMedia/Ogg/DVD/camcorder/MPEG stream formats enter the
  same ffprobe preflight. A recognized extension with no video stream is safely
  skipped rather than treated as convertible media.
- **Rotation survives encoding via two passes**: `-noautorotate` prevents pixel
  rotation; after the encode, a quick `-c copy` pass with `-display_rotation`
  writes the display matrix. The validator compares normalized angles.
- All subprocess commands are **argv lists** (no shell), with `-nostdin` on
  every ffmpeg call (it grabs the TTY otherwise) and `-y` (safe: temp names are
  unique and pre-checked).
- Trash on macOS prefers the file's own volume's `.Trashes/<uid>` — moving a
  huge video to home `~/.Trash` from an external drive would be a full copy.
  Note: terminal processes can't *list* `~/.Trash` (TCC), but renames into it work.
- `setattrlist` is the only stable macOS API for setting `ATTR_CMN_CRTIME`;
  also, setting mtime older than birthtime implicitly lowers birthtime, so the
  utime→set_birthtime order matters less than it looks — but keep it anyway.
- **Renamer gap-closing needs the deferred-apply loop** (`apply_renames`):
  `[2]→[1], [3]→[2]` — the second rename's target is occupied until the first
  happens. Renames whose target is another pending rename's source wait a
  round; anything still blocked when a full round makes no progress is skipped
  (never overwritten). `samefile()` distinguishes a real collision from a
  case-only rename on case-insensitive APFS (`exists()` lies there).
- **Renamer protected patterns run on the number-stripped base**: `IMG_1234
  (1).JPG` still gets `(1)→[1]` and `.jpg`, but the `IMG_1234` stem is
  verbatim. `PROTECTED_RE` must allow multi-group counters
  (`PXL_20230101_123456`). Word cleanup replaces dots, so timestamped names
  (Screenshot/WhatsApp) are in `NO_CLEAN_RE` — their dots are times.
- **Live Photo `.mov`s mirror their still's rename** rather than renumbering
  in their own series; otherwise diverging gap-closes would break the
  dir+stem pairing the converter's Live Photo guard relies on. With
  `--date-prefix` the mirror/sidecar map must store the *prefixed* stem
  (`finalize()` returns what it actually emitted) or the pair diverges.
  When exiftool is installed, stem-pairs whose `ContentIdentifier`s both
  exist but differ are provably not Live Photos and get unprotected.
- **Renamer parse-order matters**: recognized tags (`[N]`, `[site N]`,
  `[site]`) parse first; any *other* trailing `[…]` marks the name opaque
  (already standardized, e.g. a GUID tag) and only the extension case is
  touched — that's what makes re-runs idempotent. Dash-numbers require a
  non-digit before the dash (`Tilly-Marsh-001` numbers, `2023-01-05` does
  not); a bare space-number (`Terminator 2`) is never numbering. `SITE_RE`
  domain labels deliberately exclude dashes — in filenames a dash is a
  separator, not part of a hyphenated domain.
- **Padding tracks the current series size** (width 2 iff ≥10 members), so a
  series shrinking below 10 unpads on the next run. Numbering always
  compacts to start at 1.
- **Rename manifest**: every applied batch (files then folders, in execution
  order) is appended to `.mediate-renames.json` at the root; undo replays it
  reversed, so folder renames undo before the files inside them. Folder
  plans are applied only after all file renames resolved — file plan paths
  are computed against pre-rename folder names.
- **Probe results are cached** (`probe.py`: path+mtime_ns+ctime_ns+size+device+
  inode keyed JSON; health adds a sampled BLAKE2 fingerprint) in the user cache
  dir, loaded/saved by cli — a 50k-file re-run would otherwise
  spawn ffprobe per MP4/GIF. `load_probe_cache()` must run before the pool.
  `media_duration()` lives here too (shared by validation and progress).
- **Concurrent FFmpeg progress** (`progress.py`): all encodes, remuxes, and
  integrity decodes use `-progress pipe:1 -nostats`. Interactive terminals get
  one stable line per active worker (bar/time/speed/ETA); redirected logs get
  10% updates plus one-minute heartbeats. The console logging handler clears
  and redraws live lines around ordinary records. stderr MUST be drained on a
  thread or the pipe fills and FFmpeg deadlocks.
- **Standard MP4 health is opt-in via `--validate-existing` and full-decode
  cached**. Normal runs trust standard stream classification. With the flag,
  damaged files first get a lossless tolerant remux and only then a
  `+genpts+discardcorrupt`/`ignore_err` re-encode. Every rung gets the complete
  strict validator. HEVC repair still requires
  `--reencode-hevc`. `mark_video_healthy()` avoids decoding a just-validated
  output again on the next scan.
- **Plan files** (`write_plan`/`load_plan`): editable JSON, `--plan-file` to
  write, `--apply-plan` to execute. `load_plan` rejects absolute paths and
  `..` segments — a plan file is user input. Applied plans are recorded in
  the undo manifest like any batch.
- **Config file** (`~/.config/mediate/config` or `$MEDIATE_CONFIG`): flags
  one per line, prepended to argv in `parse_args` unless `--no-config`.
  Tests must set `MEDIATE_CONFIG=/nonexistent` to stay hermetic.
- **CI** (`.github/workflows/ci.yml`): unit tests on ubuntu+macos for every
  push/PR; a `v*` tag additionally builds `mediate.pyz` (stdlib zipapp) and
  creates the GitHub Release with `--generate-notes`. Releasing = bump
  version in `__init__.py`/`pyproject.toml`, tag, push the tag.
- **Homebrew tap** (`eddysant/homebrew-tap`, sibling checkout at
  `~/Code/homebrew-tap`): `Formula/mediate.rb` wraps the release source
  tarball (libexec + PYTHONPATH bin shim on brewed python; ffmpeg/webp as
  deps, exiftool in caveats). After each release, bump the formula's `url`
  tag and `sha256` (`curl -sL <tarball> | shasum -a 256`) and push the tap —
  this is a manual step; CI can't push cross-repo without a PAT.

## Testing

- `python3 -m unittest discover tests` — pure-Python scanner/rename/probe tests
  plus generated-media FFmpeg integration coverage for ASF/VOB, surround and
  multi-audio, chapters, subtitle/artwork policy, corruption, advanced-video
  blocking, rotation, failure-injected transactions, aggregate reservations,
  and a real toolchain smoke check. Media tests auto-skip when
  ffmpeg/ffprobe/libx264 are unavailable.
- End-to-end verification is manual but scriptable: generate fixtures with
  ffmpeg lavfi (`testsrc=size=321x239` exercises the odd-dimension GIF filter;
  `sips -s format heic` fabricates HEICs; `exiftool` seeds EXIF), run against a
  scratch dir, assert with `ffprobe`/`exiftool`/`stat -f %SB`. The dry run must
  be checked before the real run — it exercises the planning-time skip logic.
- The Homebrew ffmpeg here has no `libwebp` encoder; make `.webp` fixtures with
  `cwebp`, not ffmpeg.

## Improvement ideas (not yet done)

1. **`--convert-heic` off macOS** — could fall back to ffmpeg ≥7 HEIC demuxing
   where available instead of hard-requiring sips.
