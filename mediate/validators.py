"""Post-conversion validation protocol. All checks must pass before the
original file may be deleted."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Tuple


def _tail(stderr: str, lines: int = 3) -> str:
    kept = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    return " | ".join(kept[-lines:]) if kept else "(no stderr)"


def validate_output(
    returncode: int,
    stderr: str,
    output_path: Path,
    is_video: bool,
) -> Tuple[bool, str]:
    """Run the validation checklist. Returns (ok, reason)."""
    # 1. Exit code check
    if returncode != 0:
        return False, f"converter exited with code {returncode}: {_tail(stderr)}"

    # 2. Output existence
    if not output_path.exists():
        return False, "output file was not created"

    # 3. Size check
    if output_path.stat().st_size <= 0:
        return False, "output file is 0 bytes"

    # 4. Integrity check (videos only): decode the whole file and require a
    # clean exit AND empty stderr.
    if is_video:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(output_path), "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or proc.stderr.strip():
            return False, f"integrity check failed: {_tail(proc.stderr)}"

    return True, "ok"
