"""On-disk memo of probe results, keyed by filesystem identity.

Probing is the expensive part of a re-run: a 50k-file library would otherwise
spawn an ffprobe per MP4/GIF/WebP on every pass, and `--validate-existing`
would re-decode every file it had already proved healthy. Results are
therefore cached under the user cache directory and invalidated by the
file's own identity (mtime, ctime, size, inode, device) rather than by age.
Entries whose cost is a full decode additionally carry a sampled content
fingerprint, so a file rewritten in place with identical stat fields is not
trusted.

This module knows nothing about media: it stores whatever a caller computes
and runs ffprobe on request. The classification built on top lives in
`probe.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("mediate")

# Unbounded growth guard: a fresh start is cheaper than maintaining an LRU.
MAX_ENTRIES = 200_000

_cache: dict = {}
_cache_lock = threading.Lock()
_cache_dirty = False


def _cache_file() -> Path:
    return Path.home() / "Library" / "Caches" / "mediate" / "probe-cache.json"


def load_probe_cache() -> None:
    """Read the cache into memory. Must run before the worker pool starts."""
    global _cache
    try:
        _cache = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _cache = {}


def save_probe_cache() -> None:
    if not _cache_dirty:
        return
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(_cache) > MAX_ENTRIES:
            _cache.clear()
        # Written atomically like the journal and transaction manifests: a
        # crash or a second concurrent run must not leave a truncated cache
        # that silently discards expensive --validate-existing decode results.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(_cache), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.debug("could not write probe cache: %s", exc)


def _sample_fingerprint(path: Path) -> str | None:
    """Hash small samples from across a file without rereading large media."""
    try:
        size = path.stat().st_size
        digest = hashlib.blake2b(digest_size=16)
        with path.open("rb") as handle:
            for offset in sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)}):
                handle.seek(offset)
                digest.update(offset.to_bytes(8, "little"))
                digest.update(handle.read(65536))
        return digest.hexdigest()
    except OSError:
        return None


def file_identity(path: Path, strong: bool = False) -> dict | None:
    """What makes a cached answer still true for this file."""
    try:
        st = path.stat()
    except OSError:
        return None
    identity = {
        "m": st.st_mtime_ns,
        "c": st.st_ctime_ns,
        "s": st.st_size,
        "i": st.st_ino,
        "d": st.st_dev,
    }
    if strong:
        identity["f"] = _sample_fingerprint(path)
    return identity


def recall(key: str) -> dict | None:
    """The raw stored entry for a key, identity fields included."""
    with _cache_lock:
        return _cache.get(key)


def remember(key: str, entry: dict) -> None:
    """Store a raw entry, marking the cache for saving."""
    global _cache_dirty
    with _cache_lock:
        _cache[key] = entry
        _cache_dirty = True


def cached(kind: str, path: Path, compute, strong: bool = False):
    """Return a memoized `compute()` for a file, recomputing if it changed.

    A file whose identity cannot be read (deleted mid-run, unreadable) is
    computed without being cached rather than treated as an error.
    """
    identity = file_identity(path, strong=strong)
    if identity is None:
        return compute()
    key = f"{kind}:{path}"
    entry = recall(key)
    if entry and all(entry.get(name) == value for name, value in identity.items()):
        return entry["v"]
    value = compute()
    remember(key, {**identity, "v": value})
    return value


def ffprobe_json(args: list) -> dict | None:
    """Run ffprobe and parse its JSON, or None if anything about that fails."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-of", "json", *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
