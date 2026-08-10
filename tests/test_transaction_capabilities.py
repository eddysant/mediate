import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mediate.capabilities import check_media_capabilities
from mediate.disposal import DisposalPolicy, HARD
from mediate.safety import DiskSpaceReservations, SafetyError
from mediate.transaction import (
    ReplacementTransaction,
    TransactionError,
    recover_transactions,
)


class ReplacementTransactionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.policy = DisposalPolicy(HARD, self.root)

    def prepare(self, same_path=False):
        source = self.root / ("movie.mp4" if same_path else "movie.mov")
        source.write_bytes(b"original")
        final = source if same_path else self.root / "movie.mp4"
        output = self.root / ".movie.part.mp4"
        output.write_bytes(b"validated-output")
        transaction = ReplacementTransaction.prepare(
            self.root, source, final, output, self.policy
        )
        return transaction, source, final, output

    def test_crash_after_staging_rolls_back_original(self):
        transaction, source, final, output = self.prepare()

        def crash(state):
            if state == "source_staged":
                raise RuntimeError("simulated power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            transaction.commit(checkpoint=crash)
        self.assertFalse(source.exists())
        self.assertTrue(transaction.backup.exists())

        report = recover_transactions(self.root)
        self.assertEqual((report.rolled_back, report.unresolved), (1, 0))
        self.assertEqual(source.read_bytes(), b"original")
        self.assertFalse(final.exists())
        self.assertFalse(output.exists())

    def test_crash_after_install_finishes_recorded_disposal(self):
        transaction, source, final, _output = self.prepare()

        def crash(state):
            if state == "output_installed":
                raise RuntimeError("simulated power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            transaction.commit(checkpoint=crash)
        self.assertEqual(final.read_bytes(), b"validated-output")
        self.assertTrue(transaction.backup.exists())

        report = recover_transactions(self.root)
        self.assertEqual((report.completed, report.unresolved), (1, 0))
        self.assertEqual(final.read_bytes(), b"validated-output")
        self.assertFalse(source.exists())
        self.assertFalse(transaction.directory.exists())

    def test_same_path_repair_recovers_installed_output(self):
        transaction, source, final, _output = self.prepare(same_path=True)

        def crash(state):
            if state == "output_installed":
                raise RuntimeError("simulated power loss")

        with self.assertRaises(RuntimeError):
            transaction.commit(checkpoint=crash)
        report = recover_transactions(self.root)
        self.assertEqual((report.completed, report.unresolved), (1, 0))
        self.assertEqual(source, final)
        self.assertEqual(source.read_bytes(), b"validated-output")

    def test_disposal_failure_rolls_back_and_discards_output(self):
        transaction, source, final, output = self.prepare()

        def fail_disposal(_path, _original=None):
            raise OSError("trash unavailable")

        transaction.disposer = fail_disposal
        with self.assertRaisesRegex(TransactionError, "trash unavailable"):
            transaction.commit()
        self.assertEqual(source.read_bytes(), b"original")
        self.assertFalse(final.exists())
        self.assertFalse(output.exists())
        self.assertFalse(transaction.directory.exists())

    def test_source_replacement_after_prepare_is_never_staged(self):
        transaction, source, final, output = self.prepare()
        source.unlink()
        source.write_bytes(b"changed-behind-our-back")
        with self.assertRaisesRegex(TransactionError, "source changed"):
            transaction.commit()
        self.assertEqual(source.read_bytes(), b"changed-behind-our-back")
        self.assertFalse(final.exists())
        self.assertTrue(output.exists())
        self.assertFalse(transaction.directory.exists())

    def test_validated_output_content_change_is_rejected_even_at_same_size(self):
        transaction, source, final, output = self.prepare()
        output.write_bytes(b"X" * len(b"validated-output"))
        with self.assertRaisesRegex(TransactionError, "validated output changed"):
            transaction.commit()
        self.assertEqual(source.read_bytes(), b"original")
        self.assertFalse(final.exists())
        self.assertEqual(output.read_bytes(), b"X" * len(b"validated-output"))

    def test_prepared_transaction_is_safely_discarded(self):
        transaction, source, final, output = self.prepare()
        report = recover_transactions(self.root)
        self.assertEqual((report.rolled_back, report.unresolved), (1, 0))
        self.assertEqual(source.read_bytes(), b"original")
        self.assertFalse(final.exists())
        self.assertFalse(output.exists())
        self.assertFalse(transaction.directory.exists())


class DiskSpaceReservationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_second_worker_waits_for_same_filesystem_budget(self):
        manager = DiskSpaceReservations()
        free = 96 * 1024 * 1024
        acquired = threading.Event()
        with patch(
            "mediate.safety.shutil.disk_usage",
            return_value=SimpleNamespace(free=free),
        ):
            first = manager.acquire(self.root, 1)

            def acquire_second():
                with manager.acquire(self.root, 1):
                    acquired.set()

            worker = threading.Thread(target=acquire_second)
            worker.start()
            self.assertFalse(acquired.wait(0.1))
            first.release()
            self.assertTrue(acquired.wait(1.0))
            worker.join(1.0)
        self.assertFalse(worker.is_alive())

    def test_impossible_single_reservation_fails_immediately(self):
        manager = DiskSpaceReservations()
        with patch(
            "mediate.safety.shutil.disk_usage",
            return_value=SimpleNamespace(free=32 * 1024 * 1024),
        ):
            with self.assertRaisesRegex(SafetyError, "aggregate free space"):
                manager.acquire(self.root, 1)

    def test_waiting_reservation_honours_cancellation(self):
        manager = DiskSpaceReservations()
        with patch(
            "mediate.safety.shutil.disk_usage",
            return_value=SimpleNamespace(free=96 * 1024 * 1024),
        ):
            first = manager.acquire(self.root, 1)
            with self.assertRaisesRegex(SafetyError, "cancelled"):
                manager.acquire(self.root, 1, cancelled=lambda: True)
            first.release()


class CapabilityPreflightTests(unittest.TestCase):
    def test_missing_tools_produce_actionable_errors(self):
        with patch("mediate.capabilities.shutil.which", return_value=None):
            report = check_media_capabilities(require_video=True, require_photos=True)
        self.assertFalse(report.ok)
        self.assertTrue(any("brew install ffmpeg webp" in error for error in report.errors))

    def test_animated_webp_requires_the_native_ffmpeg_9_demuxer(self):
        def fake_run(args, timeout=20.0):
            if "-version" in args:
                return SimpleNamespace(returncode=0, stdout="ffmpeg version 8.1\n", stderr="")
            if "-encoders" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout=" V..... libx264 H.264\n A..... aac AAC\n",
                    stderr="",
                )
            if "-demuxers" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout=" D  image2 image sequence\n D  webp_pipe piped webp sequence\n",
                    stderr="",
                )
            if "-decoders" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout=" VF...D webp_anim Animated WebP\n",
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout="-progress -display_rotation",
                stderr="",
            )

        with patch("mediate.capabilities.shutil.which", return_value="/tool"), patch(
            "mediate.capabilities._run", side_effect=fake_run
        ):
            report = check_media_capabilities(
                require_video=True,
                require_photos=False,
                require_animated_webp=True,
            )
        self.assertFalse(report.ok)
        self.assertTrue(any("FFmpeg 9" in error for error in report.errors))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_installed_ffmpeg_passes_real_smoke_encode_and_probe(self):
        report = check_media_capabilities(require_video=True, require_photos=False)
        self.assertTrue(report.ok, report.errors)
        self.assertIn("ffmpeg", report.versions)
        self.assertIn("ffprobe", report.versions)


if __name__ == "__main__":
    unittest.main()
