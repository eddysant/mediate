import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from mediate.probe import check_video_integrity
from mediate.progress import ProgressDisplay, ProgressState, format_progress


class TTYStringIO(io.StringIO):
    def isatty(self):
        return True


class ProgressFormattingTests(unittest.TestCase):
    def test_bar_includes_percent_speed_and_eta(self):
        state = ProgressState(
            label="feature-film.mkv",
            action="encoding",
            total=7200,
            current=1800,
            speed=0.5,
        )
        rendered = format_progress(state, width=20)
        self.assertIn("feature-film.mkv", rendered)
        self.assertIn("25%", rendered)
        self.assertIn("0.50x", rendered)
        self.assertIn("ETA 3:00:00", rendered)

    def test_plain_output_reports_more_often_than_quarters(self):
        stream = io.StringIO()
        display = ProgressDisplay()
        display.configure(stream, enabled=True)
        display.start("one", "movie.mkv", "encoding", 100)
        for percent in (1, 10, 20, 30):
            display.update("one", percent, 1.0)
        display.finish("one")
        output = stream.getvalue()
        self.assertIn("10%", output)
        self.assertIn("20%", output)
        self.assertIn("30%", output)

    def test_tty_redraw_keeps_every_concurrent_job_visible(self):
        stream = TTYStringIO()
        display = ProgressDisplay()
        display.configure(stream, enabled=True)
        display.start("one", "movie-a.mkv", "encoding", 100)
        display.start("two", "movie-b.vob", "repairing", 200)
        display.update("one", 40, 0.5)
        display.update("two", 50, 1.0)

        with display.logging_context():
            stream.write("ordinary log message\n")

        latest_redraw = stream.getvalue().rsplit("ordinary log message\n", 1)[1]
        self.assertIn("movie-a.mkv", latest_redraw)
        self.assertIn("movie-b.vob", latest_redraw)
        self.assertIn("40%", latest_redraw)
        self.assertIn("25%", latest_redraw)


class IntegrityProgressTests(unittest.TestCase):
    def test_full_decode_maps_all_video_and_audio_and_uses_progress(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("mediate.probe.media_duration", return_value=90.0), patch(
            "mediate.probe.run_ffmpeg_progress", return_value=completed
        ) as run:
            result = check_video_integrity(Path("movie.mp4"))
        self.assertEqual(result, {"ok": True, "reason": "ok"})
        command, path, total, action = run.call_args.args
        self.assertIn("0:v?", command)
        self.assertIn("0:a?", command)
        self.assertEqual(path, Path("movie.mp4"))
        self.assertEqual(total, 90.0)
        self.assertEqual(action, "validating")


if __name__ == "__main__":
    unittest.main()
