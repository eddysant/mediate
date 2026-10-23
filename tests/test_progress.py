import io
import os
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


class TerminalWidthTests(unittest.TestCase):
    """A line wider than the terminal wraps, and the redraw erases one row
    per job — so an over-long filename used to smear instead of update."""

    LONG = "2019-06-01 Family Reunion At The Lake House [04].mov"

    def state(self, label=None):
        return ProgressState(
            label=label or self.LONG, action="encoding",
            total=600.0, current=240.0, speed=1.73,
        )

    def test_every_width_produces_a_line_that_fits(self):
        for columns in (160, 120, 100, 80, 70, 60, 50, 40, 30, 20):
            with self.subTest(columns=columns):
                line = format_progress(self.state(), max_columns=columns)
                self.assertLessEqual(len(line), columns, repr(line))

    def test_a_pathological_filename_still_fits(self):
        state = self.state("x" * 400 + ".mov")
        for columns in (80, 40, 20):
            with self.subTest(columns=columns):
                self.assertLessEqual(
                    len(format_progress(state, max_columns=columns)), columns
                )

    def test_an_unbounded_line_is_still_available(self):
        # max_columns=None keeps the old behaviour for plain (non-TTY) logs,
        # where wrapping is harmless because nothing is redrawn.
        line = format_progress(self.state())
        self.assertIn(self.LONG, line)

    def test_elision_keeps_the_start_and_the_extension(self):
        line = format_progress(self.state(), max_columns=80)
        self.assertIn("2019-06-01", line)
        self.assertIn(".mov", line)
        self.assertIn("…", line)

    def test_a_narrow_terminal_keeps_the_filename_and_percent(self):
        # Truncating the composed line instead would leave a fragment of the
        # ETA and no way to tell which file is running.
        line = format_progress(self.state(), max_columns=30)
        self.assertLessEqual(len(line), 30)
        self.assertIn("40%", line)
        self.assertIn(".mov", line)
        self.assertNotIn("ETA", line)

    def test_the_bar_yields_columns_to_the_filename(self):
        # The filename is what the line is for; the bar is decoration.
        narrow = format_progress(self.state(), max_columns=80)
        wide = format_progress(self.state(), max_columns=160)
        self.assertLess(narrow.count("-") + narrow.count("="), wide.count("-") + wide.count("="))

    def test_rendered_rows_match_what_was_erased(self):
        stream = TTYStringIO()
        display = ProgressDisplay()
        with patch("mediate.progress.shutil.get_terminal_size",
                   return_value=os.terminal_size((80, 24))):
            display.configure(stream, enabled=True)
            display.start("one", self.LONG, "encoding", 600)
            display.start("two", self.LONG.replace("04", "05"), "encoding", 600)
            display.update("one", 240, 1.73)
            for line in stream.getvalue().splitlines():
                self.assertLessEqual(len(line), 80, repr(line))
            display.clear()
        # Nothing of the progress block should remain pending on screen.
        self.assertEqual(display._rendered_rows, 0)
        self.assertEqual(display._rendered_lines, 0)

    def test_a_resize_is_picked_up_on_the_next_draw(self):
        stream = TTYStringIO()
        display = ProgressDisplay()
        with patch("mediate.progress.shutil.get_terminal_size",
                   return_value=os.terminal_size((120, 24))):
            display.configure(stream, enabled=True)
            display.start("one", self.LONG, "encoding", 600)
        mark = len(stream.getvalue())
        with patch("mediate.progress.shutil.get_terminal_size",
                   return_value=os.terminal_size((50, 24))):
            display.start("two", "clip.mkv", "encoding", 600)
        for line in stream.getvalue()[mark:].splitlines():
            self.assertLessEqual(len(line), 50, repr(line))


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
