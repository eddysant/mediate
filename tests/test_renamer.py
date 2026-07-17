import tempfile
import unittest
from pathlib import Path

from mediate.renamer import (
    apply_renames,
    clean_base,
    looks_random,
    parse_stem,
    plan_folder_renames,
    plan_renames,
    record_batch,
    undo_last_batch,
)


class ParseStemTests(unittest.TestCase):
    def test_paren_number(self):
        p = parse_stem("misty vale (1)")
        self.assertEqual((p.base, p.number, p.is_dup), ("misty vale", 1, False))

    def test_bracket_number(self):
        p = parse_stem("misty vale [03]")
        self.assertEqual((p.base, p.number, p.is_dup), ("misty vale", 3, False))

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

    def test_dash_number(self):
        p = parse_stem("Wren Tally - 2")
        self.assertEqual((p.base, p.number), ("Wren Tally", 2))
        p = parse_stem("Tilly-Marsh-001")
        self.assertEqual((p.base, p.number), ("Tilly-Marsh", 1))

    def test_date_stem_is_not_dash_numbered(self):
        p = parse_stem("2023-01-05")
        self.assertEqual((p.base, p.number), ("2023-01-05", None))

    def test_site_extraction(self):
        p = parse_stem("Nova-Quinn-Example.com-4")
        self.assertEqual((p.base, p.number, p.site), ("Nova-Quinn-", 4, "Example.com"))

    def test_existing_tag_roundtrip(self):
        p = parse_stem("Nova Quinn [Example.com 04]")
        self.assertEqual((p.base, p.number, p.site), ("Nova Quinn", 4, "Example.com"))
        p = parse_stem("Misty Vale [01]")
        self.assertEqual((p.base, p.number, p.site), ("Misty Vale", 1, None))

    def test_unknown_bracket_tag_is_opaque(self):
        self.assertTrue(parse_stem("Nova [ue73up]").opaque)
        self.assertTrue(
            parse_stem("Vacation [550e8400-e29b-41d4-a716-446655440000]").opaque
        )


class LooksRandomTests(unittest.TestCase):
    def test_random_tokens(self):
        self.assertTrue(looks_random("ue73up"))
        self.assertTrue(looks_random("x9k2mq31"))

    def test_meaningful_names_are_not_random(self):
        for stem in ("photo2023", "party2023", "4kvideo", "IMG1234", "misty vale", "holiday"):
            self.assertFalse(looks_random(stem), stem)


