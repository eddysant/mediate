"""Thread-safe live progress display for concurrent FFmpeg jobs."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, TextIO


def _clock(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    value = int(seconds)
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


@dataclass
class ProgressState:
    label: str
    action: str
    total: Optional[float]
    current: float = 0.0
    speed: Optional[float] = None
    started: float = 0.0
    last_plain: float = 0.0
    last_plain_percent: int = -10

    @property
    def percent(self) -> Optional[int]:
        if not self.total or self.total <= 0:
            return None
        return max(0, min(100, int(self.current / self.total * 100)))


# A label shorter than this tells you nothing, and a bar shorter than this
# cannot show progress; below both, parts are dropped instead of shrunk.
MIN_LABEL_COLUMNS = 14
MIN_BAR_COLUMNS = 8
# Past this the label is long enough to identify a file; extra columns are
# better spent widening the bar.
LABEL_TARGET_COLUMNS = 36


def _elide(text: str, limit: int) -> str:
    """Shorten text from the middle, keeping the start and the extension.

    Media names are front-loaded with the distinguishing part and end with a
    meaningful suffix, so trimming the middle keeps a line identifiable where
    a plain truncation would leave several jobs looking identical.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    suffix = Path(text).suffix
    tail = suffix if len(suffix) + 2 <= limit else ""
    return text[: limit - 1 - len(tail)] + "…" + tail


def _bar(percent: Optional[int], width: int) -> str:
    if percent is None:
        return "?" * width
    filled = min(width, int(width * percent / 100))
    return ("=" * filled + (">" if filled < width else "")).ljust(width, "-")


def format_progress(
    state: ProgressState, width: int = 22, max_columns: Optional[int] = None
) -> str:
    """Render one job as a single line.

    With max_columns set, the line is guaranteed to fit: optional fields are
    dropped, then the bar shrinks, then the label is elided. A line wider than
    the terminal wraps onto extra physical rows, and the redraw erases one row
    per job — so an over-long filename used to smear the display instead of
    updating it.
    """
    percent = state.percent
    percent_text = "  --%" if percent is None else f"{percent:4d}%"
    timing = f"{_clock(state.current)}/{_clock(state.total)}"
    speed = f" {state.speed:.2f}x" if state.speed else ""
    eta = ""
    if state.total and state.speed and state.speed > 0:
        eta = f" ETA {_clock(max(0.0, state.total - state.current) / state.speed)}"

    def compose(label: str, bar_width: int, tail: str) -> str:
        return f"{state.action:<10} {label} [{_bar(percent, bar_width)}]{tail}"

    full_tail = f" {percent_text} {timing}{speed}{eta}"
    if max_columns is None:
        return compose(state.label, width, full_tail)

    budget = max(1, max_columns)
    # Each candidate keeps less: ETA goes first, then speed, then the clock.
    for tail in (
        full_tail,
        f" {percent_text} {timing}{speed}",
        f" {percent_text} {timing}",
        f" {percent_text}",
    ):
        floor = len(compose("", MIN_BAR_COLUMNS, tail))
        spare = budget - floor
        if spare < MIN_LABEL_COLUMNS:
            continue  # will not fit even with the narrowest bar
        # The filename is what the line is *for*, so it is served before the
        # bar grows past its minimum; any remainder goes back to the label.
        wanted = min(len(state.label), LABEL_TARGET_COLUMNS, spare)
        bar_width = min(width, MIN_BAR_COLUMNS + spare - wanted)
        room = budget - len(compose("", bar_width, tail))
        return compose(_elide(state.label, room), bar_width, tail)
    # Too narrow for any structure. The filename and the percent are what
    # remain useful; truncating the composed line instead would leave a
    # fragment of the ETA and no way to tell which file is running.
    if budget > len(percent_text) + 2:
        label = _elide(state.label, budget - len(percent_text) - 1)
        return f"{label} {percent_text}"
    return _elide(percent_text.strip(), budget)


