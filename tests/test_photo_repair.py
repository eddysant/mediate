import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mediate.converters import (
    CONVERTED,
    Options,
    _convert,
    _convert_photo,
    _decode_heic,
    _heic_primary_stream,
    _png_dimensions,
    _repair_photo_metadata,
    process_job,
    unique_output_path,
)
from mediate.scanner import MediaJob
from mediate.validators import verify_photo_metadata


TRUNCATED_ERROR = """libjpeg error: Premature end of JPEG file
`jpegtran -copy all` MAY be able to process this file.
Error! Could not process file photo.jpg
Error! Cannot read input picture file 'photo.jpg'
"""


class PhotoRepairTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.source = self.root / "photo.jpg"
        self.source.write_bytes(b"truncated-jpeg-source")
        self.output = self.root / ".photo.part.webp"

    def test_truncated_jpeg_is_normalised_losslessly_then_retried(self):
        calls = []

        def run(command):
            calls.append(command)
            if command[0] == "cwebp" and Path(command[-3]) == self.source:
                return subprocess.CompletedProcess(command, 1, "", TRUNCATED_ERROR)
            if command[0] == "jpegtran":
                Path(command[command.index("-outfile") + 1]).write_bytes(b"normalised-jpeg")
                # jpegtran reports truncated input with code 2 even when it
                # successfully emits a complete normalised JPEG.
                return subprocess.CompletedProcess(command, 2, "", "Premature end of JPEG file")
            self.output.write_bytes(b"valid-webp")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("mediate.converters._jpegtran_path", return_value="jpegtran"), patch(
            "mediate.converters._run", side_effect=run
        ):
            result = _convert("photo", self.source, self.output)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.output.read_bytes(), b"valid-webp")
        self.assertEqual(self.source.read_bytes(), b"truncated-jpeg-source")
        self.assertEqual([command[0] for command in calls], ["cwebp", "jpegtran", "cwebp"])
        self.assertFalse(any(self.root.glob("*.jpegtran.jpg")))

    def test_missing_jpegtran_returns_actionable_failure(self):
        failed = subprocess.CompletedProcess([], 1, "", TRUNCATED_ERROR)
        with patch("mediate.converters._jpegtran_path", return_value=None), patch(
            "mediate.converters._run", return_value=failed
        ):
            result = _convert_photo(self.source, self.output)

        self.assertEqual(result.returncode, 1)
        self.assertIn("brew install jpeg-turbo", result.stderr)
        self.assertFalse(self.output.exists())

    def test_failed_jpegtran_keeps_both_decoder_diagnostics(self):
        direct = subprocess.CompletedProcess([], 1, "", TRUNCATED_ERROR)
        normalise = subprocess.CompletedProcess(
            ["jpegtran"], 1, "", "Invalid JPEG file structure"
        )
        with patch("mediate.converters._jpegtran_path", return_value="jpegtran"), patch(
            "mediate.converters._run", side_effect=[direct, normalise]
        ):
            result = _convert_photo(self.source, self.output)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Premature end of JPEG", result.stderr)
        self.assertIn("Invalid JPEG file structure", result.stderr)
        self.assertEqual(self.source.read_bytes(), b"truncated-jpeg-source")
        self.assertFalse(self.output.exists())

    def test_unrelated_cwebp_failure_does_not_attempt_repair(self):
        failure = subprocess.CompletedProcess([], 1, "", "Cannot open output file")
        with patch("mediate.converters._jpegtran_path") as jpegtran, patch(
            "mediate.converters._run", return_value=failure
        ):
            result = _convert_photo(self.source, self.output)

        self.assertIs(result, failure)
        jpegtran.assert_not_called()


