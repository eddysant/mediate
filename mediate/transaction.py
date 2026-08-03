"""Crash-recoverable replacement of validated conversion outputs."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .disposal import DisposalPolicy
from .macmeta import set_birthtime
from .scanner import BUNDLE_EXTS

TRANSACTION_PREFIX = ".mediate-txn-"
MANIFEST_NAME = "transaction.json"


class TransactionError(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fingerprint(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        for offset in sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)}):
            handle.seek(offset)
            digest.update(offset.to_bytes(8, "little"))
            digest.update(handle.read(65536))
    return digest.hexdigest()


def _identity(path: Path, *, strong: bool = False) -> dict:
    info = path.stat()
    result = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
    if strong:
        result["fingerprint"] = _fingerprint(path)
    return result


def _matches(path: Path, expected: dict, *, output: bool = False) -> bool:
    try:
        current = _identity(path, strong=output)
    except OSError:
        return False
    fields = ("device", "inode", "size", "fingerprint") if output else (
        "device", "inode", "size", "mtime_ns"
    )
    return all(current.get(name) == expected.get(name) for name in fields)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@dataclass
class ReplacementTransaction:
    root: Path
    directory: Path
    manifest_path: Path
    data: dict
    disposer: Optional[Callable] = None

    @classmethod
    def prepare(
        cls,
        root: Path,
        source: Path,
        final: Path,
        temporary: Path,
        disposer: Optional[Callable],
        sidecars: Iterable[Path] = (),
        birthtime: Optional[float] = None,
        expected_source_identity: Optional[dict] = None,
    ) -> "ReplacementTransaction":
        root = root.resolve()
        source = source.resolve()
        final = final.resolve()
        temporary = temporary.resolve()
        if not _inside(source, root):
            raise TransactionError(f"source escapes transaction root: {source}")
        if source.parent != final.parent or final.parent != temporary.parent:
            raise TransactionError("source, temporary output, and final output must share a directory")
        directory = source.parent / f"{TRANSACTION_PREFIX}{uuid.uuid4().hex}"
        directory.mkdir(mode=0o700)
        try:
            backup = directory / source.name
            source_stat = source.stat()
            source_identity = _identity(source)
            if expected_source_identity is not None and any(
                source_identity.get(name) != expected_source_identity.get(name)
                for name in ("device", "inode", "size", "mtime_ns")
            ):
                raise TransactionError("source changed before transaction preparation")
            disposal = disposer.to_dict() if isinstance(disposer, DisposalPolicy) else None
            data = {
                "version": 1,
                "state": "prepared",
                "root": str(root),
                "source": str(source),
                "final": str(final),
                "temporary": str(temporary),
                "backup": str(backup),
                "source_identity": source_identity,
                "output_identity": _identity(temporary, strong=True),
                "timestamps": {
                    "atime_ns": source_stat.st_atime_ns,
                    "mtime_ns": source_stat.st_mtime_ns,
                    "birthtime": birthtime,
                },
                "sidecars": [str(path.resolve()) for path in sidecars],
                "disposed_sidecars": [],
                "disposal": disposal,
            }
            manifest = directory / MANIFEST_NAME
            _write_json(manifest, data)
            _fsync_directory(source.parent)
            return cls(root, directory, manifest, data, disposer)
        except Exception:
            for child in directory.iterdir():
                child.unlink(missing_ok=True)
            directory.rmdir()
            raise

    @classmethod
    def load(cls, manifest: Path) -> "ReplacementTransaction":
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise TransactionError(f"unsupported transaction version in {manifest}")
        root = Path(data["root"]).resolve()
        directory = manifest.parent.resolve()
        source = Path(data["source"]).resolve()
        backup = Path(data["backup"]).resolve()
        final = Path(data["final"]).resolve()
        temporary = Path(data["temporary"]).resolve()
        if not _inside(source, root):
            raise TransactionError("transaction source escapes its root")
        if (
            not directory.name.startswith(TRANSACTION_PREFIX)
            or directory.parent != source.parent
            or backup.parent != directory
            or backup.name != source.name
        ):
            raise TransactionError("transaction backup is not local to its source")
        if final.parent != source.parent or temporary.parent != source.parent:
            raise TransactionError("transaction output paths are not local to their source")
        for value in data.get("sidecars", []):
            sidecar = Path(value).resolve()
            if sidecar.parent != source.parent or sidecar.suffix.lower() not in {".aae", ".xmp"}:
                raise TransactionError("transaction contains an invalid sidecar path")
        disposal_data = data.get("disposal")
        disposer = DisposalPolicy.from_dict(disposal_data) if disposal_data else None
        if disposer is not None and disposer.root != root:
            raise TransactionError("transaction disposal root does not match its source root")
        return cls(root, directory, manifest.resolve(), data, disposer)

    @property
    def source(self) -> Path:
        return Path(self.data["source"])

    @property
    def final(self) -> Path:
        return Path(self.data["final"])

    @property
    def temporary(self) -> Path:
        return Path(self.data["temporary"])

    @property
    def backup(self) -> Path:
        return Path(self.data["backup"])

    def _save(self, state: str) -> None:
        self.data["state"] = state
        _write_json(self.manifest_path, self.data)

    def _apply_timestamps(self) -> None:
        values = self.data["timestamps"]
        os.utime(
            self.final,
            ns=(int(values["atime_ns"]), int(values["mtime_ns"])),
        )
        if values.get("birthtime") is not None:
            set_birthtime(self.final, float(values["birthtime"]))

    def _dispose_sidecars(self) -> str:
        if self.disposer is None:
            return ""
        disposed = set(self.data.get("disposed_sidecars", []))
        failed = []
        for value in self.data.get("sidecars", []):
            sidecar = Path(value)
            if value in disposed or not sidecar.exists():
                continue
            try:
                self.disposer(sidecar, sidecar)
            except OSError:
                failed.append(sidecar.name)
                continue
            disposed.add(value)
            self.data["disposed_sidecars"] = sorted(disposed)
            self._save("original_disposed")
        result = " with " + ", ".join(Path(value).name for value in disposed) if disposed else ""
        if failed:
            result += "; sidecar(s) left in place: " + ", ".join(failed)
        return result

    def _cleanup(self) -> None:
        self.manifest_path.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass
        _fsync_directory(self.source.parent)

    def _rollback(self) -> None:
        if _matches(self.final, self.data["output_identity"], output=True):
            os.replace(self.final, self.temporary)
        if self.backup.exists() and not self.source.exists():
            os.replace(self.backup, self.source)
        if _matches(self.temporary, self.data["output_identity"], output=True):
            self.temporary.unlink(missing_ok=True)
        self._cleanup()

    def commit(self, checkpoint: Optional[Callable[[str], None]] = None) -> str:
        checkpoint = checkpoint or (lambda _state: None)
        if not _matches(self.source, self.data["source_identity"]):
            self._cleanup()
            raise TransactionError("source changed before transaction commit")
        if not _matches(self.temporary, self.data["output_identity"], output=True):
            self._cleanup()
            raise TransactionError("validated output changed before transaction commit")
        if self.final != self.source and self.final.exists():
            self._cleanup()
            raise TransactionError(f"output appeared during conversion: {self.final.name}")
        os.replace(self.source, self.backup)
        _fsync_directory(self.source.parent)
        if not _matches(self.backup, self.data["source_identity"]):
            self._rollback()
            raise TransactionError("staged source identity did not match the validated input")
        self._save("source_staged")
        checkpoint("source_staged")
        os.replace(self.temporary, self.final)
        _fsync_directory(self.final.parent)
        self._apply_timestamps()
        self._save("output_installed")
        checkpoint("output_installed")
        description = ""
        if self.disposer is not None:
            try:
                description = self.disposer(self.backup, self.source)
            except OSError as exc:
                self._rollback()
                raise TransactionError(f"could not dispose of original: {exc}") from exc
        else:
            self.backup.unlink()
            description = "original deleted"
        self._save("original_disposed")
        sidecars = self._dispose_sidecars()
        self._cleanup()
        return description + sidecars


@dataclass
class RecoveryReport:
    completed: int = 0
    rolled_back: int = 0
    unresolved: int = 0
    messages: list[str] = field(default_factory=list)


def _discover_manifests(root: Path) -> list[Path]:
    manifests = []
    for directory, dirnames, _filenames in os.walk(root):
        parent = Path(directory)
        kept = []
        for name in dirnames:
            candidate = parent / name
            if name.startswith(TRANSACTION_PREFIX):
                manifest = candidate / MANIFEST_NAME
                if manifest.is_file():
                    manifests.append(manifest)
                continue
            if name.startswith(".") or candidate.suffix.lower() in BUNDLE_EXTS:
                continue
            kept.append(name)
        dirnames[:] = kept
    return sorted(manifests)


def _preserve_conflict(transaction: ReplacementTransaction, path: Path) -> Path:
    candidate = path.parent / f".{path.name}.mediate-recovery-conflict-{uuid.uuid4().hex[:8]}"
    os.replace(path, candidate)
    return candidate


def recover_transactions(root: Path, dry_run: bool = False) -> RecoveryReport:
    """Roll back interrupted installs or finish disposal after a safe install."""
    report = RecoveryReport()
    requested_root = root.resolve()
    for manifest in _discover_manifests(requested_root):
        try:
            transaction = ReplacementTransaction.load(manifest)
            if transaction.root != requested_root:
                raise TransactionError("recorded transaction root does not match this run")
            source = transaction.source
            final = transaction.final
            backup = transaction.backup
            temporary = transaction.temporary
            original = transaction.data["source_identity"]
            output = transaction.data["output_identity"]
            final_is_output = _matches(final, output, output=True)
            source_is_original = _matches(source, original)
            backup_is_original = _matches(backup, original)

            if final_is_output and (backup_is_original or not backup.exists()):
                message = f"finish installed output {final.name}"
                if dry_run:
                    report.messages.append(message)
                    report.completed += 1
                    continue
                transaction._apply_timestamps()
                if backup_is_original:
                    if transaction.disposer is None:
                        raise TransactionError("recorded transaction has no recoverable disposal policy")
                    transaction.disposer(backup, source)
                transaction._save("original_disposed")
                transaction._dispose_sidecars()
                transaction._cleanup()
                report.completed += 1
                report.messages.append(message)
                continue

            if source_is_original and not backup.exists():
                message = f"discard uncommitted output for {source.name}"
                if not dry_run:
                    if _matches(temporary, output, output=True):
                        temporary.unlink(missing_ok=True)
                    transaction._cleanup()
                report.rolled_back += 1
                report.messages.append(message)
                continue

            if backup_is_original:
                message = f"restore original {source.name}"
                if not dry_run:
                    if source.exists() and not source_is_original:
                        conflict = _preserve_conflict(transaction, source)
                        report.messages.append(f"preserved conflicting file as {conflict.name}")
                    elif final != source and final.exists() and not final_is_output:
                        report.messages.append(f"left unrelated output untouched: {final.name}")
                    if not source.exists():
                        os.replace(backup, source)
                    if _matches(temporary, output, output=True):
                        temporary.unlink(missing_ok=True)
                    transaction._cleanup()
                report.rolled_back += 1
                report.messages.append(message)
                continue

            raise TransactionError("cannot identify a trustworthy original or validated output")
        except (OSError, ValueError, KeyError, json.JSONDecodeError, TransactionError) as exc:
            report.unresolved += 1
            report.messages.append(f"unresolved transaction {manifest.parent}: {exc}")
    return report
