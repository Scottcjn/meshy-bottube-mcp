"""ffmpeg stages: PNG frames -> raw mp4, and raw mp4 -> BoTTube-ready mp4.

``prepare`` ports BoTTube's ``prepare_video.sh``: pad to 720x720, cap at 8s,
H.264 + faststart, and guarantee an audio track (silent if the source has
none) for browser autoplay compatibility.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# BoTTube upload constraints (see SKILL.md): 720x720 max, short clips, faststart.
TARGET_SIZE = 720
MAX_DURATION = 8
SIZE_WARN_BYTES = 2 * 1024 * 1024  # 2 MB

# Wall-clock caps so a wedged ffmpeg/ffprobe can't hang the host.
FFMPEG_TIMEOUT = 600
FFPROBE_TIMEOUT = 30

_SCALE_PAD = (
    f"scale='min({TARGET_SIZE},iw)':'min({TARGET_SIZE},ih)'"
    f":force_original_aspect_ratio=decrease,"
    f"pad={TARGET_SIZE}:{TARGET_SIZE}:(ow-iw)/2:(oh-ih)/2:color=black"
)


class VideoError(RuntimeError):
    """ffmpeg/ffprobe missing or a non-zero exit."""


def _require(tool: str) -> None:
    if shutil.which(tool) is None:
        raise VideoError(f"{tool} not found in PATH. Install ffmpeg.")


def _run(cmd: list[str]) -> None:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,  # keep ffmpeg off the MCP stdio stream
            timeout=FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"{cmd[0]} timed out after {FFMPEG_TIMEOUT}s") from exc
    except OSError as exc:
        raise VideoError(f"could not launch {cmd[0]}: {exc}") from exc
    if result.returncode != 0:
        raise VideoError(f"{cmd[0]} failed:\n{result.stderr[-2000:]}")


def frames_to_video(frames_dir: str, output_path: str, fps: int = 30,
                    duration: int = 6, pattern: str = "%04d.png") -> str:
    """Combine numbered PNG frames into an H.264 mp4."""
    if fps < 1 or duration < 1:
        raise VideoError(f"fps and duration must be >= 1 (got fps={fps}, "
                         f"duration={duration})")
    _require("ffmpeg")
    frames_dir = os.path.abspath(frames_dir)
    if not os.path.isdir(frames_dir):
        raise VideoError(f"frames directory not found: {frames_dir}")
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    _run([
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", os.path.join(frames_dir, pattern),
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
        output_path,
    ])
    return output_path


def _has_audio(path: str) -> bool:
    """True iff ffprobe reports an audio stream. On probe failure, treat the
    input as having no audio (the silent-track branch is the safe default)."""
    _require("ffprobe")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_streams", path],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=FFPROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    return "codec_type=audio" in result.stdout


def prepare(input_path: str, output_path: str) -> dict:
    """Resize/pad to 720x720, cap duration, ensure audio, enable faststart.

    Returns {output_path, size_bytes, oversize} (oversize=True if >2MB).
    """
    _require("ffmpeg")
    input_path = os.path.abspath(input_path)
    output_path = os.path.abspath(output_path)
    if not os.path.isfile(input_path):
        raise VideoError(f"input video not found: {input_path}")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    common_v = [
        "-vf", _SCALE_PAD,
        "-c:v", "libx264", "-profile:v", "high", "-crf", "28",
        "-preset", "medium", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]

    if _has_audio(input_path):
        cmd = [
            "ffmpeg", "-y", "-i", input_path, "-t", str(MAX_DURATION),
            *common_v,
            "-maxrate", "800k", "-bufsize", "1600k",
            "-c:a", "aac", "-b:a", "96k", "-ac", "2",
            output_path,
        ]
    else:
        # Add a silent stereo track for browser autoplay compatibility.
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(MAX_DURATION),
            *common_v,
            "-maxrate", "900k", "-bufsize", "1800k",
            "-c:a", "aac", "-b:a", "32k", "-ac", "2", "-shortest",
            output_path,
        ]
    _run(cmd)

    size = os.path.getsize(output_path)
    return {
        "output_path": output_path,
        "size_bytes": size,
        "oversize": size > SIZE_WARN_BYTES,
    }
