"""Filesystem invariants for long-running, destructive media operations."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


class SafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def capture(cls, path: Path) -> "SourceSnapshot":
        try:
            info = path.lstat()
        except OSError as exc:
            raise SafetyError(f"cannot stat source: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise SafetyError("symbolic-link media is not processed")
        if not stat.S_ISREG(info.st_mode):
            raise SafetyError("source is not a regular file")
        if info.st_nlink > 1:
            raise SafetyError(
                f"hard-linked media has {info.st_nlink} directory entries; "
                "separate it before conversion"
            )
        try:
            with path.open("rb") as handle:
                handle.read(1)
                if info.st_size > 1:
                    handle.seek(max(0, info.st_size - 1))
                    handle.read(1)
        except OSError as exc:
            raise SafetyError(f"source is not fully readable: {exc}") from exc
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def unchanged(self, path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        return (
            not stat.S_ISLNK(info.st_mode)
            and info.st_dev == self.device
            and info.st_ino == self.inode
            and info.st_size == self.size
            and info.st_mtime_ns == self.mtime_ns
        )


def required_temporary_space(source_size: int) -> int:
    """Conservative floor for one output plus muxer/metadata overhead."""
    return max(64 * 1024 * 1024, int(source_size * 1.25))


def ensure_output_capacity(directory: Path, source_size: int) -> None:
    if not os.access(directory, os.W_OK):
        raise SafetyError(f"output directory is not writable: {directory}")
    try:
        free = shutil.disk_usage(directory).free
    except OSError as exc:
        raise SafetyError(f"cannot determine output free space: {exc}") from exc
    required = required_temporary_space(source_size)
    if free < required:
        raise SafetyError(
            f"insufficient free space: need at least {required / (1024 * 1024):.0f} MB, "
            f"have {free / (1024 * 1024):.0f} MB"
        )
