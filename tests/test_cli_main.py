"""Coverage for cli.main() itself.

The individual helpers around main() are tested elsewhere; this file drives
the orchestration — planning-time name claiming, capability gating, exit
codes, and the rename phases — because that is where a bug can silently
mis-name or clobber a file that has already been disposed of.
"""

import ast
import io
import json
import logging
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from mediate import cli
from mediate.converters import Options, Outcome, SKIPPED, classify_job


def run_main(argv):
    """Run main() with output captured, returning (exit_code, stdout)."""
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            code = cli.main(argv)
    finally:
        for handler in list(cli.log.handlers):
            cli.log.removeHandler(handler)
            handler.close()
    return code, buffer.getvalue()


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict("os.environ", {"MEDIATE_CONFIG": "/nonexistent"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def touch(self, rel: str, data: bytes = b"x") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def dry_run(self, *extra):
        code, out = run_main([str(self.root), "--dry-run", *extra])
        planned = {}
        for line in out.splitlines():
            if line.startswith("[dry]"):
                name, _, target = line.partition(" -> ")
                planned[name.split()[1]] = target.strip()
        return code, planned, out


class OutputNamingTests(MainTestCase):
    """The planning-time claim must name the file process_job will produce."""

    def test_avif_outputs_are_claimed_with_an_avif_extension(self):
        # Regression: the claim used to be computed with the default webp
        # extension regardless of --output-format, so an AVIF encode was
        # written into a .webp name.
        self.touch("a.jpg")
        _, planned, _ = self.dry_run("--output-format", "avif")
        self.assertEqual(planned["a.jpg"], "a.avif")

    def test_avif_collisions_are_separated_before_the_pool_starts(self):
        self.touch("a.jpg")
        self.touch("a.png")
        _, planned, _ = self.dry_run("--output-format", "avif")
        targets = set(planned.values())
        self.assertEqual(len(targets), 2, f"outputs collided: {planned}")
        for target in targets:
            self.assertTrue(target.endswith(".avif"), target)

    def test_webp_collisions_are_separated_before_the_pool_starts(self):
        self.touch("a.jpg")
        self.touch("a.png")
        _, planned, _ = self.dry_run()
        targets = set(planned.values())
        self.assertEqual(len(targets), 2, f"outputs collided: {planned}")
        for target in targets:
            self.assertTrue(target.endswith(".webp"), target)

    def test_an_existing_output_name_is_not_claimed_twice(self):
        self.touch("a.jpg")
        self.touch("a.webp")
        _, planned, _ = self.dry_run()
        self.assertNotEqual(planned["a.jpg"], "a.webp")


class ExitCodeTests(MainTestCase):
    def test_missing_directory_is_a_usage_error(self):
        code, out = run_main([str(self.root / "nope"), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("not a directory", out)

    def test_empty_library_succeeds(self):
        code, _, out = self.dry_run()
        self.assertEqual(code, 0)
        self.assertIn("nothing to convert", out)

    def test_dry_run_never_touches_the_original(self):
        source = self.touch("a.jpg")
        code, _, _ = self.dry_run()
        self.assertEqual(code, 0)
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "a.webp").exists())

    def test_bundles_are_not_traversed(self):
        self.touch("Photos Library.photoslibrary/Masters/a.jpg")
        _, planned, _ = self.dry_run()
        self.assertEqual(planned, {})


class WorkerArgumentTests(MainTestCase):
    def test_non_numeric_workers_is_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            with redirect_stderr(io.StringIO()):
                cli.parse_args([str(self.root), "--workers", "eight"])
        self.assertEqual(caught.exception.code, 2)

    def test_zero_workers_is_rejected(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                cli.parse_args([str(self.root), "--workers", "0"])

    def test_auto_and_integers_are_accepted(self):
        self.assertEqual(cli.parse_args([str(self.root)]).workers, "auto")
        self.assertEqual(
            cli.parse_args([str(self.root), "--workers", "4"]).workers, "4"
        )


class RenamePhaseTests(MainTestCase):
    def test_rename_only_renames_without_converting(self):
        source = self.touch("misty vale (1).jpg")
        code, out = run_main([str(self.root), "--rename-only"])
        self.assertEqual(code, 0)
        self.assertFalse(source.exists())
        self.assertTrue((self.root / "Misty Vale [1].jpg").exists())
        self.assertIn("renamed", out)

    def test_undo_restores_the_previous_names(self):
        self.touch("misty vale (1).jpg")
        run_main([str(self.root), "--rename-only"])
        code, _ = run_main([str(self.root), "--undo-renames"])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "misty vale (1).jpg").exists())

    def test_plan_file_is_written_instead_of_applying(self):
        source = self.touch("misty vale (1).jpg")
        plan = self.root / "plan.json"
        code, _ = run_main([str(self.root), "--rename-only", "--plan-file", str(plan)])
        self.assertEqual(code, 0)
        self.assertTrue(source.exists(), "plan mode must not rename")
        self.assertTrue(plan.exists())
        code, _ = run_main([str(self.root), "--apply-plan", str(plan)])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "Misty Vale [1].jpg").exists())

    def test_a_bad_plan_path_is_a_usage_error(self):
        code, out = run_main([str(self.root), "--apply-plan", str(self.root / "no.json")])
        self.assertEqual(code, 2)
        self.assertIn("cannot load plan", out)


