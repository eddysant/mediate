import tempfile
import unittest
from pathlib import Path

from mediate.renamer import apply_renames, clean_base, parse_stem, plan_renames


class ParseStemTests(unittest.TestCase):
    def test_paren_number(self):
        p = parse_stem("eddy sant (1)")
        self.assertEqual((p.base, p.number, p.is_dup), ("eddy sant", 1, False))

    def test_bracket_number(self):
        p = parse_stem("eddy sant [03]")
        self.assertEqual((p.base, p.number, p.is_dup), ("eddy sant", 3, False))

    def test_copy_markers(self):
        self.assertEqual(parse_stem("Copy of party").is_dup, True)
        self.assertEqual(parse_stem("party - copy").is_dup, True)
        p = parse_stem("party copy 2")
        self.assertEqual((p.base, p.number, p.is_dup), ("party", 2, True))

    def test_photocopy_is_not_a_copy_marker(self):
        p = parse_stem("photocopy")
        self.assertEqual((p.base, p.is_dup), ("photocopy", False))

    def test_plain(self):
        p = parse_stem("Terminator 2")
        self.assertEqual((p.base, p.number, p.is_dup), ("Terminator 2", None, False))


class CleanBaseTests(unittest.TestCase):
    def test_title_case_and_separators(self):
        self.assertEqual(clean_base("eddy sant"), "Eddy Sant")
        self.assertEqual(clean_base("eddy_sant"), "Eddy Sant")
        self.assertEqual(clean_base("eddy.sant"), "Eddy Sant")
        self.assertEqual(clean_base("  eddy   sant  "), "Eddy Sant")

    def test_small_words_stay_lower_unless_leading(self):
        self.assertEqual(clean_base("day of the tentacle"), "Day of the Tentacle")
        self.assertEqual(clean_base("the beach"), "The Beach")

    def test_existing_capitalization_is_respected(self):
        self.assertEqual(clean_base("USA trip"), "USA Trip")
        self.assertEqual(clean_base("McDonald visit"), "McDonald Visit")

    def test_protected_names_untouched(self):
        self.assertEqual(clean_base("IMG_1234"), "IMG_1234")
        self.assertEqual(clean_base("PXL_20230101_123456"), "PXL_20230101_123456")
        self.assertEqual(
            clean_base("Screenshot 2023-01-05 at 10.15.30"),
            "Screenshot 2023-01-05 at 10.15.30",
        )


class PlanRenamesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def plan(self):
        return {p.src.name: p.dst.name for p in plan_renames(self.root)}

    def test_basic_example(self):
        self.touch("eddy sant (1).jpg")
        self.assertEqual(self.plan(), {"eddy sant (1).jpg": "Eddy Sant [1].jpg"})

    def test_gap_closing_and_padding(self):
        for n in [1, 2, 4, 5, 6, 7, 8, 9, 10, 11]:
            self.touch(f"eddy sant ({n}).jpg")
        plan = self.plan()
        self.assertEqual(plan["eddy sant (1).jpg"], "Eddy Sant [01].jpg")
        self.assertEqual(plan["eddy sant (4).jpg"], "Eddy Sant [03].jpg")
        self.assertEqual(plan["eddy sant (11).jpg"], "Eddy Sant [10].jpg")

    def test_no_padding_under_ten(self):
        self.touch("trip (1).jpg")
        self.touch("trip (3).jpg")
        plan = self.plan()
        self.assertEqual(plan["trip (1).jpg"], "Trip [1].jpg")
        self.assertEqual(plan["trip (3).jpg"], "Trip [2].jpg")

    def test_guid_takes_folder_name(self):
        self.touch("Vacation 2019/550E8400-E29B-41D4-A716-446655440000.jpg")
        self.assertEqual(
            self.plan(),
            {
                "550E8400-E29B-41D4-A716-446655440000.jpg":
                "Vacation 2019 [550e8400-e29b-41d4-a716-446655440000].jpg"
            },
        )

    def test_camera_names_only_get_extension_lowered(self):
        self.touch("IMG_1234.JPG")
        self.touch("IMG_5678.jpg")
        self.assertEqual(self.plan(), {"IMG_1234.JPG": "IMG_1234.jpg"})

    def test_copy_of_joins_numbering(self):
        self.touch("party.jpg")
        self.touch("Copy of party.jpg")
        plan = self.plan()
        self.assertEqual(plan["party.jpg"], "Party.jpg")
        self.assertEqual(plan["Copy of party.jpg"], "Party [1].jpg")

    def test_lone_copy_becomes_plain(self):
        self.touch("Copy of party.jpg")
        self.assertEqual(self.plan(), {"Copy of party.jpg": "Party.jpg"})

    def test_live_photo_mov_mirrors_still(self):
        self.touch("beach day (1).heic")
        self.touch("beach day (1).mov")
        plan = self.plan()
        self.assertEqual(plan["beach day (1).heic"], "Beach Day [1].heic")
        self.assertEqual(plan["beach day (1).mov"], "Beach Day [1].mov")

    def test_sidecar_follows_media(self):
        self.touch("eddy sant (1).jpg")
        self.touch("eddy sant (1).AAE")
        plan = self.plan()
        self.assertEqual(plan["eddy sant (1).AAE"], "Eddy Sant [1].aae")

    def test_series_are_per_extension(self):
        self.touch("trip (1).jpg")
        self.touch("trip (3).png")
        plan = self.plan()
        self.assertEqual(plan["trip (1).jpg"], "Trip [1].jpg")
        self.assertEqual(plan["trip (3).png"], "Trip [1].png")


class ApplyRenamesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, rel: str, content: bytes = b"x") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_never_overwrites_on_collision(self):
        self.touch("eddy_sant.jpg", b"underscore")
        self.touch("eddy.sant.jpg", b"dots")
        plans = plan_renames(self.root)
        renamed, skipped = apply_renames(plans, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (1, 1))
        names = sorted(p.name for p in self.root.iterdir())
        self.assertIn("Eddy Sant.jpg", names)
        self.assertEqual(len(names), 2)  # loser kept its old name, nothing lost

    def test_gap_close_waits_for_occupied_slot(self):
        # [2] -> [1] and [3] -> [2]: the second rename targets a slot that
        # is occupied until the first happens.
        self.touch("trip [2].jpg")
        self.touch("trip [3].jpg")
        plans = plan_renames(self.root)
        renamed, skipped = apply_renames(plans, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (2, 0))
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["Trip [1].jpg", "Trip [2].jpg"],
        )

    def test_dry_run_touches_nothing(self):
        self.touch("eddy sant (1).jpg")
        plans = plan_renames(self.root)
        apply_renames(plans, self.root, dry_run=True)
        self.assertEqual(
            [p.name for p in self.root.iterdir()], ["eddy sant (1).jpg"]
        )


if __name__ == "__main__":
    unittest.main()
