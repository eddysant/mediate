import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mediate.converters import (
    CONVERTED,
    Options,
    _convert,
    _convert_photo,
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


if __name__ == "__main__":
    unittest.main()