class LoggingTests(MainTestCase):
    def test_repeated_runs_do_not_duplicate_handlers(self):
        self.touch("a.jpg")
        for _ in range(3):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli.main([str(self.root), "--dry-run"])
        self.assertEqual(len(cli.log.handlers), 2)  # one console, one file
        for handler in list(cli.log.handlers):
            cli.log.removeHandler(handler)
            handler.close()

    def test_a_log_file_is_written_to_the_requested_path(self):
        self.touch("a.jpg")
        log_path = self.root / "custom.log"
        run_main([str(self.root), "--dry-run", "--log-file", str(log_path)])
        self.assertTrue(log_path.exists())
        self.assertIn("scanning", log_path.read_text())


class ClassifyJobTests(MainTestCase):
    """The skip gates, exercised without producing a temp file or an encode."""

    def job(self, path, kind):
        from mediate.scanner import MediaJob

        return MediaJob(path, kind)

    def test_heic_is_skipped_without_the_opt_in(self):
        decision = classify_job(self.job(self.touch("a.heic"), "heic"), Options())
        self.assertIsInstance(decision, Outcome)
        self.assertEqual(decision.status, SKIPPED)
        self.assertIn("--convert-heic", decision.detail)

    def test_opted_in_heic_produces_a_plan_on_macos(self):
        path = self.touch("a.heic")
        with patch.object(sys, "platform", "darwin"):
            decision = classify_job(self.job(path, "heic"), Options(convert_heic=True))
        self.assertNotIsInstance(decision, Outcome)
        self.assertEqual(decision.kind, "heic")

    def test_opted_in_heic_uses_ffmpeg_off_macos(self):
        path = self.touch("a.heic")
        with patch.object(sys, "platform", "linux"), \
                patch("mediate.converters.ffmpeg_can_decode_heic", return_value=True):
            decision = classify_job(self.job(path, "heic"), Options(convert_heic=True))
        self.assertNotIsInstance(decision, Outcome)
        self.assertEqual(decision.kind, "heic")

    def test_opted_in_heic_is_skipped_without_any_decoder(self):
        path = self.touch("a.heic")
        with patch.object(sys, "platform", "linux"), \
                patch("mediate.converters.ffmpeg_can_decode_heic", return_value=False):
            decision = classify_job(self.job(path, "heic"), Options(convert_heic=True))
        self.assertIsInstance(decision, Outcome)
        self.assertIn("HEIC", decision.detail)

    def test_a_static_gif_is_reclassified_as_a_photo(self):
        path = self.touch("a.gif")
        with patch("mediate.converters.gif_is_animated", return_value=False):
            decision = classify_job(self.job(path, "gif"), Options())
        self.assertEqual(decision.kind, "photo")
        self.assertEqual(decision.job.kind, "photo")

    def test_a_static_webp_is_skipped(self):
        path = self.touch("a.webp")
        info = {"animated": False, "alpha": False, "icc": False, "exif": False, "xmp": False}
        with patch("mediate.converters.webp_animation_info", return_value=info):
            decision = classify_job(self.job(path, "webp"), Options())
        self.assertIsInstance(decision, Outcome)
        self.assertIn("static WebP", decision.detail)

    def test_animated_webp_metadata_loss_needs_the_flag(self):
        path = self.touch("a.webp")
        info = {"animated": True, "alpha": False, "icc": True, "exif": False, "xmp": False}
        with patch("mediate.converters.webp_animation_info", return_value=info):
            blocked = classify_job(self.job(path, "webp"), Options())
            allowed = classify_job(
                self.job(path, "webp"), Options(allow_stream_removal=True)
            )
        self.assertIsInstance(blocked, Outcome)
        self.assertIn("--allow-stream-removal", blocked.detail)
        self.assertNotIsInstance(allowed, Outcome)

    def test_animated_webp_alpha_needs_the_downgrade_flag(self):
        path = self.touch("a.webp")
        info = {"animated": True, "alpha": True, "icc": False, "exif": False, "xmp": False}
        with patch("mediate.converters.webp_animation_info", return_value=info):
            blocked = classify_job(self.job(path, "webp"), Options())
            allowed = classify_job(
                self.job(path, "webp"), Options(allow_video_downgrade=True)
            )
        self.assertIsInstance(blocked, Outcome)
        self.assertIn("alpha", blocked.detail)
        self.assertNotIsInstance(allowed, Outcome)

    def test_a_failed_stream_preflight_fails_closed(self):
        path = self.touch("a.mov")
        with patch("mediate.converters.video_inventory", return_value=None):
            decision = classify_job(self.job(path, "video"), Options())
        self.assertIsInstance(decision, Outcome)
        self.assertIn("preflight failed", decision.detail)


