import json
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from mediate.capabilities import CapabilityReport, _check_avifenc
from mediate.cli import load_config_args
from mediate.exiftool import _argfile_safe, exiftool_available, run_exiftool
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

    @unittest.skipUnless(shutil.which("exiftool"), "exiftool not installed")
    def test_concurrent_queries_do_not_interleave(self):
        # Each thread gets its own daemon from the pool; a shared daemon must
        # still answer one query at a time, never splicing two responses.
        results = []
        errors = []

        def query():
            try:
                results.append(run_exiftool(["-ver"]))
            except Exception as exc:  # pragma: no cover - failure detail only
                errors.append(exc)

        threads = [threading.Thread(target=query) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1, results)
        self.assertTrue(results[0].strip())


class ExifToolArgumentSafetyTests(unittest.TestCase):
    """`-@ -` is one argument per line, so a newline would split an argument."""

    def test_newline_arguments_bypass_the_daemon(self):
        args = ["-s3", "-ContentIdentifier", "/tmp/evil\n-delete_original.jpg"]
        self.assertFalse(_argfile_safe(args))

    def test_ordinary_arguments_use_the_daemon(self):
        self.assertTrue(_argfile_safe(["-s3", "-ContentIdentifier", "/tmp/a b.jpg"]))
        self.assertTrue(_argfile_safe(["-s3", "/tmp/naïve — file.jpg"]))

    def test_a_newline_path_is_sent_as_a_single_argv_entry(self):
        with patch("mediate.exiftool.exiftool_available", return_value=True), \
                patch("mediate.exiftool._one_shot") as one_shot, \
                patch("mediate.exiftool._get_pool") as pool:
            one_shot.return_value = ""
            run_exiftool(["-ver", "/tmp/a\nb.jpg"])
        pool.assert_not_called()
        one_shot.assert_called_once_with(["-ver", "/tmp/a\nb.jpg"])


class AvifInstallHintTests(unittest.TestCase):
    def test_a_missing_avifenc_names_libavif(self):
        report = CapabilityReport()
        with patch("mediate.capabilities.shutil.which", return_value=None):
            _check_avifenc(report)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("libavif", report.errors[0])
        self.assertNotIn("brew install ffmpeg webp", report.errors[0])


