"""Persistent exiftool daemon.

Metadata validation, --date-prefix, and Live Photo verification each need an
exiftool query per file; spawning a fresh exiftool (~150ms of Perl startup)
per query makes large libraries crawl. `-stay_open` keeps one exiftool alive
and feeds it commands over stdin, cutting a query to ~1ms.

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


@lru_cache(maxsize=1)
def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


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
            assert proc.stdin and proc.stdout
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
                assert self._proc.stdin
                self._proc.stdin.write("-stay_open\nFalse\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()


_daemon: Optional[_Daemon] = None
_daemon_lock = threading.Lock()


def run_exiftool(args: List[str]) -> Optional[str]:
    """Run an exiftool query through the shared daemon. Returns stdout, or
    None when exiftool isn't installed."""
    if not exiftool_available():
        return None
    global _daemon
    with _daemon_lock:
        if _daemon is None:
            _daemon = _Daemon()
            atexit.register(_daemon.stop)
    try:
        return _daemon.execute(args)
    except (OSError, BrokenPipeError, AssertionError) as exc:
        log.debug("exiftool daemon failed (%s); one-shot fallback", exc)
        proc = subprocess.run(["exiftool", *args], capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else ""