class PhotoMetadataTests(unittest.TestCase):
    def _verify(self, source_json, output_json):
        with patch("mediate.validators.exiftool_available", return_value=True), patch(
            "mediate.validators.run_exiftool", side_effect=[source_json, output_json]
        ):
            return verify_photo_metadata(Path("source.jpg"), Path("output.webp"))

    def test_composite_date_does_not_create_a_false_failure(self):
        source = """[{
          "Composite:DateTimeOriginal": "2017:10:19 00:00:00+00:00",
          "IPTC:DateCreated": "2017:10:19",
          "XMP-photoshop:DateCreated": "2017:10:19"
        }]"""
        output = """[{
          "XMP-photoshop:DateCreated": "2017:10:19"
        }]"""
        self.assertEqual(self._verify(source, output), (True, "ok"))

    def test_equivalent_exif_and_xmp_capture_times_match(self):
        source = """[{
          "ExifIFD:DateTimeOriginal": "2010:11:15 16:08:35",
          "XMP-exif:DateTimeOriginal": "2010:11:15 16:08:35.94-06:00"
        }]"""
        output = """[{
          "XMP-exif:DateTimeOriginal": "2010:11:15 16:08:35.94-06:00"
        }]"""
        self.assertEqual(self._verify(source, output), (True, "ok"))

    def test_missing_real_capture_date_still_fails_closed(self):
        source = """[{
          "ExifIFD:DateTimeOriginal": "2020:01:02 03:04:05"
        }]"""
        output = """[{
          "XMP-xmp:CreateDate": "2021:06:07 08:09:10"
        }]"""
        ok, reason = self._verify(source, output)
        self.assertFalse(ok)
        self.assertIn("capture date metadata not preserved", reason)

    def test_exiftool_can_restore_metadata_dropped_by_cwebp(self):
        with patch("mediate.converters.exiftool_available", return_value=True), patch(
            "mediate.converters.run_exiftool", return_value="1 image files updated\n"
        ) as exiftool:
            self.assertTrue(
                _repair_photo_metadata(Path("source.jpg"), Path("output.webp"))
            )
        args = exiftool.call_args.args[0]
        self.assertIn("-EXIF:All", args)
        self.assertIn("-XMP:All", args)
        self.assertIn("-ICC_Profile", args)


class OutputCollisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_unique_output_uses_guid_and_never_overwrites(self):
        preferred = self.root / "photo.webp"
        preferred.write_bytes(b"existing")
        fake_uuid = SimpleNamespace(hex="0123456789abcdef")
        with patch("mediate.converters.uuid.uuid4", return_value=fake_uuid):
            selected = unique_output_path(preferred)
        self.assertEqual(selected.name, "photo.01234567.webp")
        self.assertEqual(preferred.read_bytes(), b"existing")

    def test_process_job_converts_to_guid_name_when_output_exists(self):
        source = self.root / "photo.jpg"
        source.write_bytes(b"source")
        preferred = self.root / "photo.webp"
        preferred.write_bytes(b"existing-output")
        fake_uuid = SimpleNamespace(hex="0123456789abcdef")

        def convert(_kind, _src, output, _inventory=None, repair=False):
            output.write_bytes(b"new-output")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("mediate.converters.uuid.uuid4", return_value=fake_uuid), patch(
            "mediate.converters._convert", side_effect=convert
        ), patch(
            "mediate.converters.verify_photo_metadata", return_value=(True, "ok")
        ):
            result = process_job(
                MediaJob(source, "photo"), Options(keep_originals=True)
            )

        self.assertEqual(result.status, CONVERTED)
        self.assertEqual(preferred.read_bytes(), b"existing-output")
        self.assertEqual(
            (self.root / "photo.01234567.webp").read_bytes(), b"new-output"
        )



def _png_bytes(width: int, height: int) -> bytes:
    """Minimal PNG header — enough for the IHDR dimension read."""
    ihdr = struct.pack(">II", width, height) + bytes([8, 2, 0, 0, 0])
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk


