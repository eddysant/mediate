"""Coverage for where originals go after a successful conversion.

Only hard-delete was previously exercised, and only incidentally by the
FFmpeg integration tests — yet Trash is the *default*, so this is the code
that runs on an ordinary invocation and moves the user's originals.

Every test here redirects HOME/XDG_DATA_HOME into a temporary directory, so
nothing can reach the real Trash.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediate.disposal import (
    GRAVEYARD,
    HARD,
    TRASH,
    DisposalPolicy,
    _same_volume,
    _send_to_trash,
    _trash_dir_for,
    _unique_dest,
    make_disposer,
)


class DisposalTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.home = self.root / "home"
        (self.home / ".Trash").mkdir(parents=True)
        # Path.home() reads HOME, so this keeps every Trash operation inside
        # the temporary directory instead of the real one.
        env = patch.dict(
            os.environ,
            {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home / ".local/share")},
        )
        env.start()
        self.addCleanup(env.stop)

    def touch(self, rel: str, content: bytes = b"x") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class UniqueDestTests(DisposalTestCase):
    def test_an_unused_name_is_returned_as_is(self):
        self.assertEqual(_unique_dest(self.root, "a.jpg"), self.root / "a.jpg")

    def test_collisions_count_up_from_two(self):
        self.touch("a.jpg")
        self.assertEqual(_unique_dest(self.root, "a.jpg"), self.root / "a 2.jpg")
        self.touch("a 2.jpg")
        self.assertEqual(_unique_dest(self.root, "a.jpg"), self.root / "a 3.jpg")

    def test_a_name_without_a_suffix_is_handled(self):
        self.touch("README")
        self.assertEqual(_unique_dest(self.root, "README"), self.root / "README 2")


class TrashDirTests(DisposalTestCase):
    def test_macos_uses_the_home_trash_on_the_same_volume(self):
        media = self.touch("a.jpg")
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(_trash_dir_for(media), self.home / ".Trash")

    def test_macos_prefers_the_files_own_volume(self):
        # Moving a large video from an external drive to ~/.Trash would be a
        # full copy rather than a rename, so a volume with its own .Trashes
        # must win.
        volume = self.root / "Volumes/Media"
        (volume / ".Trashes").mkdir(parents=True)
        media = self.touch("Volumes/Media/movie.mov")
        with patch.object(sys, "platform", "darwin"), \
                patch("mediate.disposal._same_volume", return_value=False):
            resolved = _trash_dir_for(media)
        self.assertEqual(resolved, volume.resolve() / ".Trashes" / str(os.getuid()))
        self.assertTrue(resolved.is_dir())

    def test_macos_falls_back_to_home_when_the_volume_has_no_trashes(self):
        media = self.touch("Volumes/Media/movie.mov")
        with patch.object(sys, "platform", "darwin"), \
                patch("mediate.disposal._same_volume", return_value=False):
            self.assertEqual(_trash_dir_for(media), self.home / ".Trash")

    def test_freedesktop_layout_elsewhere(self):
        media = self.touch("a.jpg")
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(
                _trash_dir_for(media), self.home / ".local/share/Trash/files"
            )

    def test_same_volume_treats_an_unreadable_path_as_same(self):
        # Unknown must not send a file to a volume Trash that may not exist.
        self.assertTrue(_same_volume(self.root / "missing", self.root))


class SendToTrashTests(DisposalTestCase):
    def test_the_file_is_moved_out_of_the_library(self):
        media = self.touch("a.jpg", b"payload")
        with patch.object(sys, "platform", "darwin"):
            message = _send_to_trash(media)
        self.assertFalse(media.exists())
        trashed = self.home / ".Trash/a.jpg"
        self.assertEqual(trashed.read_bytes(), b"payload")
        self.assertIn("Trash", message)

    def test_a_name_already_in_the_trash_is_not_overwritten(self):
        (self.home / ".Trash/a.jpg").write_bytes(b"older")
        media = self.touch("a.jpg", b"newer")
        with patch.object(sys, "platform", "darwin"):
            _send_to_trash(media)
        self.assertEqual((self.home / ".Trash/a.jpg").read_bytes(), b"older")
        self.assertEqual((self.home / ".Trash/a 2.jpg").read_bytes(), b"newer")

    def test_freedesktop_writes_restorable_trashinfo(self):
        media = self.touch("a.jpg", b"payload")
        with patch.object(sys, "platform", "linux"):
            _send_to_trash(media)
        files = self.home / ".local/share/Trash/files"
        info = self.home / ".local/share/Trash/info/a.jpg.trashinfo"
        self.assertEqual((files / "a.jpg").read_bytes(), b"payload")
        body = info.read_text()
        self.assertIn("[Trash Info]", body)
        self.assertIn(f"Path={media}", body)
        self.assertIn("DeletionDate=", body)

    def test_an_unwritable_info_dir_still_leaves_the_file_trashed(self):
        # The file is already safely in the Trash; missing UI metadata must
        # not raise, or the transaction would try to roll back from a source
        # path that no longer exists.
        media = self.touch("a.jpg", b"payload")
        with patch.object(sys, "platform", "linux"), \
                patch("mediate.disposal.Path.write_text", side_effect=OSError("nope")):
            message = _send_to_trash(media)
        self.assertFalse(media.exists())
        self.assertTrue((self.home / ".local/share/Trash/files/a.jpg").exists())
        self.assertIn("Trash", message)

    def test_the_original_path_names_the_trashed_file(self):
        # Disposal runs on a staged copy, so the name must come from the
        # logical original rather than the temporary staging path.
        staged = self.touch("staged.tmp", b"payload")
        with patch.object(sys, "platform", "darwin"):
            _send_to_trash(staged, original_path=self.root / "holiday.jpg")
        self.assertTrue((self.home / ".Trash/holiday.jpg").exists())


class GraveyardTests(DisposalTestCase):
    def test_the_library_structure_is_mirrored(self):
        grave = self.root / "grave"
        media = self.touch("2019/summer/a.jpg", b"payload")
        policy = DisposalPolicy(GRAVEYARD, self.root.resolve(), grave)
        message = policy(media)
        self.assertFalse(media.exists())
        self.assertEqual((grave / "2019/summer/a.jpg").read_bytes(), b"payload")
        self.assertIn("2019/summer", message)

    def test_a_file_outside_the_root_lands_at_the_top(self):
        grave = self.root / "grave"
        outside = Path(self._tmp.name).parent / "stray-disposal-test.jpg"
        outside.write_bytes(b"payload")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        policy = DisposalPolicy(GRAVEYARD, (self.root / "library").resolve(), grave)
        policy(outside)
        self.assertTrue((grave / "stray-disposal-test.jpg").exists())

    def test_graveyard_collisions_do_not_overwrite(self):
        grave = self.root / "grave"
        policy = DisposalPolicy(GRAVEYARD, self.root.resolve(), grave)
        first = self.touch("a.jpg", b"first")
        policy(first)
        second = self.touch("a.jpg", b"second")
        policy(second)
        self.assertEqual((grave / "a.jpg").read_bytes(), b"first")
        self.assertEqual((grave / "a 2.jpg").read_bytes(), b"second")


class PolicyTests(DisposalTestCase):
    def test_hard_delete_removes_the_file(self):
        media = self.touch("a.jpg")
        self.assertEqual(DisposalPolicy(HARD, self.root)(media), "original deleted")
        self.assertFalse(media.exists())

    def test_an_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            DisposalPolicy("incinerate", self.root)

    def test_graveyard_without_a_destination_is_rejected(self):
        with self.assertRaises(ValueError):
            DisposalPolicy(GRAVEYARD, self.root)

    def test_round_trips_through_a_transaction_manifest(self):
        for policy in (
            DisposalPolicy(HARD, self.root.resolve()),
            DisposalPolicy(TRASH, self.root.resolve()),
            DisposalPolicy(GRAVEYARD, self.root.resolve(), (self.root / "g").resolve()),
        ):
            with self.subTest(mode=policy.mode):
                restored = DisposalPolicy.from_dict(policy.to_dict())
                self.assertEqual(restored, policy)

    def test_a_manifest_with_an_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            DisposalPolicy.from_dict({"mode": "incinerate", "root": str(self.root)})

    def test_a_graveyard_manifest_without_a_destination_is_rejected(self):
        with self.assertRaises(ValueError):
            DisposalPolicy.from_dict(
                {"mode": GRAVEYARD, "root": str(self.root), "graveyard": None}
            )


class MakeDisposerTests(DisposalTestCase):
    def test_trash_is_the_described_default(self):
        policy, label = make_disposer(TRASH, self.root, None)
        self.assertEqual(policy.mode, TRASH)
        self.assertEqual(label, "move original to Trash")

    def test_hard_delete_is_labelled_plainly(self):
        policy, label = make_disposer(HARD, self.root, None)
        self.assertEqual(policy.mode, HARD)
        self.assertEqual(label, "delete original")

    def test_a_graveyard_path_is_expanded_and_resolved(self):
        policy, label = make_disposer(GRAVEYARD, self.root, Path("~/grave"))
        expected = (self.home / "grave").resolve()
        self.assertEqual(policy.graveyard, expected)
        self.assertIn(str(expected), label)


if __name__ == "__main__":
    unittest.main()
