"""Filesystem invariants for long-running, destructive media operations."""

from __future__ import annotations

import os
import shutil
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


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


class DiskSpaceReservation:
    def __init__(self, manager: "DiskSpaceReservations", device: int, amount: int) -> None:
        self.manager = manager
        self.device = device
        self.amount = amount
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.manager._release(self.device, self.amount)

    def __enter__(self) -> "DiskSpaceReservation":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


class DiskSpaceReservations:
    """Reserve output budgets per filesystem across concurrent workers."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._reserved: dict[int, int] = {}

    def acquire(
        self,
        directory: Path,
        source_size: int,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> DiskSpaceReservation:
        if not os.access(directory, os.W_OK):
            raise SafetyError(f"output directory is not writable: {directory}")
        try:
            device = directory.stat().st_dev
        except OSError as exc:
            raise SafetyError(f"cannot identify output filesystem: {exc}") from exc
        required = required_temporary_space(source_size)
        with self._condition:
            while True:
                try:
                    free = shutil.disk_usage(directory).free
                except OSError as exc:
                    raise SafetyError(f"cannot determine output free space: {exc}") from exc
                already_reserved = self._reserved.get(device, 0)
                if free - already_reserved >= required:
                    self._reserved[device] = already_reserved + required
                    return DiskSpaceReservation(self, device, required)
                if already_reserved == 0:
                    raise SafetyError(
                        f"insufficient aggregate free space: need at least "
                        f"{required / (1024 * 1024):.0f} MB, "
                        f"have {free / (1024 * 1024):.0f} MB"
                    )
                if cancelled is not None and cancelled():
                    raise SafetyError("cancelled while waiting for temporary disk space")
                self._condition.wait(timeout=0.25)

    def _release(self, device: int, amount: int) -> None:
        with self._condition:
            remaining = self._reserved.get(device, 0) - amount
            if remaining > 0:
                self._reserved[device] = remaining
            else:
                self._reserved.pop(device, None)
            self._condition.notify_all()

    def reserved_bytes(self, device: int) -> int:
        with self._condition:
            return self._reserved.get(device, 0)