class CleanBaseTests(unittest.TestCase):
    def test_title_case_and_separators(self):
        self.assertEqual(clean_base("misty vale"), "Misty Vale")
        self.assertEqual(clean_base("misty_vale"), "Misty Vale")
        self.assertEqual(clean_base("misty.vale"), "Misty Vale")
        self.assertEqual(clean_base("  misty   vale  "), "Misty Vale")

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
        self.touch("misty vale (1).jpg")
        self.assertEqual(self.plan(), {"misty vale (1).jpg": "Misty Vale [1].jpg"})

    def test_gap_closing_and_padding(self):
        for n in [1, 2, 4, 5, 6, 7, 8, 9, 10, 11]:
            self.touch(f"misty vale ({n}).jpg")
        plan = self.plan()
        self.assertEqual(plan["misty vale (1).jpg"], "Misty Vale [01].jpg")
        self.assertEqual(plan["misty vale (4).jpg"], "Misty Vale [03].jpg")
        self.assertEqual(plan["misty vale (11).jpg"], "Misty Vale [10].jpg")

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
        self.touch("misty vale (1).jpg")
        self.touch("misty vale (1).AAE")
        plan = self.plan()
        self.assertEqual(plan["misty vale (1).AAE"], "Misty Vale [1].aae")

    def test_series_are_per_extension(self):
        self.touch("trip (1).jpg")
        self.touch("trip (3).png")
        plan = self.plan()
        self.assertEqual(plan["trip (1).jpg"], "Trip [1].jpg")
        self.assertEqual(plan["trip (3).png"], "Trip [1].png")

    def test_dashed_name_with_dash_numbering(self):
        self.touch("Tilly-Marsh-001.jpg")
        self.touch("Tilly-Marsh-003.jpg")
        plan = self.plan()
        self.assertEqual(plan["Tilly-Marsh-001.jpg"], "Tilly Marsh [1].jpg")
        self.assertEqual(plan["Tilly-Marsh-003.jpg"], "Tilly Marsh [2].jpg")

    def test_dash_number_starts_at_one(self):
        self.touch("Wren Tally - 2.jpg")
        self.assertEqual(self.plan(), {"Wren Tally - 2.jpg": "Wren Tally [1].jpg"})

    def test_website_moves_into_tag(self):
        self.touch("Nova-Quinn-Example.com-4.jpg")
        self.assertEqual(
            self.plan(),
            {"Nova-Quinn-Example.com-4.jpg": "Nova Quinn [Example.com 1].jpg"},
        )

    def test_site_series_are_separate(self):
        self.touch("Nova-Quinn-Example.com-4.jpg")
        self.touch("Nova-Quinn-Example.com-7.jpg")
        self.touch("Nova-Quinn-2.jpg")
        plan = self.plan()
        self.assertEqual(plan["Nova-Quinn-Example.com-4.jpg"], "Nova Quinn [Example.com 1].jpg")
        self.assertEqual(plan["Nova-Quinn-Example.com-7.jpg"], "Nova Quinn [Example.com 2].jpg")
        self.assertEqual(plan["Nova-Quinn-2.jpg"], "Nova Quinn [1].jpg")

    def test_random_token_takes_folder_name(self):
        self.touch("Nova/ue73up.jpg")
        self.assertEqual(self.plan(), {"ue73up.jpg": "Nova [ue73up].jpg"})

    def test_standardized_names_are_idempotent(self):
        self.touch("Misty Vale [1].jpg")
        self.touch("Misty Vale [2].jpg")
        self.touch("Nova [ue73up].jpg")
        self.touch("Vacation [550e8400-e29b-41d4-a716-446655440000].jpg")
        self.touch("Nova Quinn [Example.com 1].jpg")
        self.assertEqual(self.plan(), {})

    def test_date_stems_survive_cleanup(self):
        self.touch("2023-01-05 party.jpg")
        self.assertEqual(self.plan(), {"2023-01-05 party.jpg": "2023-01-05 Party.jpg"})

    def test_date_prefix_uses_mtime_fallback(self):
        import os

        p = self.touch("misty vale.jpg")
        os.utime(p, (1577975400, 1577975400))  # 2020-01-02 local time
        plans = {r.src.name: r.dst.name for r in plan_renames(self.root, date_prefix=True)}
        self.assertEqual(plans, {"misty vale.jpg": "2020-01-02 Misty Vale.jpg"})


class FolderRenameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_folders_cleaned_deepest_first(self):
        (self.root / "summer_trip/beach_day").mkdir(parents=True)
        plans = plan_folder_renames(self.root)
        self.assertEqual(
            [(p.src.name, p.dst.name) for p in plans],
            [("beach_day", "Beach Day"), ("summer_trip", "Summer Trip")],
        )


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
        self.touch("misty_vale.jpg", b"underscore")
        self.touch("misty.vale.jpg", b"dots")
        plans = plan_renames(self.root)
        renamed, skipped, _ = apply_renames(plans, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (1, 1))
        names = sorted(p.name for p in self.root.iterdir())
        self.assertIn("Misty Vale.jpg", names)
        self.assertEqual(len(names), 2)  # loser kept its old name, nothing lost

    def test_gap_close_waits_for_occupied_slot(self):
        # [2] -> [1] and [3] -> [2]: the second rename targets a slot that
        # is occupied until the first happens.
        self.touch("trip [2].jpg")
        self.touch("trip [3].jpg")
        plans = plan_renames(self.root)
        renamed, skipped, _ = apply_renames(plans, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (2, 0))
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["Trip [1].jpg", "Trip [2].jpg"],
        )

    def test_dry_run_touches_nothing(self):
        self.touch("misty vale (1).jpg")
        plans = plan_renames(self.root)
        apply_renames(plans, self.root, dry_run=True)
        self.assertEqual(
            [p.name for p in self.root.iterdir()], ["misty vale (1).jpg"]
        )

    def test_undo_restores_last_batch(self):
        self.touch("misty vale (1).jpg")
        self.touch("Tilly-Marsh-001.jpg")
        plans = plan_renames(self.root)
        renamed, _, applied = apply_renames(plans, self.root, dry_run=False)
        record_batch(self.root, applied)
        self.assertEqual(renamed, 2)
        self.assertIn(".mediate-renames.json", [p.name for p in self.root.iterdir()])
        restored = undo_last_batch(self.root, dry_run=False)
        self.assertEqual(restored, 2)
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir() if not p.name.startswith(".")),
            ["Tilly-Marsh-001.jpg", "misty vale (1).jpg"],
        )
        # A second undo has nothing left to reverse.
        self.assertEqual(undo_last_batch(self.root, dry_run=False), 0)


if __name__ == "__main__":
    unittest.main()
