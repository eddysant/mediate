import json
import os
import shutil
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path

from mediate.cli import load_config_args
from mediate.exiftool import exiftool_available, run_exiftool
from mediate.renamer import Rename, apply_renames, load_plan, plan_renames, write_plan


class PlanFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_roundtrip_and_apply(self):
        self.touch("misty vale (1).jpg")
        plan_path = self.root / "plan.json"
        write_plan(self.root, plan_renames(self.root), plan_path)
        loaded = load_plan(self.root, plan_path)
        self.assertEqual(
            [(p.src.name, p.dst.name) for p in loaded],
            [("misty vale (1).jpg", "Misty Vale [1].jpg")],
        )
        renamed, skipped, _ = apply_renames(loaded, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (1, 0))
        self.assertTrue((self.root / "Misty Vale [1].jpg").exists())

    def test_edited_plan_is_honored(self):
        self.touch("a.jpg")
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps({"renames": [{"from": "a.jpg", "to": "Chosen Name.jpg"}]}))
        loaded = load_plan(self.root, plan_path)
        apply_renames(loaded, self.root, dry_run=False)
        self.assertTrue((self.root / "Chosen Name.jpg").exists())

    def test_plan_cannot_escape_root(self):
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps({"renames": [{"from": "a.jpg", "to": "../evil.jpg"}]}))
        with self.assertRaises(ValueError):
            load_plan(self.root, plan_path)
        plan_path.write_text(json.dumps({"renames": [{"from": "/etc/passwd", "to": "x.jpg"}]}))
        with self.assertRaises(ValueError):
            load_plan(self.root, plan_path)


class ConfigTests(unittest.TestCase):
    def test_reads_flags_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.write_text(
                "# my defaults\n--only-if-smaller\n\n--workers 4\n--convert-heic\n"
            )
            old = os.environ.get("MEDIATE_CONFIG")
            os.environ["MEDIATE_CONFIG"] = str(cfg)
            try:
                self.assertEqual(
                    load_config_args(),
                    ["--only-if-smaller", "--workers", "4", "--convert-heic"],
                )
            finally:
                if old is None:
                    del os.environ["MEDIATE_CONFIG"]
                else:
                    os.environ["MEDIATE_CONFIG"] = old

    def test_missing_file_is_empty(self):
        old = os.environ.get("MEDIATE_CONFIG")
        os.environ["MEDIATE_CONFIG"] = "/nonexistent/mediate-config"
        try:
            self.assertEqual(load_config_args(), [])
        finally:
            if old is None:
                del os.environ["MEDIATE_CONFIG"]
            else:
                os.environ["MEDIATE_CONFIG"] = old


class ExifToolDaemonTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("exiftool"), "exiftool not installed")
    def test_daemon_answers_repeatedly(self):
        self.assertTrue(exiftool_available())
        first = run_exiftool(["-ver"])
        second = run_exiftool(["-ver"])
        self.assertTrue(first and first.strip())
        self.assertEqual(first, second)

    @unittest.skipIf(shutil.which("exiftool"), "exiftool installed")
    def test_returns_none_without_exiftool(self):
        self.assertIsNone(run_exiftool(["-ver"]))


class VideoStreamStatusTests(unittest.TestCase):
    """Tests for probe.video_stream_status() — classifies any container's
    streams as standard (remuxable), hevc, or needing conversion."""

    def _mock_status(self, probe_result):
        """Run video_stream_status with a mocked _ffprobe_json return."""
        from unittest.mock import patch
        from mediate.probe import video_stream_status, STREAM_STANDARD, STREAM_HEVC, STREAM_NEEDS_CONVERSION
        with tempfile.TemporaryDirectory() as tmp:
            dummy = Path(tmp) / "test.mov"
            dummy.write_bytes(b"x")
            with patch("mediate.probe._ffprobe_json", return_value=probe_result):
                # Bypass cache by using a fresh path each time
                with patch("mediate.probe._cached", side_effect=lambda _k, _p, fn: fn()):
                    return video_stream_status(dummy)

    def test_h264_aac_is_standard(self):
        from mediate.probe import STREAM_STANDARD
        data = {"streams": [
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_STANDARD)

    def test_hevc_aac_is_hevc(self):
        from mediate.probe import STREAM_HEVC
        data = {"streams": [
            {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_HEVC)

    def test_vp9_needs_conversion(self):
        from mediate.probe import STREAM_NEEDS_CONVERSION
        data = {"streams": [
            {"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "opus"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_NEEDS_CONVERSION)

    def test_h264_non_aac_audio_needs_conversion(self):
        from mediate.probe import STREAM_NEEDS_CONVERSION
        data = {"streams": [
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "pcm_s16le"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_NEEDS_CONVERSION)

    def test_h264_wrong_pixfmt_needs_conversion(self):
        from mediate.probe import STREAM_NEEDS_CONVERSION
        data = {"streams": [
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv422p"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_NEEDS_CONVERSION)

    def test_no_video_stream_needs_conversion(self):
        from mediate.probe import STREAM_NEEDS_CONVERSION
        data = {"streams": [
            {"codec_type": "audio", "codec_name": "aac"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_NEEDS_CONVERSION)

    def test_probe_failure_needs_conversion(self):
        from mediate.probe import STREAM_NEEDS_CONVERSION
        self.assertEqual(self._mock_status(None), STREAM_NEEDS_CONVERSION)

    def test_h264_no_audio_is_standard(self):
        """Screen recordings and silent clips with no audio track are remuxable."""
        from mediate.probe import STREAM_STANDARD
        data = {"streams": [
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_STANDARD)


class ScanCompletionTests(unittest.TestCase):
    def test_completed_mp4s_do_not_return_as_candidates(self):
        from mediate.cli import _filter_standardized_mp4
        from mediate.scanner import MediaJob

        complete = MediaJob(Path("complete.mp4"), "mp4")
        legacy = MediaJob(Path("legacy.vob"), "video")
        candidates, count = _filter_standardized_mp4(
            [complete, legacy], lambda _path: "standard", "standard"
        )
        self.assertEqual(candidates, [legacy])
        self.assertEqual(count, 1)

    def test_one_worker_exception_does_not_abort_result_collection(self):
        from mediate.cli import _resolve_future
        from mediate.converters import FAILED
        from mediate.scanner import MediaJob

        future = Future()
        future.set_exception(RuntimeError("bad container"))
        job = MediaJob(Path("broken.asf"), "video")
        outcome = _resolve_future(future, job)
        self.assertEqual(outcome.status, FAILED)
        self.assertEqual(outcome.path, job.path)
        self.assertIn("bad container", outcome.detail)


if __name__ == "__main__":
    unittest.main()
