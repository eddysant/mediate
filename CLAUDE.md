# mediate — Architecture Notes

Stdlib-only Python (≥3.9) CLI by Misty Vale, built with AI assistance. Recursively
standardizes a media library: photos → lossless WebP (`cwebp`), videos and
GIF/WebP animations → h264/yuv420p/AAC MP4 (`ffmpeg`). Originals are disposed
of (Trash by
default) **only** after a strict validation checklist. No Python dependencies —
everything is subprocess calls to `cwebp`/`ffmpeg`/`ffprobe` (+ `sips` on macOS).

## Module map (`mediate/`)

| Module | Role |
|---|---|
| `cli.py` | argparse, dual logging (console + `conversion.log`), `recover_pending_transactions`/`run_rename_shortcuts`/`build_options`/`plan_jobs`/`summarize_run` around a ThreadPoolExecutor, exit codes |
| `scanner.py` | `os.walk` traversal → `MediaJob(path, kind)`; kind ∈ photo/heic/gif/webp/video/mp4; Live Photo pairing helper |
| `probe.py` | cached `ffprobe -of json` helpers: codec/remux classification plus a normalized inventory of all streams, stream groups, chapters, track identity, rotation, colour, and artwork |
| `converters.py` | command construction, stream-safety policy, temp-file protocol, `classify_job()` (every probe-based skip, no filesystem writes) + `process_job()`; includes `-c copy` remuxing for compatible containers and a rotation display-matrix finalizer |
| `validators.py` | exit/existence/size/full-decode checks plus photo metadata; `verify_video_streams` dispatches lazily to named per-concern checks (audio tracks, subtitles, artwork, chapters, video format) |
| `progress.py` | concurrent FFmpeg progress plus cooperative cancellation and child termination |
| `safety.py` | source snapshots, link/readability policy, output writability and aggregate per-filesystem free-space reservations |
| `journal.py` | atomic `.mediate-run.json` state and interrupted-job prioritization |
| `transaction.py` | durable two-phase validated-output replacement plus startup rollback/completion recovery |
| `capabilities.py` | FFmpeg/cwebp version, encoder/demuxer, progress, rotation, smoke-encode, and ffprobe JSON preflight |
| `disposal.py` | serializable Trash (macOS per-volume `.Trashes`, freedesktop elsewhere) / graveyard / hard-delete policy; `_same_volume` is a seam so tests can exercise the external-drive branch |
| `macmeta.py` | ctypes `setattrlist(2)` to copy the original's birthtime (Finder "date created") onto outputs; no-op off macOS |
| `exiftool.py` | pool of up to 4 persistent `exiftool -stay_open` daemons behind `run_exiftool(args)` (per-thread, atexit-stopped, one-shot fallback); all exiftool queries go through it |
| `renamer.py` | `--rename`/`--rename-only` phase: stem parsing (paren/bracket/dash numbers, copy markers, `[site N]` tags, websites), cleanup + title case, per-(dir, base, site, ext) series renumbering compacted to 1 with gap-closing and zero-padding, GUID/random-token→folder-name, `--date-prefix`, `--rename-folders`, manifest + `--undo-renames`, never-overwrite apply loop |

## The safety pipeline (order matters)

`process_job` in `converters.py`:

1. Probe-based skips (standard/HEVC mp4, static gif, HEIC without `--convert-heic`).
1a. Before altering a video, inventory every stream, stream group, and chapter. All audio
    tracks (including commentary) are mapped; chapters, rotation, and colour
    are preserved. Compatible text subtitles and cover streams are mapped;
    incompatible streams cause a safety skip unless `--allow-stream-removal`.
    HDR/high-bit-depth (including SMPTE 2094-50), LCEVC, alpha, interlaced,
    and unusual/VFR video is blocked
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
  planning-time; later duplicates become SKIPPED outcomes. The claim **must**
  be computed with the run's `--output-format`: a claimed name is stored on
  the job and used verbatim as the final path, so claiming with the wrong
  extension both mis-names the output and stops real collisions being seen.
  `tests/test_cli_main.py` guards this for both formats.
