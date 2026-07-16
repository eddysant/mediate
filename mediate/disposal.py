"""Disposal of original files after successful conversion.

Default is the system Trash, so a regretted conversion stays recoverable
until the Trash is emptied. Video re-encoding is lossy: once an original is
hard-deleted, that quality is gone forever.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Tuple

TRASH = "trash"
GRAVEYARD = "graveyard"
HARD = "hard-delete"

# Disposer(path) -> short description of what happened, for the log line.
Disposer = Callable[[Path], str]


def _unique_dest(directory: Path, name: str) -> Path:
    dest = directory / name
    stem, suffix = dest.stem, dest.suffix
    counter = 2
    while dest.exists():
        dest = directory / f"{stem} {counter}{suffix}"
        counter += 1
    return dest


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError:
        # Cross-volume: copy + unlink.
        shutil.move(str(src), str(dest))


def _trash_dir_for(path: Path) -> Path:
    """The Trash directory to use for a file, per platform convention."""
    if sys.platform == "darwin":
        home_trash = Path.home() / ".Trash"
        try:
            # A file on another volume gets that volume's .Trashes/<uid>,
            # avoiding a full copy of (potentially huge) video files.
            if path.stat().st_dev != home_trash.stat().st_dev:
                for parent in path.resolve().parents:
                    candidate = parent / ".Trashes" / str(os.getuid())
                    if (parent / ".Trashes").is_dir():
                        candidate.mkdir(parents=True, exist_ok=True)
                        return candidate
        except OSError:
            pass
        return home_trash
    # freedesktop.org convention (Linux/BSD).
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return xdg_data / "Trash" / "files"


def _send_to_trash(path: Path) -> str:
    trash_dir = _trash_dir_for(path)
    dest = _unique_dest(trash_dir, path.name)
    _move(path, dest)
    if sys.platform != "darwin":
        # Minimal freedesktop trashinfo so desktop Trash UIs can restore it.
        info_dir = trash_dir.parent / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        (info_dir / f"{dest.name}.trashinfo").write_text(
            "[Trash Info]\n"
            f"Path={path}\n"
            f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n",
            encoding="utf-8",
        )
    return "original moved to Trash"


def make_disposer(mode: str, root: Path, graveyard: Path | None) -> Tuple[Disposer, str]:
    """Return (disposer, human label of what will happen to originals)."""
    if mode == HARD:
        def dispose(path: Path) -> str:
            path.unlink()
            return "original deleted"
        return dispose, "delete original"

    if mode == GRAVEYARD:
        assert graveyard is not None
        base = graveyard.expanduser().resolve()

        def dispose(path: Path) -> str:
            try:
                rel = path.resolve().relative_to(root)
            except ValueError:
                rel = Path(path.name)
            dest = base / rel
            dest = _unique_dest(dest.parent, dest.name)
            _move(path, dest)
            return f"original moved to {base.name}/{rel.parent}" if str(rel.parent) != "." else f"original moved to {base.name}/"
        return dispose, f"move original to graveyard {base}"

    return _send_to_trash, "move original to Trash"