class ProgressDisplay:
    """Render all active jobs as stable terminal lines, or periodic plain logs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: "OrderedDict[str, ProgressState]" = OrderedDict()
        self._stream: TextIO = sys.stdout
        self._enabled = False
        self._tty = False
        self._rendered_lines = 0
        self._rendered_rows = 0
        self._last_draw = 0.0

    def configure(self, stream: TextIO, enabled: bool = True) -> None:
        with self._lock:
            self._clear_locked()
            self._stream = stream
            self._enabled = enabled
            self._tty = bool(enabled and getattr(stream, "isatty", lambda: False)())

    def start(self, key: str, label: str, action: str, total: Optional[float]) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        with self._lock:
            self._states[key] = ProgressState(label, action, total, started=now, last_plain=now)
            self._draw_locked(force=True)

    def update(self, key: str, current: float, speed: Optional[float]) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.current = max(0.0, current)
            state.speed = speed
            if self._tty:
                self._draw_locked(force=False)
                return
            percent = state.percent
            advanced = percent is not None and percent >= state.last_plain_percent + 10
            if advanced or now - state.last_plain >= 60:
                self._stream.write(format_progress(state) + "\n")
                self._stream.flush()
                state.last_plain = now
                if percent is not None:
                    state.last_plain_percent = percent - percent % 10

    def finish(self, key: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._clear_locked()
            self._states.pop(key, None)
            self._render_locked()

    def clear(self) -> None:
        with self._lock:
            self._clear_locked()
            self._states.clear()

    @contextlib.contextmanager
    def logging_context(self):
        if not self._enabled or not self._tty:
            with self._lock:
                yield
            return
        with self._lock:
            self._clear_locked()
            try:
                yield
            finally:
                self._render_locked()

    def _draw_locked(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_draw < 0.2:
            return
        self._clear_locked()
        self._render_locked()
        self._last_draw = now

    def _columns(self) -> int:
        """Usable width, re-read each draw so a window resize is picked up.

        One column is held back: filling the last one leaves the cursor past
        the right margin, and some terminals wrap there immediately, adding a
        phantom row the redraw would not account for.
        """
        try:
            columns = shutil.get_terminal_size().columns
        except OSError:
            columns = 80
        return max(20, columns - 1)

    def _clear_locked(self) -> None:
        # Rows, not lines: a line wider than the terminal wraps onto several
        # physical rows, and erasing one row per job would leave the overflow
        # on screen — the smear that long filenames used to produce.
        if not self._tty or not self._rendered_rows:
            self._rendered_lines = 0
            self._rendered_rows = 0
            return
        self._stream.write("\r")
        for row in range(self._rendered_rows):
            self._stream.write("\x1b[2K")
            if row < self._rendered_rows - 1:
                self._stream.write("\x1b[1A")
        self._stream.write("\r")
        self._stream.flush()
        self._rendered_lines = 0
        self._rendered_rows = 0

    def _render_locked(self) -> None:
        if not self._tty or not self._states:
            return
        columns = self._columns()
        lines = [
            format_progress(state, max_columns=columns)
            for state in self._states.values()
        ]
        self._stream.write("\n".join(lines))
        self._stream.flush()
        self._rendered_lines = len(lines)
        # Width-aware formatting should keep every line to one row; count
        # anyway so a mis-detected width degrades instead of corrupting.
        self._rendered_rows = sum(
            max(1, -(-len(line) // columns)) for line in lines
        )


LIVE_PROGRESS = ProgressDisplay()


class ConversionCancelled(RuntimeError):
    """Raised in workers after the user requests a cooperative stop."""


class CancellationController:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()

    def reset(self) -> None:
        self._event.clear()

    def requested(self) -> bool:
        return self._event.is_set()

    def register(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.add(process)
            cancelled = self._event.is_set()
        if cancelled:
            try:
                process.terminate()
            except OSError:
                pass

    def unregister(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(process)

    def request(self) -> None:
        self._event.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                    timer = threading.Timer(2.0, self._kill_if_alive, args=(process,))
                    timer.daemon = True
                    timer.start()
                except OSError:
                    pass

    @staticmethod
    def _kill_if_alive(process: subprocess.Popen) -> None:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


CANCELLATION = CancellationController()


def _parse_speed(value: str) -> Optional[float]:
    try:
        return float(value.rstrip("x"))
    except ValueError:
        return None


def run_ffmpeg_progress(
    cmd: List[str],
    src: Path,
    total: Optional[float],
    action: str,
) -> subprocess.CompletedProcess:
    """Run FFmpeg while forwarding machine progress to the shared display."""
    if CANCELLATION.requested():
        raise ConversionCancelled("conversion cancelled")
    progress_cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    key = f"{threading.get_ident()}:{src}:{action}"
    LIVE_PROGRESS.start(key, src.name, action, total)
    try:
        proc = subprocess.Popen(
            progress_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        LIVE_PROGRESS.finish(key)
        raise
    CANCELLATION.register(proc)
    stderr_chunks: List[str] = []
    drain = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()))
    drain.start()
    current = 0.0
    speed = None
    try:
        for line in proc.stdout:
            name, separator, value = line.strip().partition("=")
            if not separator:
                continue
            if name in ("out_time_us", "out_time_ms"):
                try:
                    current = int(value) / 1_000_000
                except ValueError:
                    pass
            elif name == "speed":
                speed = _parse_speed(value)
            elif name == "progress":
                LIVE_PROGRESS.update(key, current, speed)
        proc.wait()
        drain.join()
        proc.stdout.close()
        proc.stderr.close()
    finally:
        CANCELLATION.unregister(proc)
        if proc.poll() is None:
            proc.terminate()
        LIVE_PROGRESS.finish(key)
    if CANCELLATION.requested():
        raise ConversionCancelled("conversion cancelled")
    # The last out_time FFmpeg reported is the decoded timeline length. It is
    # echoed back on stdout (where it arrived) so callers that need a measured
    # duration — rather than a container's declared one — can read it without
    # decoding the file a second time.
    return subprocess.CompletedProcess(
        progress_cmd,
        proc.returncode,
        f"out_time_us={int(current * 1_000_000)}\n",
        stderr_chunks[0] if stderr_chunks else "",
    )
