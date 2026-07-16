"""macOS file-metadata helpers (no-ops elsewhere).

Finder's "date created" is the file's birthtime, which a fresh conversion
resets to "now". EXIF dates survive inside the converted files, but Finder
sorting would change — so the original's birthtime is copied onto the output
via setattrlist(2), the only stable API that can set ATTR_CMN_CRTIME.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("mediate")

ATTR_BIT_MAP_COUNT = 5
ATTR_CMN_CRTIME = 0x00000200


class _timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _attrlist(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


_libc = None
if sys.platform == "darwin":
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:  # pragma: no cover
        _libc = None


def get_birthtime(path: Path) -> float | None:
    try:
        return path.stat().st_birthtime  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return None


def set_birthtime(path: Path, timestamp: float) -> bool:
    """Set the file's creation date on macOS. Returns False (harmlessly)
    anywhere it can't be done."""
    if _libc is None:
        return False
    attrs = _attrlist(bitmapcount=ATTR_BIT_MAP_COUNT, commonattr=ATTR_CMN_CRTIME)
    ts = _timespec(int(timestamp), int((timestamp % 1) * 1_000_000_000))
    result = _libc.setattrlist(
        os.fsencode(str(path)),
        ctypes.byref(attrs),
        ctypes.byref(ts),
        ctypes.sizeof(ts),
        0,
    )
    if result != 0:
        log.debug("setattrlist failed for %s (errno %d)", path, ctypes.get_errno())
        return False
    return True
