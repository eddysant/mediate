import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediate.renamer import (
    Rename,
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

    def test_nested_copy_markers_are_all_stripped(self):
        for stem, expected in (
            ("Copy of a", "a"),
            ("Copy of Copy of a", "a"),
            ("Copy of Copy of Copy of a", "a"),
        ):
            with self.subTest(stem=stem):
                parsed = parse_stem(stem)
                self.assertEqual(parsed.base, expected)
                self.assertTrue(parsed.is_dup)

    def test_random_token_takes_folder_name(self):
        self.touch("Nova/ue73up.jpg")
        self.assertEqual(self.plan(), {"ue73up.jpg": "Nova [ue73up].jpg"})

    def test_guid_folder_name_is_cleaned_like_any_other_base(self):
        # Otherwise --rename-folders tidies the directory to "My Holiday
        # Photos" while the files inside still read "my_holiday_photos".
        self.touch("my_holiday_photos/550E8400-E29B-41D4-A716-446655440000.jpg")
        self.touch("my_holiday_photos/ue73up.jpg")
        self.assertEqual(
            self.plan(),
            {
                "550E8400-E29B-41D4-A716-446655440000.jpg":
                "My Holiday Photos [550e8400-e29b-41d4-a716-446655440000].jpg",
                "ue73up.jpg": "My Holiday Photos [ue73up].jpg",
            },
        )

    def test_standardized_names_are_idempotent(self):
        self.touch("Misty Vale [1].jpg")
        self.touch("Misty Vale [2].jpg")
        self.touch("Nova [ue73up].jpg")
        self.touch("Vacation [550e8400-e29b-41d4-a716-446655440000].jpg")
        self.touch("Nova Quinn [Example.com 1].jpg")
        self.assertEqual(self.plan(), {})

    def test_padding_widens_past_ninety_nine(self):
        # A flat width of 2 put "[100]" lexically between "[09]" and "[10]",
        # defeating the only reason padding exists.
        for i in range(1, 106):
            self.touch(f"shot ({i}).jpg")
        plan = self.plan()
        numeric = [plan[f"shot ({i}).jpg"] for i in range(1, 106)]
        self.assertEqual(numeric[0], "Shot [001].jpg")
        self.assertEqual(numeric[-1], "Shot [105].jpg")
        self.assertEqual(sorted(numeric), numeric, "lexical order != numeric order")

    def test_padding_tracks_the_series_size(self):
        for i in range(1, 13):
            self.touch(f"a ({i}).jpg")
        plan = self.plan()
        self.assertEqual(plan["a (1).jpg"], "A [01].jpg")
        self.assertEqual(plan["a (12).jpg"], "A [12].jpg")

    def test_a_short_series_is_not_padded(self):
        for i in range(1, 4):
            self.touch(f"b ({i}).jpg")
        self.assertEqual(self.plan()["b (1).jpg"], "B [1].jpg")

    def test_a_parenthesised_year_is_not_a_duplicate_counter(self):
        # "The Matrix (1999)" became "The Matrix [1]" — the year was read as
        # Finder's duplicate marker and destroyed.
        self.touch("The Matrix (1999).mp4")
        self.touch("Blade Runner (2049).mkv")
        self.assertEqual(self.plan(), {})

    def test_ordinary_duplicate_counters_still_fold(self):
        self.touch("photo (1).jpg")
        self.touch("photo (2).jpg")
        plan = self.plan()
        self.assertEqual(plan["photo (1).jpg"], "Photo [1].jpg")
        self.assertEqual(plan["photo (2).jpg"], "Photo [2].jpg")

    def test_a_large_non_year_number_is_still_a_counter(self):
        self.touch("photo (3000).jpg")
        self.assertEqual(self.plan(), {"photo (3000).jpg": "Photo [1].jpg"})

    def test_initialisms_keep_their_dots_and_are_uppercased(self):
        self.touch("R.E.M. concert.jpg")
        self.touch("e.e. cummings.jpg")
        plan = self.plan()
        self.assertEqual(plan["R.E.M. concert.jpg"], "R.E.M. Concert.jpg")
        self.assertEqual(plan["e.e. cummings.jpg"], "E.E. Cummings.jpg")

    def test_multi_letter_abbreviations_still_lose_their_dot(self):
        # Only a *single* letter before the dot marks an initialism.
        self.touch("Mr. Smith.jpg")
        self.touch("holiday.photo.jpg")
        plan = self.plan()
        self.assertEqual(plan["Mr. Smith.jpg"], "Mr Smith.jpg")
        self.assertEqual(plan["holiday.photo.jpg"], "Holiday Photo.jpg")

    def test_a_date_stamped_name_keeps_its_clock_but_still_tidies_words(self):
        self.touch("2023-01-05 12.30.45.jpg")
        self.touch("2023-01-05 party.jpg")
        plan = self.plan()
        self.assertNotIn("2023-01-05 12.30.45.jpg", plan)  # clock intact
        self.assertEqual(plan["2023-01-05 party.jpg"], "2023-01-05 Party.jpg")

    def test_hyphenated_names_are_still_flattened(self):
        # Deliberate: once numbering is stripped, "Anne-Marie" cannot be told
        # apart from "Tilly-Marsh", and dashes-as-separators is the common case.
        self.touch("Anne-Marie.jpg")
        self.assertEqual(self.plan(), {"Anne-Marie.jpg": "Anne Marie.jpg"})

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


class ManifestDurabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, rel: str, content: bytes = b"x") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_the_manifest_is_replaced_atomically(self):
        self.touch("a.jpg")
        record_batch(self.root, [Rename(self.root / "a.jpg", self.root / "A.jpg")])
        manifest = self.root / ".mediate-renames.json"
        self.assertTrue(manifest.exists())
        leftovers = [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "temp manifest left behind")

    def test_a_failed_manifest_write_keeps_the_previous_history(self):
        self.touch("a.jpg")
        record_batch(self.root, [Rename(self.root / "a.jpg", self.root / "A.jpg")])
        manifest = self.root / ".mediate-renames.json"
        before = manifest.read_text()
        with patch("mediate.renamer.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                record_batch(self.root, [Rename(self.root / "b.jpg", self.root / "B.jpg")])
        self.assertEqual(manifest.read_text(), before)
        leftovers = [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "temp manifest left behind")

    def test_undo_survives_one_unrestorable_entry(self):
        # A single failing rename must not abort the batch with a traceback.
        self.touch("A.jpg", b"one")
        self.touch("B.jpg", b"two")
        record_batch(self.root, [
            Rename(self.root / "a.jpg", self.root / "A.jpg"),
            Rename(self.root / "b.jpg", self.root / "B.jpg"),
        ])
        real_rename = os.rename

        def flaky(src, dst):
            if Path(src).name == "B.jpg":
                raise OSError("simulated failure")
            return real_rename(src, dst)

        with patch("mediate.renamer.os.rename", side_effect=flaky):
            restored = undo_last_batch(self.root, dry_run=False)
        self.assertEqual(restored, 1)
        self.assertTrue((self.root / "a.jpg").exists())   # the one that worked
        self.assertTrue((self.root / "B.jpg").exists())   # the one that failed
        # The batch stays recorded so the remainder can be retried.
        data = json.loads((self.root / ".mediate-renames.json").read_text())
        self.assertEqual(len(data["batches"]), 1)

    def test_a_fully_successful_undo_pops_the_batch(self):
        self.touch("A.jpg", b"one")
        record_batch(self.root, [Rename(self.root / "a.jpg", self.root / "A.jpg")])
        self.assertEqual(undo_last_batch(self.root, dry_run=False), 1)
        data = json.loads((self.root / ".mediate-renames.json").read_text())
        self.assertEqual(data["batches"], [])


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

    def test_same_base_files_are_numbered_rather_than_colliding(self):
        # Both clean to "Misty Vale". Unnumbered they would target one path
        # and apply_renames would skip the loser, which reads as the tool
        # doing nothing; they are numbered into one series instead.
        self.touch("misty_vale.jpg", b"underscore")
        self.touch("misty.vale.jpg", b"dots")
        plans = plan_renames(self.root)
        renamed, skipped, _ = apply_renames(plans, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (2, 0))
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, ["Misty Vale [1].jpg", "Misty Vale.jpg"])
        self.assertEqual(
            sorted(p.read_bytes() for p in self.root.iterdir()),
            [b"dots", b"underscore"],  # nothing overwritten
        )

    def test_a_member_already_named_correctly_is_left_alone(self):
        self.touch("Misty Vale.jpg", b"keep")
        self.touch("misty-vale.jpg", b"extra")
        plans = plan_renames(self.root)
        self.assertEqual([(p.src.name, p.dst.name) for p in plans],
                         [("misty-vale.jpg", "Misty Vale [1].jpg")])

    def test_never_overwrites_an_unrelated_existing_file(self):
        # The never-overwrite guarantee still holds when the occupied target
        # is not part of any series being renumbered.
        self.touch("misty vale.jpg", b"media")
        self.touch("Misty Vale.txt", b"unrelated")
        plans = [Rename(self.root / "misty vale.jpg", self.root / "Misty Vale.txt")]
        renamed, skipped, applied = apply_renames(plans, self.root, dry_run=False)
        self.assertEqual((renamed, skipped, applied), (0, 1, []))
        self.assertEqual((self.root / "Misty Vale.txt").read_bytes(), b"unrelated")
        self.assertTrue((self.root / "misty vale.jpg").exists())

    def test_a_parent_directory_waits_for_its_children(self):
        # A directory rename listed before the files inside it must be
        # deferred, or those files' source paths stop existing mid-batch.
        self.touch("old_dir/a.jpg", b"inner")
        plans = [
            Rename(self.root / "old_dir", self.root / "New Dir"),
            Rename(self.root / "old_dir/a.jpg", self.root / "old_dir/A.jpg"),
        ]
        renamed, skipped, applied = apply_renames(plans, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (2, 0))
        # The child must be recorded first so an undo replays it last.
        self.assertEqual([r.src.name for r in applied], ["a.jpg", "old_dir"])
        self.assertEqual((self.root / "New Dir/A.jpg").read_bytes(), b"inner")

    def test_a_deep_descendant_also_defers_its_ancestor(self):
        self.touch("top/mid/deep/a.jpg", b"inner")
        plans = [
            Rename(self.root / "top", self.root / "Renamed Top"),
            Rename(self.root / "top/mid/deep/a.jpg", self.root / "top/mid/deep/A.jpg"),
        ]
        renamed, skipped, _ = apply_renames(plans, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (2, 0))
        self.assertEqual((self.root / "Renamed Top/mid/deep/A.jpg").read_bytes(), b"inner")

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