class ProbeCacheDurabilityTests(unittest.TestCase):
    def test_the_cache_is_replaced_atomically(self):
        from mediate import probe

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "sub" / "probe-cache.json"
            cache.parent.mkdir()
            cache.write_text(json.dumps({"previous": 1}), encoding="utf-8")
            with patch.object(probe, "_cache", {"fresh": {"v": "ok"}}), \
                    patch.object(probe, "_cache_dirty", True), \
                    patch.object(probe, "_cache_file", return_value=cache):
                probe.save_probe_cache()
            self.assertEqual(json.loads(cache.read_text()), {"fresh": {"v": "ok"}})
            self.assertEqual(list(cache.parent.iterdir()), [cache], "temp file left behind")

    def test_a_failed_write_leaves_the_previous_cache_intact(self):
        from mediate import probe

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "probe-cache.json"
            cache.write_text(json.dumps({"previous": 1}), encoding="utf-8")
            with patch.object(probe, "_cache", {"fresh": 1}), \
                    patch.object(probe, "_cache_dirty", True), \
                    patch.object(probe, "_cache_file", return_value=cache), \
                    patch("mediate.probe.os.replace", side_effect=OSError("boom")):
                probe.save_probe_cache()  # must not raise
            self.assertEqual(json.loads(cache.read_text()), {"previous": 1})
            self.assertEqual(list(cache.parent.iterdir()), [cache], "temp file left behind")


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

    def test_full_range_h264_alias_is_standard(self):
        from unittest.mock import patch
        from mediate.probe import MP4_STANDARD, STREAM_STANDARD, _mp4_status_uncached
        data = {"streams": [
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuvj420p"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_STANDARD)
        with patch("mediate.probe._ffprobe_json", return_value=data):
            self.assertEqual(_mp4_status_uncached(Path("full-range.mp4")), MP4_STANDARD)

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

    def test_h264_non_aac_audio_copies_video_and_converts_only_audio(self):
        from mediate.probe import STREAM_COPY_VIDEO
        data = {"streams": [
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "pcm_s16le"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_COPY_VIDEO)

    def test_apple_compatible_hevc_non_aac_audio_copies_video(self):
        from mediate.probe import STREAM_COPY_VIDEO
        data = {"streams": [
            {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p"},
            {"codec_type": "audio", "codec_name": "ac3"},
        ]}
        self.assertEqual(self._mock_status(data), STREAM_COPY_VIDEO)

    def test_non_apple_hevc_chroma_requires_video_conversion(self):
        from mediate.probe import STREAM_NEEDS_CONVERSION
        data = {"streams": [
            {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv422p10le"},
            {"codec_type": "audio", "codec_name": "ac3"},
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

    def test_mp4_classification_explains_non_aac_audio(self):
        from unittest.mock import patch
        from mediate.probe import MP4_NEEDS_CONVERSION, _mp4_classification_uncached

        data = {"streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "codec_tag_string": "avc1",
                "pix_fmt": "yuv420p",
            },
            {"codec_type": "audio", "codec_name": "ac3", "codec_tag_string": "ac-3"},
        ]}
        with patch("mediate.probe._ffprobe_json", return_value=data):
            result = _mp4_classification_uncached(Path("movie.mp4"))
        self.assertEqual(result["status"], MP4_NEEDS_CONVERSION)
        self.assertEqual(result["reason"], "audio is ac3, not AAC")

    def test_mp4_with_non_apple_h264_tag_needs_only_remux(self):
        from unittest.mock import patch
        from mediate.probe import MP4_NEEDS_CONVERSION, _mp4_classification_uncached

        data = {"streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "codec_tag_string": "avc3",
                "pix_fmt": "yuv420p",
            },
            {"codec_type": "audio", "codec_name": "aac", "codec_tag_string": "mp4a"},
        ]}
        with patch("mediate.probe._ffprobe_json", return_value=data):
            result = _mp4_classification_uncached(Path("movie.mp4"))
        self.assertEqual(result["status"], MP4_NEEDS_CONVERSION)
        self.assertIn("remux", result["reason"])

    def test_mp4_with_hev1_tag_is_remuxed_for_apple_preview(self):
        from unittest.mock import patch
        from mediate.probe import MP4_NEEDS_CONVERSION, _mp4_classification_uncached

        data = {"streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "codec_tag_string": "hev1",
                "pix_fmt": "yuv420p",
            },
            {"codec_type": "audio", "codec_name": "aac", "codec_tag_string": "mp4a"},
        ]}
        with patch("mediate.probe._ffprobe_json", return_value=data):
            result = _mp4_classification_uncached(Path("movie.mp4"))
        self.assertEqual(result["status"], MP4_NEEDS_CONVERSION)
        self.assertIn("hvc1 MP4 remux", result["reason"])

    def test_mp4_with_hvc1_and_apple_hevc_chroma_is_native(self):
        from unittest.mock import patch
        from mediate.probe import MP4_HEVC, _mp4_classification_uncached

        data = {"streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "codec_tag_string": "hvc1",
                "pix_fmt": "yuv420p10le",
            },
            {"codec_type": "audio", "codec_name": "aac", "codec_tag_string": "mp4a"},
        ]}
        with patch("mediate.probe._ffprobe_json", return_value=data):
            result = _mp4_classification_uncached(Path("movie.mp4"))
        self.assertEqual(result["status"], MP4_HEVC)


