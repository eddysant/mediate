import tempfile
import unittest
from pathlib import Path

from mediate.scanner import find_live_photo_companions, iter_media


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def scan(self):
        return {job.path.relative_to(self.root).as_posix(): job.kind for job in iter_media(self.root)}

    def test_classifies_by_extension(self):
        self.touch("a.jpg")
        self.touch("b.PNG")
        self.touch("c.gif")
        self.touch("d.mov")
        self.touch("e.mp4")
        self.touch("f.heic")
        self.assertEqual(
            self.scan(),
            {
                "a.jpg": "photo", "b.PNG": "photo", "c.gif": "gif",
                "d.mov": "video", "e.mp4": "mp4", "f.heic": "heic",
            },
        )

    def test_recurses_subdirectories(self):
        self.touch("sub/deep/x.tiff")
        self.assertEqual(self.scan(), {"sub/deep/x.tiff": "photo"})

    def test_skips_hidden_and_standardized(self):
        self.touch(".DS_Store")
        self.touch(".hidden.jpg")
        self.touch(".secret/inside.jpg")
        self.touch("already.webp")
        self.touch("notes.txt")
        self.touch("conversion.log")
        self.assertEqual(self.scan(), {})

    def test_never_enters_application_bundles(self):
        self.touch("Photos Library.photoslibrary/originals/img.jpg")
        self.touch("Some.app/Contents/logo.png")
        self.touch("Cut.fcpbundle/clip.mov")
        self.touch("safe/img.jpg")
        self.assertEqual(self.scan(), {"safe/img.jpg": "photo"})


class LivePhotoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def jobs(self):
        return list(iter_media(self.root))

    def test_pairs_mov_with_same_stem_still(self):
        heic = self.touch("IMG_0001.heic")
        mov = self.touch("IMG_0001.mov")
        self.touch("IMG_0002.mov")  # no still: not a pair
        companions = find_live_photo_companions(self.jobs())
        self.assertEqual(companions, {mov: heic})

    def test_pairing_is_per_directory_and_case_insensitive(self):
        jpg = self.touch("a/img_5.JPG")
        mov = self.touch("a/IMG_5.mov")
        self.touch("b/IMG_5.mov")  # same stem, different dir: not a pair
        companions = find_live_photo_companions(self.jobs())
        self.assertEqual(companions, {mov: jpg})

    def test_only_mov_counts_as_companion(self):
        self.touch("clip.jpg")
        self.touch("clip.mkv")  # same stem but not .mov
        self.assertEqual(find_live_photo_companions(self.jobs()), {})


if __name__ == "__main__":
    unittest.main()
