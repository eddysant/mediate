"""Preflight checks for the external media toolchain mediate depends on."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MINIMUM_FFMPEG = (6, 0)


@dataclass
class CapabilityReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _first_line(proc: subprocess.CompletedProcess) -> str:
    text = proc.stdout.strip() or proc.stderr.strip()
    return text.splitlines()[0] if text else "unknown version"


def _version_tuple(text: str) -> tuple[int, int] | None:
    match = re.search(r"\bversion\s+(\d+)\.(\d+)", text, re.IGNORECASE)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _install_hint() -> str:
    return "install/upgrade with `brew install ffmpeg webp` (or your system package manager)"


def _check_binary(name: str, report: CapabilityReport) -> bool:
    if shutil.which(name) is not None:
        return True
    report.errors.append(f"{name} is not on PATH; {_install_hint()}")
    return False


def _check_cwebp(report: CapabilityReport) -> None:
    if not _check_binary("cwebp", report):
        return
    try:
        proc = _run(["cwebp", "-version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.errors.append(f"cwebp could not start: {exc}; {_install_hint()}")
        return
    if proc.returncode != 0:
        report.errors.append(f"cwebp self-check failed; {_install_hint()}")
        return
    report.versions["cwebp"] = _first_line(proc)


def _check_ffmpeg(report: CapabilityReport) -> None:
    have_ffmpeg = _check_binary("ffmpeg", report)
    have_ffprobe = _check_binary("ffprobe", report)
    if not (have_ffmpeg and have_ffprobe):
        return
    try:
        ffmpeg_version = _run(["ffmpeg", "-version"])
        ffprobe_version = _run(["ffprobe", "-version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.errors.append(f"FFmpeg toolchain could not start: {exc}; {_install_hint()}")
        return
    if ffmpeg_version.returncode != 0 or ffprobe_version.returncode != 0:
        report.errors.append(f"FFmpeg/ffprobe version check failed; {_install_hint()}")
        return
    ffmpeg_line = _first_line(ffmpeg_version)
    report.versions["ffmpeg"] = ffmpeg_line
    report.versions["ffprobe"] = _first_line(ffprobe_version)
    parsed = _version_tuple(ffmpeg_line)
    if parsed is None:
        report.warnings.append("could not parse FFmpeg version; capability smoke test will decide")
    elif parsed < MINIMUM_FFMPEG:
        report.warnings.append(
            f"FFmpeg {parsed[0]}.{parsed[1]} is older than the tested minimum "
            f"{MINIMUM_FFMPEG[0]}.{MINIMUM_FFMPEG[1]}; {_install_hint()}"
        )

    try:
        encoders = _run(["ffmpeg", "-hide_banner", "-encoders"])
        help_output = _run(["ffmpeg", "-hide_banner", "-h", "full"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.errors.append(f"FFmpeg capability listing failed: {exc}")
        return
    available = encoders.stdout + encoders.stderr
    for encoder in ("libx264", "aac"):
        if not re.search(rf"\b{re.escape(encoder)}\b", available):
            report.errors.append(
                f"FFmpeg is missing the {encoder} encoder; install a full FFmpeg build "
                "with GPL/libx264 support"
            )
    full_help = help_output.stdout + help_output.stderr
    if "-progress" not in full_help:
        report.errors.append("FFmpeg lacks -progress support required for conversion feedback")
    if "-display_rotation" not in full_help:
        report.warnings.append(
            "FFmpeg lacks -display_rotation; rotated re-encodes will fail safely. "
            "Upgrade FFmpeg to preserve rotation metadata"
        )
    if report.errors:
        return

    try:
        with tempfile.TemporaryDirectory(prefix="mediate-preflight-") as temp:
            output = Path(temp) / "smoke.mp4"
            encode = _run([
                "ffmpeg", "-nostdin", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=size=16x16:rate=10:duration=0.2",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", "0.2", "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-movflags", "faststart", str(output),
            ])
            if encode.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                detail = (encode.stderr.strip().splitlines() or ["no diagnostic"])[-1]
                report.errors.append(
                    f"FFmpeg h264/AAC/MP4 smoke encode failed: {detail}; {_install_hint()}"
                )
                return
            probe = _run([
                "ffprobe", "-v", "error", "-of", "json", "-show_streams", str(output)
            ])
            try:
                streams = json.loads(probe.stdout).get("streams", [])
            except json.JSONDecodeError:
                streams = []
            codecs = {stream.get("codec_name") for stream in streams}
            if probe.returncode != 0 or not {"h264", "aac"}.issubset(codecs):
                report.errors.append(
                    "ffprobe JSON stream inventory failed on a generated MP4; "
                    "install matching ffmpeg and ffprobe builds"
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.errors.append(f"FFmpeg smoke test could not complete: {exc}; {_install_hint()}")


def check_media_capabilities(
    *, require_video: bool, require_photos: bool
) -> CapabilityReport:
    report = CapabilityReport()
    if require_video:
        _check_ffmpeg(report)
    if require_photos:
        _check_cwebp(report)
    return report
