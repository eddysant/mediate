"""Disposal of original files after successful conversion.

Default is the system Trash, so a regretted conversion stays recoverable
until the Trash is emptied. Video re-encoding is lossy: once an original is
hard-deleted, that quality is gone forever.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

TRASH = "trash"
GRAVEYARD = "graveyard"
HARD = "hard-delete"


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


def _send_to_trash(path: Path, original_path: Optional[Path] = None) -> str:
    logical_path = original_path or path
    trash_dir = _trash_dir_for(path)
    dest = _unique_dest(trash_dir, logical_path.name)
    _move(path, dest)
    if sys.platform != "darwin":
        # Minimal freedesktop trashinfo so desktop Trash UIs can restore it.
        info_dir = trash_dir.parent / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        try:
            (info_dir / f"{dest.name}.trashinfo").write_text(
                "[Trash Info]\n"
                f"Path={logical_path}\n"
                f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n",
                encoding="utf-8",
            )
        except OSError:
            # The file is already safely in Trash. Missing UI metadata must
            # not make the replacement transaction attempt an impossible
            # rollback from a source path that no longer exists.
            pass
    return "original moved to Trash"


@dataclass(frozen=True)
class DisposalPolicy:
    """Serializable disposal behavior used by crash-recovery transactions."""

    mode: str
    root: Path
    graveyard: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.mode not in {TRASH, GRAVEYARD, HARD}:
            raise ValueError(f"unknown disposal mode: {self.mode}")
        if self.mode == GRAVEYARD and self.graveyard is None:
            raise ValueError("graveyard disposal requires a destination")

    def __call__(self, path: Path, original_path: Optional[Path] = None) -> str:
        logical_path = original_path or path
        if self.mode == HARD:
            path.unlink()
            return "original deleted"
        if self.mode == GRAVEYARD:
            assert self.graveyard is not None
            try:
                rel = logical_path.resolve().relative_to(self.root)
            except ValueError:
                rel = Path(logical_path.name)
            dest = self.graveyard / rel
            dest = _unique_dest(dest.parent, dest.name)
            _move(path, dest)
            return (
                f"original moved to {self.graveyard.name}/{rel.parent}"
                if str(rel.parent) != "."
                else f"original moved to {self.graveyard.name}/"
            )
        return _send_to_trash(path, logical_path)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "root": str(self.root),
            "graveyard": str(self.graveyard) if self.graveyard is not None else None,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "DisposalPolicy":
        mode = value.get("mode")
        if mode not in {TRASH, GRAVEYARD, HARD}:
            raise ValueError(f"unknown disposal mode: {mode}")
        graveyard = value.get("graveyard")
        if mode == GRAVEYARD and not graveyard:
            raise ValueError("recorded graveyard transaction has no destination")
        return cls(
            mode,
            Path(value["root"]).resolve(),
            Path(graveyard).resolve() if graveyard else None,
        )


# DisposalPolicy(path, original_path=None) -> short description for the log.
Disposer = DisposalPolicy


def make_disposer(mode: str, root: Path, graveyard: Path | None) -> Tuple[DisposalPolicy, str]:
    """Return (serializable policy, human label for original disposal)."""
    base = graveyard.expanduser().resolve() if graveyard is not None else None
    policy = DisposalPolicy(mode, root.resolve(), base)
    if mode == HARD:
        return policy, "delete original"
    if mode == GRAVEYARD:
        assert base is not None
        return policy, f"move original to graveyard {base}"

    return policy, "move original to Trash"
