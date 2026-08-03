import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediate.converters import (
    Options,
    SKIPPED,
    _build_command,
    _build_remux_command,
    _build_rotation_command,
    process_job,
)
from mediate.probe import (
    preservation_summary,
    stream_removal_risks,
)
from mediate.scanner import MediaJob
from mediate.validators import validate_output, verify_video_streams


def _stream(
    codec_type,
    codec_name,
    *,
    index=0,
    tags=None,
    disposition=None,
    rotation=None,
    color=None,
    attached_pic=False,
    duration=None,
):
    return {
        "index": index,
        "codec_type": codec_type,
        "codec_name": codec_name,
        "codec_tag_string": None,
        "tags": tags or {},
        "disposition": disposition or {},
        "rotation": rotation,
        "color": color or {},
        "attached_pic": attached_pic,
        "duration": duration,
    }


def _inventory(*streams, chapters=None):
    return {"streams": list(streams), "chapters": chapters or []}


class StreamPreflightTests(unittest.TestCase):
    def test_detects_every_stream_that_requires_removal_opt_in(self):
        inventory = _inventory(
            _stream("video", "h264"),
            _stream("video", "h264", index=1),
            _stream("video", "mjpeg", index=2, attached_pic=True),
            _stream("subtitle", "ass", index=3),
            _stream("data", "bin_data", index=4),
        )
        risks = stream_removal_risks(inventory)
        self.assertEqual(
            risks,
            [
                "1 additional video track(s)",
                "1 subtitle track(s) (ass)",
                "1 attached artwork stream(s)",
                "1 unsupported data stream(s)",
            ],
        )

    def test_audio_commentary_chapters_rotation_and_colour_are_preservable(self):
        inventory = _inventory(
            _stream(
                "video",
                "h264",
                rotation=90,
                color={"color_primaries": "bt709", "color_space": "bt709"},
            ),
            _stream("audio", "aac", index=1, tags={"language": "jpn"}),
            _stream(
                "audio",
                "aac",
                index=2,
                tags={"language": "eng", "title": "Director Commentary"},
                disposition={"comment": 1},
            ),
            chapters=[{"start_time": "0.0", "end_time": "1.0", "title": "Opening"}],
        )
        self.assertEqual(stream_removal_risks(inventory), [])
        self.assertEqual(
            preservation_summary(inventory),
            "2 audio tracks (1 commentary), 1 chapter(s), rotation 90 degrees, colour metadata",
        )

    def test_mov_chapter_carrier_is_not_treated_as_removable_data(self):
        carrier = _stream("data", "bin_data", index=3)
        carrier["codec_tag_string"] = "text"
        inventory = _inventory(
            _stream("video", "h264"),
            carrier,
            chapters=[{"start_time": "0.0", "end_time": "1.0", "title": "Opening"}],
        )
        self.assertEqual(stream_removal_risks(inventory), [])

    def test_matroska_image_attachment_is_reported_as_artwork(self):
        artwork = _stream(
            "attachment",
            "mjpeg",
            index=2,
            tags={"filename": "cover.jpg", "mimetype": "image/jpeg"},
        )
        inventory = _inventory(_stream("video", "h264"), artwork)
        self.assertEqual(
            stream_removal_risks(inventory),
            ["1 attached artwork stream(s)"],
        )

    def test_process_job_warns_and_skips_without_opt_in(self):
        inventory = _inventory(
            _stream("video", "vp9"),
            _stream("subtitle", "webvtt", index=1),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mkv"
            path.write_bytes(b"fixture")
            with patch("mediate.converters.video_inventory", return_value=inventory):
                result = process_job(MediaJob(path, "video"), Options(dry_run=True))
        self.assertEqual(result.status, SKIPPED)
        self.assertIn("stream safety warning", result.detail)
        self.assertIn("--allow-stream-removal", result.detail)

    def test_process_job_fails_closed_when_stream_preflight_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.asf"
            path.write_bytes(b"not media")
            with patch("mediate.converters.video_inventory", return_value=None):
                result = process_job(MediaJob(path, "video"), Options(dry_run=True))
        self.assertEqual(result.status, SKIPPED)
        self.assertIn("stream preflight failed", result.detail)


class StreamCommandTests(unittest.TestCase):
    def setUp(self):
        self.inventory = _inventory(
            _stream(
                "video",
                "vp9",
                rotation=-90,
                color={
                    "color_range": "tv",
                    "color_space": "bt709",
                    "color_transfer": "bt709",
                    "color_primaries": "bt709",
                    "chroma_location": "left",
                },
            ),
            _stream("audio", "opus", index=1, tags={"language": "jpn"}),
            _stream("audio", "opus", index=2, tags={"title": "Commentary"}),
        )

    def test_encode_explicitly_maps_primary_video_and_all_audio(self):
        command = _build_command("video", Path("in.mkv"), Path("out.mp4"), self.inventory)
        joined = " ".join(command)
        self.assertIn("-noautorotate -i in.mkv", joined)
        self.assertIn("-map 0:v:0 -map 0:a?", joined)
        self.assertIn("-map_metadata:s:a:0 0:s:a:0", joined)
        self.assertIn("-map_metadata:s:a:1 0:s:a:1", joined)
        self.assertIn("-metadata:s:a:0 language=jpn", joined)
        self.assertIn("-metadata:s:a:1 title=Commentary", joined)
        self.assertIn("-metadata:s:a:1 handler_name=Commentary", joined)
        self.assertIn("-map_chapters 0", joined)
        self.assertIn("-color_primaries:v:0 bt709", joined)
        self.assertNotIn("0:s?", joined)

    def test_repair_command_enables_tolerant_timestamp_and_packet_handling(self):
        command = _build_command(
            "video", Path("broken.mp4"), Path("repaired.mp4"), self.inventory,
            repair=True,
        )
        joined = " ".join(command)
        self.assertIn("-fflags +genpts+discardcorrupt", joined)
        self.assertIn("-err_detect ignore_err", joined)

    def test_remux_uses_the_same_explicit_mapping(self):
        command = _build_remux_command(Path("in.mov"), Path("out.mp4"), self.inventory)
        joined = " ".join(command)
        self.assertIn("-map 0:v:0 -map 0:a?", joined)
        self.assertIn("-map_chapters 0", joined)
        self.assertIn("-c copy", joined)

    def test_rotation_finalizer_writes_display_matrix_without_reencoding(self):
        command = _build_rotation_command(
            Path("encoded.mp4"), Path("out.mp4"), self.inventory
        )
        joined = " ".join(command)
        self.assertIn("-display_rotation:v:0 -90 -i encoded.mp4", joined)
        self.assertIn("-c copy", joined)


class StreamValidationTests(unittest.TestCase):
    def setUp(self):
        self.video = _stream(
            "video",
            "h264",
            rotation=90,
            color={"color_primaries": "bt709", "color_space": "bt709"},
        )
        self.japanese = _stream(
            "audio",
            "aac",
            index=1,
            tags={"language": "jpn", "title": "Japanese"},
            disposition={"default": 1},
        )
        self.commentary = _stream(
            "audio",
            "aac",
            index=2,
            tags={"language": "eng", "title": "Commentary"},
            disposition={"comment": 1},
        )
        self.chapter = {"start_time": "0.000", "end_time": "2.000", "title": "Opening"}

    def _verify(self, source, output):
        with patch("mediate.validators.verify_video_duration", return_value=(True, "ok")), patch(
            "mediate.validators.video_inventory", return_value=output
        ):
            return verify_video_streams(Path("source.mkv"), Path("output.mp4"), source)

    def test_accepts_preserved_tracks_and_metadata(self):
        inventory = _inventory(self.video, self.japanese, self.commentary, chapters=[self.chapter])
        self.assertEqual(self._verify(inventory, inventory), (True, "ok"))

    def test_accepts_mp4_handler_name_as_the_preserved_track_title(self):
        output_audio = dict(self.japanese)
        output_audio["tags"] = {"language": "jpn", "handler_name": "Japanese"}
        source = _inventory(self.video, self.japanese)
        output = _inventory(self.video, output_audio)
        self.assertEqual(self._verify(source, output), (True, "ok"))

    def test_ignores_a_generic_mp4_audio_handler_name(self):
        source_audio = dict(self.japanese)
        source_audio["tags"] = {"language": "jpn"}
        output_audio = dict(source_audio)
        output_audio["tags"] = {"language": "jpn", "handler_name": "SoundHandler"}
        source = _inventory(self.video, source_audio)
        output = _inventory(self.video, output_audio)
        self.assertEqual(self._verify(source, output), (True, "ok"))

    def test_rejects_a_quietly_dropped_japanese_track(self):
        source = _inventory(self.video, self.japanese, self.commentary)
        output = _inventory(self.video, self.commentary)
        ok, reason = self._verify(source, output)
        self.assertFalse(ok)
        self.assertIn("audio track count changed", reason)

    def test_rejects_a_truncated_secondary_audio_track(self):
        japanese = dict(self.japanese)
        japanese["duration"] = "10.0"
        commentary = dict(self.commentary)
        commentary["duration"] = "10.0"
        source = _inventory(self.video, japanese, commentary)
        truncated = dict(commentary)
        truncated["duration"] = "4.0"
        output = _inventory(self.video, japanese, truncated)
        ok, reason = self._verify(source, output)
        self.assertFalse(ok)
        self.assertIn("audio track 2 duration changed", reason)

    def test_rejects_changed_commentary_identity(self):
        source = _inventory(self.video, self.japanese, self.commentary)
        changed = dict(self.commentary)
        changed["disposition"] = {}
        changed["tags"] = {"language": "eng", "title": "Stereo Mix"}
        output = _inventory(self.video, self.japanese, changed)
        ok, reason = self._verify(source, output)
        self.assertFalse(ok)
        self.assertIn("audio track 2", reason)

    def test_allows_mp4_to_default_first_track_when_source_has_no_default(self):
        source_audio = dict(self.japanese)
        source_audio["disposition"] = {}
        output_audio = dict(source_audio)
        output_audio["disposition"] = {"default": 1}
        source = _inventory(self.video, source_audio)
        output = _inventory(self.video, output_audio)
        self.assertEqual(self._verify(source, output), (True, "ok"))

    def test_rejects_moving_an_explicit_default_to_another_track(self):
        source = _inventory(self.video, self.japanese, self.commentary)
        japanese = dict(self.japanese)
        japanese["disposition"] = {}
        commentary = dict(self.commentary)
        commentary["disposition"] = {"default": 1, "comment": 1}
        output = _inventory(self.video, japanese, commentary)
        ok, reason = self._verify(source, output)
        self.assertFalse(ok)
        self.assertIn("default audio selection changed", reason)

    def test_rejects_lost_chapters_rotation_or_colour(self):
        source = _inventory(self.video, self.japanese, chapters=[self.chapter])

        ok, reason = self._verify(source, _inventory(self.video, self.japanese))
        self.assertFalse(ok)
        self.assertIn("chapter count changed", reason)

        rotated = dict(self.video)
        rotated["rotation"] = None
        ok, reason = self._verify(source, _inventory(rotated, self.japanese, chapters=[self.chapter]))
        self.assertFalse(ok)
        self.assertIn("rotation", reason)

        recoloured = dict(self.video)
        recoloured["color"] = {"color_primaries": "bt709"}
        ok, reason = self._verify(source, _inventory(recoloured, self.japanese, chapters=[self.chapter]))
        self.assertFalse(ok)
        self.assertIn("color_space", reason)

    def test_accepts_implicit_h264_limited_range(self):
        source_video = dict(self.video)
        source_video["color"] = {"color_range": "tv"}
        output_video = dict(source_video)
        output_video["color"] = {}
        source = _inventory(source_video, self.japanese)
        output = _inventory(output_video, self.japanese)
        self.assertEqual(self._verify(source, output), (True, "ok"))

    def test_allows_explicit_removal_of_an_extra_video_track(self):
        source = _inventory(
            self.video,
            _stream("video", "h264", index=3),
            self.japanese,
        )
        output = _inventory(self.video, self.japanese)
        with patch("mediate.validators.verify_video_duration", return_value=(True, "ok")), patch(
            "mediate.validators.video_inventory", return_value=output
        ):
            result = verify_video_streams(
                Path("source.mkv"),
                Path("output.mp4"),
                source,
                allow_stream_removal=True,
            )
        self.assertEqual(result, (True, "ok"))


class DecodeIntegrityTests(unittest.TestCase):
    def test_integrity_check_maps_every_video_and_audio_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.mp4"
            output.write_bytes(b"not-empty")
            with patch(
                "mediate.validators.check_video_integrity",
                return_value={"ok": True, "reason": "ok"},
            ) as check:
                result = validate_output(0, "", output, is_video=True)
        self.assertEqual(result, (True, "ok"))
        check.assert_called_once_with(output, progress_path=None)


if __name__ == "__main__":
    unittest.main()
