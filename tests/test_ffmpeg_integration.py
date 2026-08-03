"""Small, real-media tests for behavior that mocks cannot prove.

The fixtures are generated at runtime and stay under a temporary directory.
The class is skipped when FFmpeg or the production libx264 encoder is absent,
so the stdlib-only test suite remains usable on development machines without
media tools.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mediate.converters import (
    CONVERTED,
    REMUXED,
    REPAIRED,
    SKIPPED,
    Options,
    process_job,
)
from mediate.disposal import DisposalPolicy, HARD
from mediate.probe import (
    audio_stream_label,
    attached_artwork_streams,
    inventory_streams,
    primary_video_streams,
    video_health,
    video_inventory,
)
from mediate.scanner import MediaJob


def _ffmpeg_has_encoder(name: str) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and name in proc.stdout


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe") and _ffmpeg_has_encoder("libx264"),
    "FFmpeg, ffprobe, and libx264 are required for media integration tests",
)
class FFmpegIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

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

    def convert(self, path: Path, *, allow_stream_removal: bool = False):
        return process_job(
            MediaJob(path, "video"),
            Options(
                keep_originals=True,
                allow_stream_removal=allow_stream_removal,
            ),
        )

    def test_asf_and_vob_convert_and_validate_end_to_end(self):
        self.require_encoders("wmv2", "wmav2", "mpeg2video", "ac3", "aac")
        asf = self.root / "legacy.asf"
        vob = self.root / "dvd.vob"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "wmv2", "-c:a", "wmav2", asf,
        )
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
            "-c:v", "mpeg2video", "-c:a", "ac3", "-f", "vob", vob,
        )

        for source in (asf, vob):
            with self.subTest(extension=source.suffix):
                outcome = self.convert(source)
                output = source.with_suffix(".mp4")
                self.assertEqual(outcome.status, CONVERTED, outcome.detail)
                self.assertTrue(source.exists())
                self.assertTrue(output.exists())
                inventory = video_inventory(output)
                self.assertEqual(len(primary_video_streams(inventory)), 1)
                self.assertEqual(len(inventory_streams(inventory, "audio")), 1)

    def test_validated_conversion_uses_transactional_hard_delete(self):
        self.require_encoders("ffv1", "flac", "aac")
        source = self.root / "transactional.mkv"
        sidecar = self.root / "transactional.xmp"
        sidecar.write_text("metadata", encoding="utf-8")
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "ffv1", "-level", "3", "-c:a", "flac", source,
        )
        outcome = process_job(
            MediaJob(source, "video"),
            Options(
                dispose=DisposalPolicy(HARD, self.root),
                transaction_root=self.root,
            ),
        )
        self.assertEqual(outcome.status, CONVERTED, outcome.detail)
        self.assertFalse(source.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue(source.with_suffix(".mp4").exists())
        self.assertEqual(list(self.root.glob(".mediate-txn-*")), [])

    def test_multiple_audio_tracks_commentary_and_chapters_survive(self):
        self.require_encoders("ffv1", "flac", "aac")
        metadata = self.root / "chapters.ffmeta"
        metadata.write_text(
            ";FFMETADATA1\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=Opening\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=Ending\n",
            encoding="utf-8",
        )
        source = self.root / "languages.mkv"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
            "-f", "ffmetadata", "-i", metadata,
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
            "-map_metadata", "3", "-map_chapters", "3",
            "-c:v", "ffv1", "-level", "3", "-c:a", "flac",
            "-metadata:s:a:0", "language=jpn",
            "-metadata:s:a:0", "title=Japanese",
            "-disposition:a:0", "default",
            "-metadata:s:a:1", "language=eng",
            "-metadata:s:a:1", "title=Director Commentary",
            "-disposition:a:1", "comment",
            source,
        )

        outcome = self.convert(source)
        self.assertEqual(outcome.status, CONVERTED, outcome.detail)
        output = video_inventory(source.with_suffix(".mp4"))
        audio = inventory_streams(output, "audio")
        self.assertEqual(len(audio), 2)
        self.assertEqual([track["tags"].get("language") for track in audio], ["jpn", "eng"])
        self.assertEqual(audio_stream_label(audio[0]), "Japanese")
        self.assertIn("Commentary", audio_stream_label(audio[1]) or "")
        self.assertEqual([chapter["title"] for chapter in output["chapters"]], ["Opening", "Ending"])

    def test_surround_channel_layout_and_sample_rate_survive(self):
        self.require_encoders("ffv1", "flac", "aac")
        source = self.root / "surround.mkv"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=5.1:sample_rate=48000",
            "-t", "1", "-c:v", "ffv1", "-c:a", "flac", source,
        )
        outcome = self.convert(source)
        self.assertEqual(outcome.status, CONVERTED, outcome.detail)
        audio = inventory_streams(video_inventory(source.with_suffix(".mp4")), "audio")[0]
        self.assertEqual(audio["channels"], 6)
        self.assertIn("5.1", audio["channel_layout"])
        self.assertEqual(audio["sample_rate"], "48000")

    def test_compatible_subtitles_and_artwork_are_preserved(self):
        self.require_encoders("aac", "mjpeg")
        base = self.root / "base.mkv"
        subtitle = self.root / "captions.srt"
        cover = self.root / "cover.jpg"
        guarded = self.root / "guarded.mkv"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,700\nImportant subtitle.\n",
            encoding="utf-8",
        )
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", base,
        )
        self.ffmpeg(
            "-f", "lavfi", "-i", "color=c=blue:size=32x32",
            "-frames:v", "1", cover,
        )
        self.ffmpeg(
            "-i", base, "-i", subtitle,
            "-map", "0:v:0", "-map", "0:a:0", "-map", "1:s:0",
            "-c", "copy", "-attach", cover,
            "-metadata:s:t", "mimetype=image/jpeg",
            "-metadata:s:t", "filename=cover.jpg",
            guarded,
        )

        allowed = self.convert(guarded)
        self.assertEqual(allowed.status, REMUXED, allowed.detail)
        output = video_inventory(guarded.with_suffix(".mp4"))
        self.assertEqual(
            [stream["codec_name"] for stream in inventory_streams(output, "subtitle")],
            ["mov_text"],
        )
        self.assertEqual(len(attached_artwork_streams(output)), 1)
        self.assertEqual(len(inventory_streams(output, "audio")), 1)

    def test_styled_subtitles_still_require_explicit_removal(self):
        subtitle = self.root / "styled.ass"
        subtitle.write_text(
            "[Script Info]\nScriptType: v4.00+\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
            "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:00.80,Default,,0,0,0,,Styled text\n",
            encoding="utf-8",
        )
        source = self.root / "styled.mkv"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-i", subtitle,
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-c:s", "ass", source,
        )
        blocked = self.convert(source)
        self.assertEqual(blocked.status, SKIPPED, blocked.detail)
        self.assertIn("subtitle track", blocked.detail)
        allowed = self.convert(source, allow_stream_removal=True)
        self.assertEqual(allowed.status, REMUXED, allowed.detail)
        self.assertEqual(
            inventory_streams(video_inventory(source.with_suffix(".mp4")), "subtitle"),
            [],
        )

    def test_rotation_display_matrix_survives_a_real_reencode(self):
        help_text = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "full"],
            capture_output=True,
            text=True,
        ).stdout
        if "display_rotation" not in help_text:
            self.skipTest("FFmpeg lacks -display_rotation")
        self.require_encoders("mpeg4", "aac")
        base = self.root / "base.mov"
        rotated = self.root / "rotated.mov"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "mpeg4", "-q:v", "3", "-c:a", "aac", base,
        )
        self.ffmpeg(
            "-display_rotation:v:0", "90", "-i", base,
            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", rotated,
        )
        source_inventory = video_inventory(rotated)
        self.assertEqual(primary_video_streams(source_inventory)[0]["rotation"], 90)

        outcome = self.convert(rotated)
        self.assertEqual(outcome.status, CONVERTED, outcome.detail)
        output_inventory = video_inventory(rotated.with_suffix(".mp4"))
        self.assertEqual(primary_video_streams(output_inventory)[0]["rotation"], 90)

    def test_damaged_standard_mp4_is_repaired_without_touching_original(self):
        self.require_encoders("aac")
        source = self.root / "damaged.mp4"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=24:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "faststart", source,
        )
        data = bytearray(source.read_bytes())
        mdat = data.find(b"mdat")
        self.assertGreater(mdat, 0)
        damage_at = mdat + 4 + (len(data) - mdat - 4) // 3
        data[damage_at:damage_at + 1024] = b"\0" * 1024
        source.write_bytes(data)
        self.assertFalse(video_health(source)["ok"], "fixture corruption was not detected")

        outcome = process_job(MediaJob(source, "mp4"), Options(keep_originals=True))
        repaired = self.root / "damaged.repaired.mp4"
        self.assertEqual(outcome.status, REPAIRED, outcome.detail)
        self.assertTrue(source.exists())
        self.assertTrue(repaired.exists())
        self.assertTrue(video_health(repaired)["ok"])

    def test_high_bit_depth_source_is_blocked_before_destructive_encode(self):
        self.require_encoders("ffv1")
        source = self.root / "ten-bit.mkv"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-c:v", "ffv1", "-pix_fmt", "yuv420p10le", source,
        )
        outcome = self.convert(source)
        self.assertEqual(outcome.status, SKIPPED, outcome.detail)
        self.assertIn("10-bit video", outcome.detail)
        self.assertFalse(source.with_suffix(".mp4").exists())

    def test_missing_moov_atom_fails_closed_without_replacing_source(self):
        source = self.root / "missing-moov.mp4"
        self.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=12:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", source,
        )
        data = source.read_bytes()
        moov = data.find(b"moov")
        self.assertGreater(moov, 4)
        source.write_bytes(data[:moov - 4])
        outcome = process_job(MediaJob(source, "mp4"), Options(keep_originals=True))
        self.assertEqual(outcome.status, SKIPPED, outcome.detail)
        self.assertIn("stream preflight failed", outcome.detail)
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "missing-moov.repaired.mp4").exists())


if __name__ == "__main__":
    unittest.main()