- **HEIC pipeline must use a PNG intermediate** (`_decode_heic` in
  `converters.py`): `sips → PNG → cwebp` on macOS, and FFmpeg's mov/mp4
  demuxer (HEIC is ISOBMFF carrying HEVC — there is no separate "heic"
  demuxer to look for) elsewhere. Both preserve the EXIF block; anything a
  decoder drops is restored by `_repair_photo_metadata`. Because the mov
  demuxer exposes *every* auxiliary image as a stream, the largest-area
  stream is mapped explicitly and the decoded PNG's IHDR dimensions are
  checked against it — otherwise a thumbnail or depth map would silently
  replace the photo, which nothing downstream would catch. sips copies EXIF into PNG and cwebp extracts it; with a
  TIFF intermediate cwebp prints "EXIF extraction from TIFF is unsupported" and
  silently drops all metadata (the bug that shaped this design). cwebp can't
  read HEIC at all (HEVC-compressed stills, patent-encumbered).
- **Bundle guard is deliberately non-overridable** (`scanner.py`): directories
  with `.photoslibrary`/`.app`/`.fcpbundle`-style suffixes are pruned from
  `os.walk`. `Photos Library.photoslibrary` is *not* hidden — without this,
  pointing mediate at `~/Pictures` would convert/delete Apple Photos' masters.
- **Live Photo protection covers both halves** (`find_live_photo_companions` +
  `cli.py`): same-stem `.mov`/still files are candidates, but matching Apple
  `ContentIdentifier` metadata is required when ExifTool is available.
  Converting *either* verified half breaks the pairing, so both are skipped
  unless `--convert-live-photos`. Without ExifTool, naming remains the
  conservative fallback.
- **HEVC MP4s are skipped by default**: re-encoding HEVC→h264 at crf 18 *grows*
  the file (verified 7.6 KB → 11.3 KB on a test clip) and Apple plays HEVC
  natively. `--reencode-hevc` opts into the size hit for non-Apple targets.
- **A container's declared duration can be a lie** (`verify_video_duration`):
  concatenated MPEG program streams — how a ripped DVD's `VTS_01_N.VOB`
  parts are normally joined — restart their timestamps at each join, so
  ffprobe reports only the final segment (3.9s for a 10s file) and packet
  spans are fooled identically. Only a decode is reliable, so
  `decoded_duration()` is consulted **only** when validation would otherwise
  fail *and* the output is longer than the source claims: a conversion cannot
  invent content, but a container can understate itself. A short output is
  still genuine truncation and fails on the cheap comparison without paying
  for a decode. The same measurement settles per-audio-track durations.
- **Validation requires empty stderr, not just exit 0**, on the video integrity
  pass — ffmpeg reports many corruptions on stderr while still exiting 0.
- **Container signalling is exempt from removal risks** (`probe.py`):
  `dvd_nav_packet` (every ripped DVD VOB), `epg`, and `scte_35` are data
  streams describing seek points/menus or broadcast splicing. They exist only
  inside their source container, have no MP4 representation, and carry no
  content — so, like the MOV chapter carrier, they are not counted as
  removable. Counting them made *every* VOB need `--allow-stream-removal`,
  which would then also have permitted discarding real subtitles. DVD bitmap
  subtitles (`dvd_subtitle`) still block: MP4 cannot carry VOBSUB, so that
  loss is real and stays opt-in.
- **Disposal tests must never touch the real Trash** (`test_disposal.py`):
  every test redirects `HOME`/`XDG_DATA_HOME` into a temp dir, because
  `Path.home()` reads `HOME` and an unpatched run would move fixtures into
  the developer's own `~/.Trash`. Trash is the *default* mode, so this is the
  path an ordinary invocation takes.
- **Stream-inventory failures fail closed**: a video is skipped because it is
  unsafe to alter without knowing what it contains. Narrow codec/GIF probes
  still fail open into conversion/validation where no destructive stream
  selection can occur before the full video preflight.
