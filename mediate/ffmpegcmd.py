"""Building the ffmpeg argv for one conversion.

FFmpeg's defaults are the hazard here: left to itself it picks a "best"
stream per type and drops the rest, re-derives metadata, and bakes rotation
into the pixels. So every command is written out explicitly — each stream
mapped by its absolute index, each track's identity and dispositions copied
across, rotation handled in a separate display-matrix pass.

Nothing in this module touches the filesystem or runs a process; it turns an
inventory into an argument list, which is what lets the stream-mapping rules
be tested without media.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .inventory import (
    COLOR_FIELDS,
    audio_stream_label,
    inventory_streams,
    is_commentary_stream,
    preservable_artwork_streams,
    preservable_subtitle_streams,
    primary_video_streams,
)


def _video_mapping_args(inventory: dict) -> List[str]:
    """Map the primary picture and each preservable stream by absolute index."""
    primary = primary_video_streams(inventory)[0]
    primary_index = primary.get("index")
    args = ["-map", f"0:{primary_index}" if primary_index is not None else "0:v:0"]
    for audio_index, stream in enumerate(inventory_streams(inventory, "audio")):
        source_index = stream.get("index")
        args.extend(["-map", f"0:{source_index}" if source_index is not None else f"0:a:{audio_index}"])
        args.extend([
            f"-map_metadata:s:a:{audio_index}",
            f"0:s:a:{audio_index}",
        ])
        tags = stream.get("tags", {})
        language = tags.get("language")
        if language:
            args.extend([f"-metadata:s:a:{audio_index}", f"language={language}"])
        title = audio_stream_label(stream)
        if not title and is_commentary_stream(stream):
            title = "Commentary"
        if title:
            # MOV reports this field as `name` or `handler_name`; Matroska
            # generally reports `title`. Older FFmpeg MP4 muxers only retain
            # handler_name, so write both forms and validate them equivalently.
            args.extend([
                f"-metadata:s:a:{audio_index}", f"title={title}",
                f"-metadata:s:a:{audio_index}", f"handler_name={title}",
            ])
        dispositions = [
            name for name, enabled in stream.get("disposition", {}).items()
            if enabled
        ]
        args.extend([
            f"-disposition:a:{audio_index}",
            "+".join(dispositions) if dispositions else "0",
        ])
    for subtitle_index, stream in enumerate(preservable_subtitle_streams(inventory)):
        source_index = stream.get("index")
        args.extend(["-map", f"0:{source_index}"])
        tags = stream.get("tags", {})
        for name in ("language", "title"):
            if tags.get(name):
                args.extend([
                    f"-metadata:s:s:{subtitle_index}", f"{name}={tags[name]}"
                ])
        dispositions = [
            name for name, enabled in stream.get("disposition", {}).items()
            if enabled
        ]
        args.extend([
            f"-disposition:s:{subtitle_index}",
            "+".join(dispositions) if dispositions else "0",
        ])
    for artwork_index, stream in enumerate(preservable_artwork_streams(inventory), 1):
        source_index = stream.get("index")
        if source_index is None:
            continue
        args.extend(["-map", f"0:{source_index}"])
        args.extend([f"-disposition:v:{artwork_index}", "attached_pic"])
    return args


def _preserved_stream_codec_args(inventory: dict) -> List[str]:
    args: List[str] = []
    if preservable_subtitle_streams(inventory):
        args.extend(["-c:s", "mov_text"])
    for artwork_index, _stream in enumerate(preservable_artwork_streams(inventory), 1):
        args.extend([f"-c:v:{artwork_index}", "copy"])
    return args


def _video_metadata_args(inventory: dict) -> List[str]:
    """Carry source colour declarations onto the primary output video."""
    videos = primary_video_streams(inventory)
    if not videos:
        return []
    video = videos[0]
    args: List[str] = []
    option_names = {
        "color_range": "color_range",
        "color_space": "colorspace",
        "color_transfer": "color_trc",
        "color_primaries": "color_primaries",
        "chroma_location": "chroma_sample_location",
    }
    for field in COLOR_FIELDS:
        value = video.get("color", {}).get(field)
        if value and value != "unknown":
            args.extend([f"-{option_names[field]}:v:0", str(value)])
    return args


def _build_rotation_command(
    input_path: Path,
    output_path: Path,
    inventory: dict,
    copy_video: bool = False,
) -> List[str]:
    """Remux an encoded MP4 while writing an explicit display matrix.

    FFmpeg preserves a display matrix during stream copy, but does not carry
    it through video encoding. Its legacy output `rotate` metadata also has
    container/version-dependent behaviour, so a short second pass applies the
    documented input-side display override and copies the encoded streams.
    """
    rotation = primary_video_streams(inventory)[0].get("rotation")
    return [
        "ffmpeg", "-nostdin", "-y",
        "-display_rotation:v:0", str(rotation),
        "-i", str(input_path),
        "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
        "-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0",
        "-map_chapters", "0",
        "-c", "copy",
        *_video_metadata_args(inventory),
        "-tag:v:0", _apple_video_tag(inventory) if copy_video else "avc1",
        "-tag:a", "mp4a",
        "-brand", "mp42", "-movflags", "+faststart",
        str(output_path),
    ]


def _build_command(
    kind: str,
    input_path: Path,
    output_path: Path,
    inventory: Optional[dict] = None,
    repair: bool = False,
    copy_video: bool = False,
    output_format: str = "webp",
) -> List[str]:
    if kind == "photo":
        if output_format == "avif":
            return [
                "avifenc", "--lossless", "--jobs", "all",
                str(input_path), str(output_path),
            ]
        return [
            "cwebp", "-lossless", "-metadata", "all", "-preset", "photo",
            str(input_path), "-o", str(output_path),
        ]
    if kind == "video":
        if inventory is None:
            raise ValueError("video conversion requires a stream inventory")
        input_repair = [
            "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        ] if repair else []
        return [
            "ffmpeg", "-nostdin", "-y", *input_repair,
            "-noautorotate", "-i", str(input_path),
            *_video_mapping_args(inventory),
            "-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0",
            "-map_chapters", "0",
            *(
                ["-c:v:0", "copy"]
                if copy_video
                else ["-c:v:0", "libx264", "-preset", "slow", "-crf", "18"]
            ),
            "-c:a", "aac", "-b:a", "256k",
            *_preserved_stream_codec_args(inventory),
            *([] if copy_video else [
                "-pix_fmt", "yuv420p",
                "-vf", (
                    "setpts=N/FRAME_RATE/TB,pad=ceil(iw/2)*2:ceil(ih/2)*2"
                    if repair else "pad=ceil(iw/2)*2:ceil(ih/2)*2"
                ),
            ]),
            *(["-fps_mode", "passthrough"] if repair else []),
            *_video_metadata_args(inventory),
            "-tag:v:0", _apple_video_tag(inventory) if copy_video else "avc1",
            "-tag:a", "mp4a",
            "-brand", "mp42", "-movflags", "+faststart",
            str(output_path),
        ]
    if kind in ("gif", "webp"):
        return [
            "ffmpeg", "-nostdin", "-y", "-i", str(input_path),
            "-tag:v:0", "avc1", "-brand", "mp42", "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(output_path),
        ]
    raise ValueError(f"unknown job kind: {kind}")


def _build_remux_command(
    input_path: Path,
    output_path: Path,
    inventory: dict,
    repair: bool = False,
) -> List[str]:
    """Remux into MP4 with ``-c copy`` — no re-encoding."""
    input_repair = [
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
    ] if repair else []
    return [
        "ffmpeg", "-nostdin", "-y", *input_repair,
        "-noautorotate", "-i", str(input_path),
        *_video_mapping_args(inventory),
        "-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0",
        "-map_chapters", "0",
        "-c", "copy",
        *_preserved_stream_codec_args(inventory),
        *_video_metadata_args(inventory),
        "-tag:v:0", _apple_video_tag(inventory), "-tag:a", "mp4a",
        "-brand", "mp42", "-movflags", "+faststart",
        str(output_path),
    ]


def _apple_video_tag(inventory: dict) -> str:
    videos = primary_video_streams(inventory)
    return "hvc1" if videos and videos[0].get("codec_name") == "hevc" else "avc1"
