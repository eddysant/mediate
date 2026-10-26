"""One real run of `cli.main()` over a real library, start to finish.

Everything else is tested in pieces: `test_cli_main.py` drives `main()` with
conversion stubbed out, and `test_ffmpeg_integration.py` calls `process_job`
directly on real media. Neither exercises the whole chain — scan, plan,
convert, validate, transact, dispose, journal — in one invocation, and that
chain is precisely the one whose failure mode is a lost file.

So these tests build a small library of genuine media, run the CLI over it
the way a person would, and then assert on the filesystem: the outputs are
real and playable, every original is accounted for in the graveyard, the
files that should have been left alone are byte-identical, and nothing
half-finished is left behind.

Disposal goes to a graveyard under the temporary directory, and `HOME` is
redirected regardless, so neither the developer's Trash nor their probe
cache is touched.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediate import cli
from mediate.probe import video_health
from mediate.inventory import inventory_streams, primary_video_streams, video_inventory
from mediate.transaction import TRANSACTION_PREFIX


def _ffmpeg_has_encoder(name: str) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    )
    return proc.returncode == 0 and name in proc.stdout


@unittest.skipUnless(
    shutil.which("ffmpeg")
    and shutil.which("ffprobe")
    and shutil.which("cwebp")
    and _ffmpeg_has_encoder("libx264")
    and _ffmpeg_has_encoder("aac"),
    "FFmpeg, ffprobe, cwebp, libx264 and aac are required for end-to-end tests",
)
class EndToEndRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "library"
        self.graveyard = base / "graveyard"
        self.home = base / "home"
        self.log = base / "conversion.log"
        for directory in (self.root, self.root / "nested", self.home):
            directory.mkdir(parents=True)

        # Neither the real Trash nor the real probe cache may be involved:
        # both are reached through Path.home().
        environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.home / "data"),
                "XDG_CACHE_HOME": str(self.home / "cache"),
                "MEDIATE_CONFIG": "/nonexistent",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

        self.photo = self.root / "snapshot.jpg"
        self.legacy = self.root / "nested" / "camcorder.avi"
        self.ready = self.root / "nested" / "already-h264.mkv"
        self.standard = self.root / "standard.mp4"
        self.build_library()

    def ffmpeg(self, *args: str) -> None:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", *map(str, args)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def require_encoders(self, *names: str) -> None:
        missing = [name for name in names if not _ffmpeg_has_encoder(name)]
        if missing:
            self.skipTest(f"FFmpeg encoder(s) unavailable: {', '.join(missing)}")

    def build_library(self):
        self.require_encoders("mpeg4", "ac3", "mjpeg")
        # A photo, in a nested-free spot, with a capture date to preserve.
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=1:duration=1",
            "-frames:v", "1", "-c:v", "mjpeg", self.photo,
        )
        if shutil.which("exiftool"):
            subprocess.run(
                ["exiftool", "-overwrite_original",
                 "-EXIF:DateTimeOriginal=2019:07:04 11:22:33", str(self.photo)],
                capture_output=True, text=True, check=True,
            )
        # A legacy container needing a real encode of both streams.
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-c:a", "ac3", self.legacy,
        )
        # Already h264/yuv420p + AAC, but in Matroska: a remux, not an encode.
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", self.ready,
        )
        # Already standard: must not be touched at all.
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", self.standard,
        )

    def run_cli(self, *extra: str) -> int:
        return cli.main([str(self.root), "--log-file", str(self.log), *extra])

    def assertNothingHalfFinished(self):
        """No temp outputs, and no transaction left pending anywhere."""
        partials = [p.name for p in self.root.rglob(".*.part.*")]
        self.assertEqual(partials, [], f"temp outputs left behind: {partials}")
        pending = [
            p.name for p in self.root.rglob(f"{TRANSACTION_PREFIX}*") if p.is_dir()
        ]
        self.assertEqual(pending, [], f"transactions left pending: {pending}")

    def photo_capture_dates(self, path: Path) -> set:
        from mediate.validators import _photo_capture_dates

        return _photo_capture_dates(path)

    def test_a_dry_run_changes_nothing_at_all(self):
        before = {p: p.stat().st_mtime_ns for p in sorted(self.root.rglob("*"))}

        self.assertEqual(self.run_cli("--dry-run", "--graveyard", str(self.graveyard)), 0)

        after = {p: p.stat().st_mtime_ns for p in sorted(self.root.rglob("*"))}
        self.assertEqual(after, before, "a dry run modified the library")
        self.assertFalse(self.graveyard.exists(), "a dry run created the graveyard")
        self.assertNothingHalfFinished()

    def test_a_real_run_converts_disposes_and_leaves_nothing_behind(self):
        self.assertEqual(self.run_cli("--graveyard", str(self.graveyard), "--workers", "2"), 0)

        photo_out = self.root / "snapshot.webp"
        legacy_out = self.legacy.with_suffix(".mp4")
        ready_out = self.ready.with_suffix(".mp4")

        # Every conversion produced its intended output, and the originals
        # moved to the graveyard at their original relative paths.
        for source, output in (
            (self.photo, photo_out),
            (self.legacy, legacy_out),
            (self.ready, ready_out),
        ):
            with self.subTest(source=source.name):
                self.assertTrue(output.exists(), f"{output.name} was not produced")
                self.assertFalse(source.exists(), f"{source.name} was not disposed of")
                relative = source.relative_to(self.root)
                self.assertTrue(
                    (self.graveyard / relative).exists(),
                    f"{relative} is not in the graveyard",
                )

        # The videos are genuinely standard and genuinely decodable.
        for output in (legacy_out, ready_out):
            with self.subTest(output=output.name):
                self.assertTrue(video_health(output)["ok"])
                inventory = video_inventory(output)
                video = primary_video_streams(inventory)[0]
                self.assertEqual(video["codec_name"], "h264")
                self.assertEqual(video["pix_fmt"], "yuv420p")
                audio = inventory_streams(inventory, "audio")
                self.assertEqual(len(audio), 1)
                self.assertEqual(audio[0]["codec_name"], "aac")

        if shutil.which("exiftool"):
            self.assertIn(
                "2019-07-04 11:22:33",
                self.photo_capture_dates(photo_out),
                "the photo's capture date did not survive the conversion",
            )

        self.assertNothingHalfFinished()
        self.assertTrue(self.log.exists() and self.log.stat().st_size > 0)
        self.assertTrue((self.root / ".mediate-run.json").exists())

    def test_a_converted_photo_is_pixel_identical_to_its_source(self):
        """Lossless must mean lossless, measured on the pixels themselves.

        mediate disposes of the original, so "lossless WebP" is a promise
        about the only remaining copy. It was once broken silently: cwebp's
        -preset resets the parameters it follows, so `-lossless ... -preset
        photo` wrote a lossy VP8 file that passed every check there was.
        This test decodes the output and compares it to the source, which is
        the one assertion that cannot be satisfied by a lossy encode.
        """
        if not shutil.which("dwebp"):
            self.skipTest("dwebp is required to decode the output for comparison")
        # A PNG source, so there is no JPEG decode to make the comparison
        # ambiguous: the bytes going in are the pixels to get back.
        source = self.root / "lossless-me.png"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=1:duration=1",
            "-frames:v", "1", source,
        )

        self.assertEqual(self.run_cli("--graveyard", str(self.graveyard)), 0)

        output = source.with_suffix(".webp")
        self.assertTrue(output.exists(), "the photo was not converted")
        decoded = self.root / "decoded.png"
        subprocess.run(
            ["dwebp", "-quiet", str(output), "-o", str(decoded)],
            capture_output=True, text=True, check=True,
        )
        original = self.graveyard / "lossless-me.png"
        self.assertTrue(original.exists(), "the source was not disposed of")
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(original),
             "-i", str(decoded), "-lavfi", "[0:v][1:v]psnr=stats_file=-",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "psnr_avg:inf", proc.stdout,
            f"the conversion was not lossless: {proc.stdout.strip()}",
        )

    def test_an_already_standard_mp4_is_left_byte_identical(self):
        before = self.standard.read_bytes()
        before_mtime = self.standard.stat().st_mtime_ns

        self.assertEqual(self.run_cli("--graveyard", str(self.graveyard)), 0)

        self.assertTrue(self.standard.exists(), "a standard MP4 was disposed of")
        self.assertEqual(self.standard.read_bytes(), before)
        self.assertEqual(self.standard.stat().st_mtime_ns, before_mtime)
        self.assertFalse((self.graveyard / "standard.mp4").exists())

    def test_a_second_run_finds_no_work_and_touches_nothing(self):
        # The repeat-run gotcha: a converted library must look finished, not
        # like a library full of newly discovered candidates.
        self.assertEqual(self.run_cli("--graveyard", str(self.graveyard)), 0)
        state = {
            p.relative_to(self.root): p.stat().st_mtime_ns
            for p in sorted(self.root.rglob("*"))
            if p.name != ".mediate-run.json"
        }

        self.assertEqual(self.run_cli("--graveyard", str(self.graveyard)), 0)

        again = {
            p.relative_to(self.root): p.stat().st_mtime_ns
            for p in sorted(self.root.rglob("*"))
            if p.name != ".mediate-run.json"
        }
        self.assertEqual(again, state, "a second run altered an already-converted library")
        self.assertNothingHalfFinished()


if __name__ == "__main__":
    unittest.main()
