"""Persistent exiftool daemon.

Metadata validation, --date-prefix, and Live Photo verification each need an
exiftool query per file; spawning a fresh exiftool (~150ms of Perl startup)
per query makes large libraries crawl. `-stay_open` keeps one exiftool alive
and feeds it commands over stdin, cutting a query to ~1ms.

One daemon serializes every query in the process, which caps what the worker
pool can do on metadata-heavy runs, so a small pool of daemons is kept and
handed out per calling thread.

Callers use run_exiftool(args) exactly as if the args went to a one-shot
`exiftool` invocation; it returns stdout, or None when exiftool isn't
installed. Falls back to a one-shot subprocess if the daemon dies.
"""

from __future__ import annotations

import atexit
import logging
import shutil
import subprocess
import threading
from functools import lru_cache
from typing import List, Optional

log = logging.getLogger("mediate")

# `-@ -` is strictly one argument per line, so an argument containing a
# newline would be split into two exiftool arguments. Media paths are
# attacker-influenced input (a filename may legally contain a newline on
# APFS) and some call sites pass -overwrite_original, so such arguments never
# go to the daemon; they take the one-shot argv path instead, where the
# operating system passes each argument intact.
_UNSAFE_FOR_ARGFILE = ("\n", "\r")

# Enough to keep several conversion workers from queueing behind one Perl
# process, without paying for an interpreter per thread on a large pool.
MAX_DAEMONS = 4


@lru_cache(maxsize=1)
def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def _argfile_safe(args: List[str]) -> bool:
    return not any(
        marker in arg for arg in args for marker in _UNSAFE_FOR_ARGFILE
    )


def _one_shot(args: List[str]) -> str:
    proc = subprocess.run(["exiftool", *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


class _Daemon:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _ensure(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                ["exiftool", "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        return self._proc

    def execute(self, args: List[str]) -> str:
        with self._lock:
            proc = self._ensure()
            # Checked rather than asserted: `python -O` strips assert, and the
            # AttributeError that followed is not what callers catch.
            if proc.stdin is None or proc.stdout is None:
                raise BrokenPipeError("exiftool daemon has no usable pipes")
            for arg in args:
                proc.stdin.write(arg + "\n")
            proc.stdin.write("-execute\n")
            proc.stdin.flush()
            lines: List[str] = []
            while True:
                line = proc.stdout.readline()
                if not line:  # daemon died mid-answer
                    raise BrokenPipeError("exiftool daemon exited")
                if line.strip() == "{ready}":
                    break
                lines.append(line)
            return "".join(lines)

    def stop(self) -> None:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return
            try:
                if self._proc.stdin is None:
                    raise BrokenPipeError("exiftool daemon has no stdin")
                self._proc.stdin.write("-stay_open\nFalse\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()


class _DaemonPool:
    """Hand each calling thread a daemon, reusing at most MAX_DAEMONS."""

    def __init__(self, size: int = MAX_DAEMONS) -> None:
        self._size = max(1, size)
        self._daemons: List[_Daemon] = []
        self._by_thread: dict = {}
        self._lock = threading.Lock()
        self._next = 0

    def acquire(self) -> _Daemon:
        key = threading.get_ident()
        with self._lock:
            daemon = self._by_thread.get(key)
            if daemon is not None:
                return daemon
            if len(self._daemons) < self._size:
                daemon = _Daemon()
                self._daemons.append(daemon)
            else:
                # Threads beyond the pool size share round-robin; each daemon
                # keeps its own lock, so sharing is correct, just serialized.
                daemon = self._daemons[self._next % len(self._daemons)]
                self._next += 1
            self._by_thread[key] = daemon
            return daemon

    def stop(self) -> None:
        with self._lock:
            daemons = list(self._daemons)
            self._daemons.clear()
            self._by_thread.clear()
        for daemon in daemons:
            daemon.stop()


_pool: Optional[_DaemonPool] = None
_pool_lock = threading.Lock()


def _get_pool() -> _DaemonPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _DaemonPool()
            atexit.register(_pool.stop)
    return _pool


def run_exiftool(args: List[str]) -> Optional[str]:
    """Run an exiftool query through the shared daemon pool. Returns stdout,
    or None when exiftool isn't installed."""
    if not exiftool_available():
        return None
    if not _argfile_safe(args):
        log.debug("exiftool argument contains a newline; using one-shot invocation")
        return _one_shot(args)
    try:
        return _get_pool().acquire().execute(args)
    except (OSError, BrokenPipeError) as exc:
        log.debug("exiftool daemon failed (%s); one-shot fallback", exc)
        return _one_shot(args)
