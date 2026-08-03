"""Persistent run state for explaining and resuming interrupted libraries."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .scanner import MediaJob


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunJournal:
    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = root.resolve()
        self.path = self.root / ".mediate-run.json"
        self.enabled = enabled
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._data = self._load()

    def _load(self) -> dict:
        if not self.enabled:
            return {"version": 1, "files": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if data.get("version") == 1 else {"version": 1, "files": {}}
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "files": {}}

    def _key(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root))

    @staticmethod
    def _identity(path: Path) -> dict:
        stat = path.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def _write_locked(self, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last_write < 1.0:
            return
        self._data["updated_at"] = _now()
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._last_write = now

    def prepare(self, jobs: Iterable[MediaJob]) -> tuple[list[MediaJob], int]:
        jobs = list(jobs)
        unfinished = []
        old_files = self._data.get("files", {})
        for job in jobs:
            try:
                key = self._key(job.path)
                old = old_files.get(key, {})
                if old.get("status") in {"queued", "running", "interrupted"} and all(
                    old.get("identity", {}).get(name) == value
                    for name, value in self._identity(job.path).items()
                ):
                    unfinished.append(job.path)
            except (OSError, ValueError):
                continue
        unfinished_set = set(unfinished)
        ordered = [job for job in jobs if job.path in unfinished_set]
        ordered.extend(job for job in jobs if job.path not in unfinished_set)
        with self._lock:
            self._data = {
                "version": 1,
                "started_at": _now(),
                "interrupted": False,
                "files": {},
            }
            for job in ordered:
                try:
                    self._data["files"][self._key(job.path)] = {
                        "kind": job.kind,
                        "identity": self._identity(job.path),
                        "status": "queued",
                    }
                except (OSError, ValueError):
                    pass
            self._write_locked(force=True)
        return ordered, len(unfinished)

    def mark(self, job: MediaJob, status: str, detail: str = "") -> None:
        if not self.enabled:
            return
        try:
            key = self._key(job.path)
        except ValueError:
            return
        with self._lock:
            entry = self._data.setdefault("files", {}).setdefault(key, {"kind": job.kind})
            entry["status"] = status
            entry["detail"] = detail
            entry["updated_at"] = _now()
            self._write_locked(force=status in {"failed", "interrupted"})

    def finish(self, interrupted: bool) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._data["interrupted"] = interrupted
            self._data["finished_at"] = _now()
            if interrupted:
                for entry in self._data.get("files", {}).values():
                    if entry.get("status") in {"queued", "running"}:
                        entry["status"] = "interrupted"
            self._write_locked(force=True)