class ScanCompletionTests(unittest.TestCase):
    def test_optional_existing_health_includes_apple_playback(self):
        from unittest.mock import patch
        from mediate.cli import _existing_mp4_health

        with patch(
            "mediate.probe.video_health",
            return_value={"ok": True, "reason": "ok"},
        ), patch(
            "mediate.validators.verify_apple_playback",
            return_value=(False, "not playable"),
        ):
            health = _existing_mp4_health(Path("old-output.mp4"))
        self.assertEqual(
            health,
            {"ok": False, "reason": "Apple playback: not playable"},
        )

    def test_completed_retained_sources_do_not_run_again(self):
        from mediate.cli import _filter_completed_conversions
        from mediate.scanner import MediaJob

        source = MediaJob(Path("legacy.webm"), "video")
        output = MediaJob(Path("legacy.mp4"), "mp4")
        remaining, completed = _filter_completed_conversions(
            [source, output],
            lambda path: Path("legacy.mp4") if path == source.path else None,
        )
        self.assertEqual(remaining, [output])
        self.assertEqual(completed, {source.path: Path("legacy.mp4")})

    def test_compatible_hevc_mp4s_can_be_filtered_before_candidate_count(self):
        from mediate.cli import _filter_standardized_mp4
        from mediate.scanner import MediaJob

        hevc = MediaJob(Path("native-hevc.mp4"), "mp4")
        candidates, count = _filter_standardized_mp4(
            [hevc],
            lambda _path: "hevc",
            {"standard", "hevc"},
        )
        self.assertEqual(candidates, [])
        self.assertEqual(count, 1)

    def test_retained_source_is_complete_only_while_validated_output_is_intact(self):
        from unittest.mock import patch
        from mediate import probe

        with tempfile.TemporaryDirectory() as tmp, patch.dict(probe._cache, {}, clear=True):
            source = Path(tmp) / "legacy.webm"
            output = Path(tmp) / "legacy.mp4"
            source.write_bytes(b"unchanged-source")
            output.write_bytes(b"validated-output")
            probe.mark_conversion_complete(source, output)
            self.assertEqual(probe.completed_conversion_output(source), output.resolve())

            output.write_bytes(b"changed-output")
            self.assertIsNone(probe.completed_conversion_output(source))

    def test_completed_mp4s_do_not_return_as_candidates(self):
        from mediate.cli import _filter_standardized_mp4
        from mediate.scanner import MediaJob

        complete = MediaJob(Path("complete.mp4"), "mp4")
        legacy = MediaJob(Path("legacy.vob"), "video")
        candidates, count = _filter_standardized_mp4(
            [complete, legacy],
            lambda _path: "standard",
            "standard",
            health_fn=lambda _path: {"ok": True, "reason": "ok"},
        )
        self.assertEqual(candidates, [legacy])
        self.assertEqual(count, 1)

    def test_damaged_standard_mp4_remains_a_repair_candidate(self):
        from mediate.cli import _filter_standardized_mp4
        from mediate.scanner import MediaJob

        damaged = MediaJob(Path("damaged.mp4"), "mp4")
        candidates, count = _filter_standardized_mp4(
            [damaged],
            lambda _path: "standard",
            "standard",
            health_fn=lambda _path: {"ok": False, "reason": "decode error"},
            validate_health=True,
        )
        self.assertEqual(candidates, [damaged])
        self.assertEqual(count, 0)

    def test_existing_health_validation_is_opt_in(self):
        from unittest.mock import Mock
        from mediate.cli import _filter_standardized_mp4
        from mediate.scanner import MediaJob

        complete = MediaJob(Path("complete.mp4"), "mp4")
        health = Mock(return_value={"ok": False, "reason": "decode error"})
        candidates, count = _filter_standardized_mp4(
            [complete],
            lambda _path: "standard",
            "standard",
            health_fn=health,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(count, 1)
        health.assert_not_called()

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


class ProbeCacheIdentityTests(unittest.TestCase):
    def test_strong_cache_recomputes_when_same_size_and_mtime_content_changes(self):
        import mediate.probe as probe

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "movie.mp4"
            path.write_bytes(b"A" * 200000)
            original = path.stat()
            calls = []

            def compute():
                calls.append(True)
                return len(calls)

            kind = f"test-{id(path)}"
            self.assertEqual(probe._cached(kind, path, compute, strong=True), 1)
            path.write_bytes(b"B" * 200000)
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(probe._cached(kind, path, compute, strong=True), 2)


class WorkerResolutionTests(unittest.TestCase):
    def test_explicit_integer_workers(self):
        from mediate.cli import _resolve_workers

        self.assertEqual(_resolve_workers("4", has_video=True), 4)
        self.assertEqual(_resolve_workers("1", has_video=False), 1)
        self.assertEqual(_resolve_workers("invalid", has_video=True), 2)
        self.assertEqual(_resolve_workers("0", has_video=True), 2)
        self.assertEqual(_resolve_workers("-5", has_video=True), 2)

    def test_auto_workers(self):
        from unittest.mock import patch
        from mediate.cli import _resolve_workers

        with patch("os.cpu_count", return_value=16):
            self.assertEqual(_resolve_workers("auto", has_video=True), 8)
            self.assertEqual(_resolve_workers("auto", has_video=False), 16)
        with patch("os.cpu_count", return_value=4):
            self.assertEqual(_resolve_workers("auto", has_video=True), 4)
            self.assertEqual(_resolve_workers("auto", has_video=False), 4)
        with patch("os.cpu_count", return_value=None):
            self.assertEqual(_resolve_workers("auto", has_video=True), 2)
            self.assertEqual(_resolve_workers("auto", has_video=False), 2)


class AvifConverterTests(unittest.TestCase):
    def test_intended_output_respects_format(self):
        from mediate.converters import intended_output
        from mediate.scanner import MediaJob

        photo_job = MediaJob(Path("photo.jpg"), "photo")
        heic_job = MediaJob(Path("image.heic"), "heic")
        video_job = MediaJob(Path("video.mov"), "video")

        self.assertEqual(intended_output(photo_job, output_format="webp"), Path("photo.webp"))
        self.assertEqual(intended_output(photo_job, output_format="avif"), Path("photo.avif"))
        self.assertEqual(intended_output(heic_job, output_format="avif"), Path("image.avif"))
        self.assertEqual(intended_output(video_job, output_format="avif"), Path("video.mp4"))

    def test_build_command_photo_avif(self):
        from mediate.converters import _build_command

        webp_cmd = _build_command("photo", Path("in.jpg"), Path("out.webp"), output_format="webp")
        avif_cmd = _build_command("photo", Path("in.jpg"), Path("out.avif"), output_format="avif")

        self.assertEqual(webp_cmd[0], "cwebp")
        self.assertEqual(avif_cmd[0], "avifenc")
        self.assertIn("--lossless", avif_cmd)


if __name__ == "__main__":
    unittest.main()

