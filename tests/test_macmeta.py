"""Coverage for the birthtime copy that runs on every converted file.

macmeta is 70 lines of ctypes against setattrlist(2) — a hand-packed struct
passed to a C API with no type checking in between. A wrong field width or
order does not raise; it writes a nonsense creation date, and nothing
downstream looks at birthtime again. So the round-trip is asserted against
the filesystem itself rather than against a mock.

The round-trip tests are macOS-only by nature; the rest assert the documented
no-op behaviour everywhere else.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediate import macmeta

darwin_only = unittest.skipUnless(
    sys.platform == "darwin", "setattrlist is macOS-only"
)


class MacMetaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, name: str = "a.webp") -> Path:
        path = self.root / name
        path.write_bytes(b"payload")
        return path


class GetBirthtimeTests(MacMetaTestCase):
    @darwin_only
    def test_an_existing_file_reports_a_plausible_birthtime(self):
        path = self.touch()
        birthtime = macmeta.get_birthtime(path)
        self.assertIsNotNone(birthtime)
        # Just created, so it must sit within a minute of the mtime.
        self.assertAlmostEqual(birthtime, path.stat().st_mtime, delta=60)

    def test_a_missing_file_reports_none_rather_than_raising(self):
        self.assertIsNone(macmeta.get_birthtime(self.root / "nope.webp"))

    def test_a_platform_without_birthtime_reports_none(self):
        # st_birthtime is absent on Linux; the AttributeError must be caught
        # rather than surfacing as a conversion failure.
        path = self.touch()
        stat = os.stat(path)
        bare = type("Stat", (), {name: getattr(stat, name) for name in ("st_size",)})()
        with patch.object(Path, "stat", return_value=bare):
            self.assertIsNone(macmeta.get_birthtime(path))


class SetBirthtimeTests(MacMetaTestCase):
    @darwin_only
    def test_the_written_birthtime_is_read_back(self):
        path = self.touch()
        # A date far enough back that no filesystem default could match it,
        # and old enough to catch a 32/64-bit tv_sec truncation.
        wanted = 1_234_567_890.0  # 2009-02-13
        self.assertTrue(macmeta.set_birthtime(path, wanted))
        self.assertAlmostEqual(path.stat().st_birthtime, wanted, delta=1)

    @darwin_only
    def test_a_date_past_the_32_bit_epoch_survives(self):
        # tv_sec is declared c_long; if it were ever narrowed to 32 bits this
        # is the assertion that would fail rather than silently wrapping.
        path = self.touch()
        wanted = 2_400_000_000.0  # 2046
        self.assertTrue(macmeta.set_birthtime(path, wanted))
        self.assertAlmostEqual(path.stat().st_birthtime, wanted, delta=1)

    @darwin_only
    def test_the_sub_second_part_is_carried_across(self):
        path = self.touch()
        wanted = 1_234_567_890.25
        self.assertTrue(macmeta.set_birthtime(path, wanted))
        self.assertAlmostEqual(path.stat().st_birthtime, wanted, delta=0.01)

    @darwin_only
    def test_a_non_ascii_name_is_encoded_for_the_c_api(self):
        # The path crosses into C as bytes, so an encoding slip would show up
        # as a silent failure on exactly the filenames a photo library has.
        path = self.touch("Ciudad de México 🌞.webp")
        wanted = 1_234_567_890.0
        self.assertTrue(macmeta.set_birthtime(path, wanted))
        self.assertAlmostEqual(path.stat().st_birthtime, wanted, delta=1)

    @darwin_only
    def test_a_missing_file_fails_softly(self):
        # Disposal has already run by the time birthtime is restored, so a
        # raise here would abort a conversion that actually succeeded.
        self.assertFalse(macmeta.set_birthtime(self.root / "nope.webp", 1_000_000.0))

    def test_without_libc_it_is_a_no_op(self):
        path = self.touch()
        with patch.object(macmeta, "_libc", None):
            self.assertFalse(macmeta.set_birthtime(path, 1_234_567_890.0))


@darwin_only
class RoundTripTests(MacMetaTestCase):
    def test_a_birthtime_copied_between_files_matches(self):
        # This is what the converter actually does: read the original's
        # birthtime, write it onto the validated output.
        original = self.touch("original.jpg")
        self.assertTrue(macmeta.set_birthtime(original, 1_111_111_111.0))
        source_birthtime = macmeta.get_birthtime(original)

        output = self.touch("output.webp")
        self.assertTrue(macmeta.set_birthtime(output, source_birthtime))
        self.assertAlmostEqual(
            macmeta.get_birthtime(output), source_birthtime, delta=0.01
        )

    def test_setting_an_older_mtime_does_not_leave_birthtime_ahead_of_it(self):
        # Documented ordering quirk: lowering mtime below birthtime implicitly
        # lowers birthtime too. Pin it, because the converter relies on the
        # utime-then-set_birthtime order being harmless either way.
        path = self.touch()
        macmeta.set_birthtime(path, 1_500_000_000.0)
        os.utime(path, (1_400_000_000.0, 1_400_000_000.0))
        self.assertLessEqual(path.stat().st_birthtime, path.stat().st_mtime)


if __name__ == "__main__":
    unittest.main()
