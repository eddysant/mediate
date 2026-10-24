import json
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

        def convert(_kind, _src, output, _inventory=None, repair=False, **_kw):
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

    def job(self, name: str = "movie.mov", content: bytes = b"x") -> MediaJob:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return MediaJob(path, "video")

    def state(self) -> dict:
        return json.loads((self.root / ".mediate-run.json").read_text())

    def test_a_clean_finish_is_distinguishable_from_an_interrupted_one(self):
        job = self.job()
        journal = RunJournal(self.root)
        journal.prepare([job])
        journal.mark(job, "converted")
        journal.finish(interrupted=False)

        state = self.state()
        self.assertFalse(state["interrupted"])
        self.assertIn("finished_at", state)
        self.assertEqual(state["files"]["movie.mov"]["status"], "converted")
        # A clean run leaves nothing to resume.
        self.assertEqual(RunJournal(self.root).prepare([job])[1], 0)

    def test_an_interrupted_run_reclassifies_unfinished_entries(self):
        done, running, queued = (
            self.job("done.mov"), self.job("running.mov"), self.job("queued.mov")
        )
        journal = RunJournal(self.root)
        journal.prepare([done, running, queued])
        journal.mark(done, "converted")
        journal.mark(running, "running")
        journal.finish(interrupted=True)

        files = self.state()["files"]
        self.assertEqual(files["done.mov"]["status"], "converted")
        # Both the in-flight job and the one never started must come back.
        self.assertEqual(files["running.mov"]["status"], "interrupted")
        self.assertEqual(files["queued.mov"]["status"], "interrupted")

    def test_a_failure_is_flushed_immediately_rather_than_throttled(self):
        # Writes are throttled to one a second so a large run does not thrash
        # the disk; a crash right after a failure would otherwise lose the one
        # status the next run most needs.
        job = self.job()
        journal = RunJournal(self.root)
        journal.prepare([job])
        journal.mark(job, "failed", "ffmpeg exited 1")

        entry = self.state()["files"]["movie.mov"]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["detail"], "ffmpeg exited 1")

    def test_a_corrupt_journal_is_discarded_rather_than_raising(self):
        # The file is rewritten constantly; a truncated one must cost the
        # resume information, not the run.
        (self.root / ".mediate-run.json").write_text("{not json")
        ordered, count = RunJournal(self.root).prepare([self.job()])
        self.assertEqual(count, 0)
        self.assertEqual(len(ordered), 1)

    def test_a_journal_from_a_future_version_is_ignored(self):
        # The identity matches, so only the version check can reject this; a
        # newer schema read as version 1 would mis-resume the whole library.
        job = self.job()
        stat = job.path.stat()
        (self.root / ".mediate-run.json").write_text(
            json.dumps({
                "version": 99,
                "files": {
                    "movie.mov": {
                        "status": "running",
                        "identity": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                    }
                },
            })
        )
        self.assertEqual(RunJournal(self.root).prepare([job])[1], 0)

    def test_preparing_clears_entries_for_files_no_longer_present(self):
        first = self.job("first.mov")
        journal = RunJournal(self.root)
        journal.prepare([first, self.job("second.mov")])
        journal.finish(interrupted=True)
        (self.root / "second.mov").unlink()

        RunJournal(self.root).prepare([first])
        self.assertEqual(list(self.state()["files"]), ["first.mov"])

    def test_a_file_outside_the_root_is_skipped_quietly(self):
        # plan files and --apply-plan can name a path the journal has no key
        # for; relative_to raises ValueError and must not reach the caller.
        outside = Path(self._tmp.name).parent / "stray-journal-test.mov"
        outside.write_bytes(b"x")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        stray = MediaJob(outside, "video")

        journal = RunJournal(self.root)
        ordered, count = journal.prepare([stray])
        journal.mark(stray, "converted")
        journal.finish(interrupted=False)

        self.assertEqual((ordered, count), ([stray], 0))
        self.assertEqual(self.state()["files"], {})

    def test_nested_paths_keep_their_relative_key(self):
        nested = self.job("2019/summer/movie.mov")
        journal = RunJournal(self.root)
        journal.prepare([nested])
        journal.finish(interrupted=False)
        self.assertIn(str(Path("2019/summer/movie.mov")), self.state()["files"])

    def test_a_disabled_journal_writes_nothing(self):
        job = self.job()
        journal = RunJournal(self.root, enabled=False)
        journal.prepare([job])
        journal.mark(job, "failed")
        journal.finish(interrupted=True)
        self.assertFalse((self.root / ".mediate-run.json").exists())

    def test_a_disabled_journal_ignores_an_existing_file(self):
        RunJournal(self.root).prepare([self.job()])
        self.assertEqual(RunJournal(self.root, enabled=False)._data["files"], {})

    def test_the_write_is_atomic_and_leaves_no_temp_behind(self):
        # Same temp-file + os.replace protocol as the probe cache and the
        # transaction manifest: a reader must never see a half-written file.
        journal = RunJournal(self.root)
        journal.prepare([self.job()])
        journal.finish(interrupted=False)
        leftovers = [p.name for p in self.root.glob(".mediate-run.json.*")]
        self.assertEqual(leftovers, [])
        self.assertIn("updated_at", self.state())


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