- **Animated WebP is FFmpeg 9 capability-gated**: the scanner reads only the
  VP8X feature byte, so static WebPs remain ignored. Animation conversion is
  attempted only when FFmpeg exposes the native `webp_anim` demuxer; alpha and
  ICC/EXIF/XMP loss require the existing explicit downgrade/removal flags.
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
- **exiftool arguments never carry a newline into the daemon**
  (`exiftool.py`): `-@ -` is strictly one argument per line, so a path
  containing `\n` (legal on APFS) would split into two exiftool arguments —
  and `_repair_photo_metadata` passes `-overwrite_original`. Such calls fall
  back to a one-shot argv invocation, where the OS passes each argument
  intact. The daemon also *checks* its pipes instead of asserting them:
  `python -O` strips `assert`, and the resulting `AttributeError` is not what
  callers catch.
- **Unnumbered same-base files join the series** (`_assign`): "a b", "a_b"
  and "a-b" all clean to "A B" and would target one path, where the
  never-overwrite loop skips all but one — indistinguishable from the tool
  doing nothing. Extras are numbered instead. The member already carrying the
  final name keeps it, so an already-standardized library does not churn.
- **The ancestor check in `apply_renames` must stay a set lookup**: the
  condition is "p.src is a strict ancestor of a pending source", so the
  ancestors are collected once per round. Scanning every source per plan is
  quadratic — measured at 12s for 1,600 renames, extrapolating past three
  hours for a 50k library before a single file moves.
- **The rename manifest is written atomically** (`_write_manifest`): it is
  the only record that makes renames reversible. `undo_last_batch` also
  survives a single unrestorable entry, and keeps the batch recorded when
  any entry failed so the remainder can be retried.
- **The renamer skips symlinks** (`_walk_files`), matching the converter's
  refusal of symlinked media. Renaming a link would move a pointer whose
  target may live anywhere, and a broken link would be quietly tidied.
- **Names are trimmed to 255 bytes** (`fit_within_name_max`): the filesystem
  limit is bytes, not characters, so trimming happens on the encoded form
  without splitting a character. The trailing `[N]` tag and any
  `--date-prefix` are preserved and the descriptive middle is shortened —
  dropping the tag would collapse two long series members onto one name.
- **Invisible formatting is stripped, joiners are not** (`INVISIBLE_RE`):
  zero-width space, LTR/RTL marks, directional overrides, word joiner and BOM
  are scrape residue. U+200C/U+200D are deliberately excluded — they join
  visible glyphs in Indic scripts and emoji, so stripping them would split a
  family emoji into three people.
- **Renamer gap-closing needs the deferred-apply loop** (`apply_renames`):
  `[2]→[1], [3]→[2]` — the second rename's target is occupied until the first
  happens. Renames whose target is another pending rename's source wait a
  round; anything still blocked when a full round makes no progress is skipped
  (never overwritten). `samefile()` distinguishes a real collision from a
  case-only rename on case-insensitive APFS (`exists()` lies there).
- **Meaningful punctuation is protected before word cleanup** (`clean_base`):
  a dot after a *single* letter is an initialism (`R.E.M.`, `e.e.`) and is
  kept, then uppercased — a dot after a longer run stays a separator, so
  `Mr. Smith` still flattens. A stem starting `YYYY-MM-DD` additionally keeps
  digit-dot-digit (a clock), but is *not* exempted from title-casing: the
  words around the timestamp are still tidied. Both protections work by
  sentinel substitution around the existing `[_.]+` pass.
- **A parenthesised 1900–2099 number is a year, not a counter** (`parse_stem`):
  `The Matrix (1999)` used to become `The Matrix [1]`, destroying the year.
  Only `(N)` is exempted — `[1999]` stays a tag and dash-numbers are
  unaffected — so ordinary `photo (1)` duplicates still fold.
