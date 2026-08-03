import os
import tempfile
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mediate.journal import RunJournal
from mediate.converters import FAILED, Options, process_job
from mediate.progress import CANCELLATION, ConversionCancelled, run_ffmpeg_progress
from mediate.safety import (
    SafetyError,
    SourceSnapshot,
    ensure_output_capacity,
    required_temporary_space,
)
from mediate.scanner import MediaJob


class FilesystemSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_snapshot_rejects_symlinks_and_detects_source_mutation(self):
        source = self.root / "movie.mp4"
        source.write_bytes(b"abcdef")
        snapshot = SourceSnapshot.capture(source)
        self.assertTrue(snapshot.unchanged(source))
        source.write_bytes(b"ghijkl")
        self.assertFalse(snapshot.unchanged(source))

        link = self.root / "linked.mp4"
        link.symlink_to(source)
        with self.assertRaisesRegex(SafetyError, "symbolic-link"):
            SourceSnapshot.capture(link)

    def test_snapshot_rejects_hard_linked_media(self):
        source = self.root / "movie.mp4"
        alias = self.root / "alias.mp4"
        source.write_bytes(b"media")
        os.link(source, alias)
        with self.assertRaisesRegex(SafetyError, "hard-linked"):
            SourceSnapshot.capture(source)

    def test_disk_space_floor_and_failure(self):
        self.assertEqual(required_temporary_space(1), 64 * 1024 * 1024)
        with patch("mediate.safety.os.access", return_value=True), patch(
            "mediate.safety.shutil.disk_usage",
            return_value=SimpleNamespace(free=1024),
        ):
            with self.assertRaisesRegex(SafetyError, "insufficient free space"):
                ensure_output_capacity(self.root, 100 * 1024 * 1024)

    def test_conversion_output_is_discarded_if_source_changes_mid_run(self):
        source = self.root / "photo.jpg"
        source.write_bytes(b"source")

        def convert(_kind, _src, output, _inventory=None, repair=False):
            output.write_bytes(b"converted")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("mediate.converters._convert", side_effect=convert), patch(
            "mediate.converters.validate_output", return_value=(True, "ok")
        ), patch(
            "mediate.converters.verify_photo_metadata", return_value=(True, "ok")
        ), patch(
            "mediate.converters.SourceSnapshot.unchanged", return_value=False
        ):
            outcome = process_job(MediaJob(source, "photo"), Options(keep_originals=True))
        self.assertEqual(outcome.status, FAILED)
        self.assertIn("source changed during conversion", outcome.detail)
        self.assertTrue(source.exists())
        self.assertFalse(source.with_suffix(".webp").exists())


class JournalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_interrupted_files_are_prioritized_when_identity_matches(self):
        first = self.root / "first.mov"
        second = self.root / "second.mov"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        jobs = [MediaJob(first, "video"), MediaJob(second, "video")]

        journal = RunJournal(self.root)
        journal.prepare(jobs)
        journal.mark(jobs[1], "running")
        journal.finish(interrupted=True)

        resumed = RunJournal(self.root)
        ordered, count = resumed.prepare(jobs)
        self.assertEqual(count, 2)
        self.assertEqual(ordered, jobs)

    def test_changed_file_is_not_treated_as_the_same_interrupted_job(self):
        source = self.root / "movie.mov"
        source.write_bytes(b"old")
        job = MediaJob(source, "video")
        journal = RunJournal(self.root)
        journal.prepare([job])
        journal.finish(interrupted=True)
        source.write_bytes(b"new-content")

        _ordered, count = RunJournal(self.root).prepare([job])
        self.assertEqual(count, 0)


class CancellationTests(unittest.TestCase):
    def tearDown(self):
        CANCELLATION.reset()

    def test_request_terminates_every_registered_process(self):
        process = Mock()
        process.poll.side_effect = [None, 0]
        CANCELLATION.register(process)
        CANCELLATION.request()
        process.terminate.assert_called_once_with()
        CANCELLATION.unregister(process)

    def test_pre_cancelled_ffmpeg_does_not_start(self):
        CANCELLATION.request()
        with patch("mediate.progress.subprocess.Popen") as popen:
            with self.assertRaises(ConversionCancelled):
                run_ffmpeg_progress(
                    ["ffmpeg", "-f", "null", "-"],
                    Path("movie.mp4"),
                    1.0,
                    "encoding",
                )
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
