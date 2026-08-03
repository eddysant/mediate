"""Thread-safe live progress display for concurrent FFmpeg jobs."""

from __future__ import annotations

import contextlib
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


def format_progress(state: ProgressState, width: int = 22) -> str:
    percent = state.percent
    if percent is None:
        bar = "?" * width
        percent_text = "  --%"
    else:
        filled = min(width, int(width * percent / 100))
        bar = "=" * filled + (">" if filled < width else "")
        bar = bar.ljust(width, "-")
        percent_text = f"{percent:4d}%"
    timing = f"{_clock(state.current)}/{_clock(state.total)}"
    speed = f" {state.speed:.2f}x" if state.speed else ""
    eta = ""
    if state.total and state.speed and state.speed > 0:
        eta = f" ETA {_clock(max(0.0, state.total - state.current) / state.speed)}"
    return f"{state.action:<10} {state.label} [{bar}] {percent_text} {timing}{speed}{eta}"


class ProgressDisplay:
    """Render all active jobs as stable terminal lines, or periodic plain logs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: "OrderedDict[str, ProgressState]" = OrderedDict()
        self._stream: TextIO = sys.stdout
        self._enabled = False
        self._tty = False
        self._rendered_lines = 0
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

    def _clear_locked(self) -> None:
        if not self._tty or not self._rendered_lines:
            self._rendered_lines = 0
            return
        self._stream.write("\r")
        for line in range(self._rendered_lines):
            self._stream.write("\x1b[2K")
            if line < self._rendered_lines - 1:
                self._stream.write("\x1b[1A")
        self._stream.write("\r")
        self._stream.flush()
        self._rendered_lines = 0

    def _render_locked(self) -> None:
        if not self._tty or not self._states:
            return
        lines = [format_progress(state) for state in self._states.values()]
        self._stream.write("\n".join(lines))
        self._stream.flush()
        self._rendered_lines = len(lines)


LIVE_PROGRESS = ProgressDisplay()


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
        LIVE_PROGRESS.finish(key)
    return subprocess.CompletedProcess(
        progress_cmd,
        proc.returncode,
        "",
        stderr_chunks[0] if stderr_chunks else "",
    )