- **Hyphens between capitalised words are deliberately NOT protected**: once
  numbering is stripped, `Anne-Marie` is structurally identical to
  `Tilly-Marsh`, and dash-as-separator is the common case. Keeping them would
  turn `Tilly-Marsh-001` into `Tilly-Marsh [1]`.
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
  When ExifTool is installed, only stem-pairs with equal, non-empty
  `ContentIdentifier`s are protected; missing or different identifiers mean
  the files are not a verified Live Photo pair.
- **Renamer parse-order matters**: recognized tags (`[N]`, `[site N]`,
  `[site]`) parse first; any *other* trailing `[…]` marks the name opaque
  (already standardized, e.g. a GUID tag) and only the extension case is
  touched — that's what makes re-runs idempotent. Dash-numbers require a
  non-digit before the dash (`Tilly-Marsh-001` numbers, `2023-01-05` does
  not); a bare space-number (`Terminator 2`) is never numbering. `SITE_RE`
  domain labels deliberately exclude dashes — in filenames a dash is a
  separator, not part of a hyphenated domain.
- **Padding tracks the current series size** — width is the digit count of
  the largest number, so a series shrinking below 10 unpads on the next run
  and one passing 99 widens to 3. A flat width of 2 put `[100]` lexically
  between `[09]` and `[10]`, which is the one thing padding exists to
  prevent. Numbering always compacts to start at 1.
- **Rename manifest**: every applied batch (files then folders, in execution
  order) is appended to `.mediate-renames.json` at the root; undo replays it
  reversed, so folder renames undo before the files inside them. Folder
  plans are applied only after all file renames resolved — file plan paths
  are computed against pre-rename folder names.
- **Probe results are cached** (`probe.py`: path+mtime_ns+ctime_ns+size+device+
  inode keyed JSON; health adds a sampled BLAKE2 fingerprint) in the user cache
  dir, loaded/saved by cli — a 50k-file re-run would otherwise
  spawn ffprobe per MP4/GIF/WebP animation. `load_probe_cache()` must run before the pool.
  The save is a temp-file + `os.replace`, like the journal and transaction
  manifests: a truncated cache silently discards expensive
  `--validate-existing` decode results.
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
- **CI** (`.github/workflows/ci.yml`): unit tests on ubuntu+macos x Python
  3.9 (the `requires-python` floor) and 3.12 for every push/PR; a `v*` tag
  additionally builds `mediate.pyz` (stdlib zipapp) and creates the GitHub
  Release with `--generate-notes`. Releasing = bump version in
  `__init__.py`/`pyproject.toml`, tag, push the tag — the release job now
  **fails** if the tag disagrees with either file, or if the built zipapp's
  `--version` disagrees with the tag.
- **Homebrew tap** (`eddysant/homebrew-tap`, sibling checkout at
  `~/Code/homebrew-tap`): `Formula/mediate.rb` wraps the release source
  tarball (libexec + PYTHONPATH bin shim on brewed python; ffmpeg/webp as
  deps, exiftool in caveats). **The bump is automatic and lives in the tap**,
  not here: `.github/workflows/update-mediate-formula.yml` there reads this
  repo's latest release on a daily schedule and commits the new `url`/`sha256`
  to its own repository with the built-in `github.token`. Nothing needs to be
  done after tagging; `gh workflow run "Update mediate Formula" --repo
  eddysant/homebrew-tap` forces it immediately.
  This replaced a `tap-update` job here that pushed cross-repo with a
  `TAP_TOKEN` PAT. That failed every `v*` build with a 403 — and the failure
  hid for a while because the tap is public, so the *checkout* succeeded with
  a token that had no write access. Pulling from the tap side needs no PAT at
  all, which is why the job is gone rather than repaired.

## Testing

- `python3 -m unittest discover tests` — pure-Python scanner/rename/probe tests
  plus `tests/test_cli_main.py`, which drives `cli.main()` end to end (output
  claiming per format, exit codes, rename/undo/plan phases, handler
  idempotence) and `classify_job()` directly, and asserts every `mediate/*.py`
  still parses under the declared 3.9 floor
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

*(none currently recorded)*