class ExtractedSeamTests(MainTestCase):
    """The pieces main() delegates to, exercised without running a whole scan."""

    def test_summarize_run_is_stable(self):
        from mediate.cli import summarize_run
        from mediate.converters import (
            CONVERTED, FAILED, PLANNED, REMUXED, REPAIRED, SKIPPED,
        )

        def tally(**kw):
            base = {
                CONVERTED: 0, REMUXED: 0, REPAIRED: 0,
                SKIPPED: 0, FAILED: 0, PLANNED: 0,
            }
            base.update(kw)
            return base

        self.assertEqual(
            summarize_run(tally(**{CONVERTED: 2, FAILED: 1}), 7_235_000, False),
            "done: 2 converted, 0 skipped, 1 failed, 6.9 MB saved",
        )
        self.assertEqual(
            summarize_run(tally(**{PLANNED: 2}), 0, True),
            "dry run complete: 2 would be converted, 0 skipped",
        )
        # Zero-count stages stay out of the line; skipped/failed always show.
        self.assertNotIn("remuxed", summarize_run(tally(**{CONVERTED: 1}), 0, False))

    def test_plan_jobs_claims_one_name_per_output(self):
        from mediate.cli import plan_jobs
        from mediate.scanner import MediaJob

        jobs = [
            MediaJob(self.touch("a.jpg"), "photo"),
            MediaJob(self.touch("a.png"), "photo"),
        ]
        runnable, skips = plan_jobs(jobs, output_format="webp", convert_live_photos=False)
        self.assertEqual(skips, [])
        outputs = {
            (job.output or job.path.with_suffix(".webp")) for job in runnable
        }
        self.assertEqual(len(outputs), 2)

    def test_plan_jobs_protects_both_live_photo_halves(self):
        from mediate.cli import plan_jobs
        from mediate.scanner import MediaJob

        still = self.touch("IMG_1.heic")
        mov = self.touch("IMG_1.mov")
        jobs = [MediaJob(still, "heic"), MediaJob(mov, "video")]
        with patch("mediate.cli.find_live_photo_companions", return_value={mov: still}):
            runnable, skips = plan_jobs(
                jobs, output_format="webp", convert_live_photos=False
            )
        self.assertEqual(runnable, [])
        self.assertEqual({o.path for o in skips}, {still, mov})
        for outcome in skips:
            self.assertIn("Live Photo", outcome.detail)

    def test_plan_jobs_honours_the_live_photo_opt_in(self):
        from mediate.cli import plan_jobs
        from mediate.scanner import MediaJob

        jobs = [
            MediaJob(self.touch("IMG_1.heic"), "heic"),
            MediaJob(self.touch("IMG_1.mov"), "video"),
        ]
        runnable, skips = plan_jobs(jobs, output_format="webp", convert_live_photos=True)
        self.assertEqual(len(runnable), 2)
        self.assertEqual(skips, [])

    def test_build_options_carries_the_flags_through(self):
        from mediate.cli import build_options

        args = cli.parse_args([
            str(self.root), "--only-if-smaller", "--reencode-hevc",
            "--output-format", "avif",
        ])
        opts, label = build_options(self.root, args)
        self.assertTrue(opts.only_if_smaller)
        self.assertTrue(opts.reencode_hevc)
        self.assertEqual(opts.output_format, "avif")
        self.assertEqual(opts.transaction_root, self.root)
        self.assertTrue(label)

    def test_rename_shortcuts_return_none_when_not_requested(self):
        from mediate.cli import run_rename_shortcuts

        args = cli.parse_args([str(self.root)])
        self.assertIsNone(run_rename_shortcuts(self.root, args))


class PythonFloorTests(unittest.TestCase):
    """pyproject declares >=3.9; keep the sources parseable there."""

    def test_sources_parse_under_the_declared_minimum(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for path in sorted(root.glob("mediate/*.py")):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(), str(path), feature_version=(3, 9))

    def test_the_packaged_version_matches_pyproject(self):
        import re

        import mediate

        root = pathlib.Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text()
        declared = re.search(r'^version = "([^"]+)"', text, re.MULTILINE).group(1)
        self.assertEqual(mediate.__version__, declared)


if __name__ == "__main__":
    unittest.main()