class GifFallbackPlatformTests(unittest.TestCase):
    """The GIF decode fallback is macOS-only and must say so."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.src = self.root / "broken.gif"
        self.src.write_bytes(b"GIF89a not really")

    @staticmethod
    def _cwebp_rejects(cmd):
        if cmd[0] == "sips":
            raise FileNotFoundError(2, "No such file or directory", "sips")
        return subprocess.CompletedProcess(cmd, 1, "", "Cannot read input picture file")

    def test_off_macos_keeps_the_real_cwebp_error(self):
        # Calling sips blind reported "converter not found: sips", blaming a
        # tool the user never asked for and discarding the actual reason.
        with patch.object(sys, "platform", "linux"), \
                patch("mediate.converters._run", side_effect=self._cwebp_rejects):
            result = _convert_photo(self.src, self.root / "out.webp")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read input picture file", result.stderr)
        self.assertIn("macOS sips", result.stderr)

    def test_macos_without_sips_installed_also_falls_back_cleanly(self):
        with patch.object(sys, "platform", "darwin"), \
                patch("mediate.converters.shutil.which", return_value=None), \
                patch("mediate.converters._run", side_effect=self._cwebp_rejects):
            result = _convert_photo(self.src, self.root / "out.webp")
        self.assertIn("Cannot read input picture file", result.stderr)

    def test_macos_with_sips_still_retries_through_it(self):
        calls = []

        def runner(cmd):
            calls.append(cmd[0])
            if cmd[0] == "sips":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    cmd, 1, "", "Cannot read input picture file"
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(sys, "platform", "darwin"), \
                patch("mediate.converters.shutil.which", return_value="/usr/bin/sips"), \
                patch("mediate.converters._run", side_effect=runner):
            result = _convert_photo(self.src, self.root / "out.webp")
        self.assertEqual(result.returncode, 0)
        self.assertIn("sips", calls)


class HeicDecodeTests(unittest.TestCase):
    """HEIC is ISOBMFF, so FFmpeg sees every auxiliary image as a stream."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_the_largest_image_is_chosen_over_a_thumbnail(self):
        src = self.root / "a.heic"
        src.write_bytes(b"x")
        streams = [
            {"index": 0, "codec_name": "hevc", "width": 160, "height": 120},
            {"index": 1, "codec_name": "hevc", "width": 4032, "height": 3024},
        ]
        with patch("mediate.probe.heic_image_streams", return_value=streams):
            self.assertEqual(_heic_primary_stream(src)["index"], 1)

    def test_no_decodable_image_is_an_error_not_a_crash(self):
        src = self.root / "a.heic"
        src.write_bytes(b"x")
        with patch("mediate.probe.heic_image_streams", return_value=[]), \
                patch.object(sys, "platform", "linux"):
            result = _decode_heic(src, self.root / "out.png")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no decodable still image", result.stderr)

    def test_a_decode_that_yields_the_wrong_size_is_rejected(self):
        # Converting the thumbnail instead of the photo would be silent data
        # loss: nothing downstream compares photo dimensions.
        src = self.root / "a.heic"
        src.write_bytes(b"x")
        png = self.root / "out.png"
        streams = [{"index": 0, "codec_name": "hevc", "width": 4032, "height": 3024}]

        def fake_run(cmd):
            png.write_bytes(_png_bytes(160, 120))  # a thumbnail slipped through
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("mediate.probe.heic_image_streams", return_value=streams), \
                patch.object(sys, "platform", "linux"), \
                patch("mediate.converters._run", side_effect=fake_run):
            result = _decode_heic(src, png)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("auxiliary image", result.stderr)

    def test_a_correctly_sized_decode_is_accepted(self):
        src = self.root / "a.heic"
        src.write_bytes(b"x")
        png = self.root / "out.png"
        streams = [{"index": 0, "codec_name": "hevc", "width": 320, "height": 240}]

        def fake_run(cmd):
            png.write_bytes(_png_bytes(320, 240))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("mediate.probe.heic_image_streams", return_value=streams), \
                patch.object(sys, "platform", "linux"), \
                patch("mediate.converters._run", side_effect=fake_run):
            result = _decode_heic(src, png)
        self.assertEqual(result.returncode, 0)

    def test_png_dimensions_reads_the_ihdr(self):
        png = self.root / "a.png"
        png.write_bytes(_png_bytes(1920, 1080))
        self.assertEqual(_png_dimensions(png), (1920, 1080))

    def test_png_dimensions_rejects_a_non_png(self):
        other = self.root / "a.bin"
        other.write_bytes(b"not a png at all, really quite definitely not")
        self.assertIsNone(_png_dimensions(other))


if __name__ == "__main__":
    unittest.main()
