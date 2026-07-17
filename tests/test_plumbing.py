import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from mediate.cli import load_config_args
from mediate.exiftool import exiftool_available, run_exiftool
from mediate.renamer import Rename, apply_renames, load_plan, plan_renames, write_plan


class PlanFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_roundtrip_and_apply(self):
        self.touch("eddy sant (1).jpg")
        plan_path = self.root / "plan.json"
        write_plan(self.root, plan_renames(self.root), plan_path)
        loaded = load_plan(self.root, plan_path)
        self.assertEqual(
            [(p.src.name, p.dst.name) for p in loaded],
            [("eddy sant (1).jpg", "Eddy Sant [1].jpg")],
        )
        renamed, skipped, _ = apply_renames(loaded, self.root, dry_run=False)
        self.assertEqual((renamed, skipped), (1, 0))
        self.assertTrue((self.root / "Eddy Sant [1].jpg").exists())

    def test_edited_plan_is_honored(self):
        self.touch("a.jpg")
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps({"renames": [{"from": "a.jpg", "to": "Chosen Name.jpg"}]}))
        loaded = load_plan(self.root, plan_path)
        apply_renames(loaded, self.root, dry_run=False)
        self.assertTrue((self.root / "Chosen Name.jpg").exists())

    def test_plan_cannot_escape_root(self):
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps({"renames": [{"from": "a.jpg", "to": "../evil.jpg"}]}))
        with self.assertRaises(ValueError):
            load_plan(self.root, plan_path)
        plan_path.write_text(json.dumps({"renames": [{"from": "/etc/passwd", "to": "x.jpg"}]}))
        with self.assertRaises(ValueError):
            load_plan(self.root, plan_path)


class ConfigTests(unittest.TestCase):
    def test_reads_flags_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            cfg.write_text(
                "# my defaults\n--only-if-smaller\n\n--workers 4\n--convert-heic\n"
            )
            old = os.environ.get("MEDIATE_CONFIG")
            os.environ["MEDIATE_CONFIG"] = str(cfg)
            try:
                self.assertEqual(
                    load_config_args(),
                    ["--only-if-smaller", "--workers", "4", "--convert-heic"],
                )
            finally:
                if old is None:
                    del os.environ["MEDIATE_CONFIG"]
                else:
                    os.environ["MEDIATE_CONFIG"] = old

    def test_missing_file_is_empty(self):
        old = os.environ.get("MEDIATE_CONFIG")
        os.environ["MEDIATE_CONFIG"] = "/nonexistent/mediate-config"
        try:
            self.assertEqual(load_config_args(), [])
        finally:
            if old is None:
                del os.environ["MEDIATE_CONFIG"]
            else:
                os.environ["MEDIATE_CONFIG"] = old


class ExifToolDaemonTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("exiftool"), "exiftool not installed")
    def test_daemon_answers_repeatedly(self):
        self.assertTrue(exiftool_available())
        first = run_exiftool(["-ver"])
        second = run_exiftool(["-ver"])
        self.assertTrue(first and first.strip())
        self.assertEqual(first, second)

    @unittest.skipIf(shutil.which("exiftool"), "exiftool installed")
    def test_returns_none_without_exiftool(self):
        self.assertIsNone(run_exiftool(["-ver"]))


if __name__ == "__main__":
    unittest.main()
