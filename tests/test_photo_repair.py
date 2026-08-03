import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediate.converters import _convert, _convert_photo


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


if __name__ == "__main__":
    unittest.main()
