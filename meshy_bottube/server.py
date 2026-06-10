#!/usr/bin/env python3
"""meshy-bottube MCP server.

Exposes the BoTTube 3D-to-video pipeline as MCP tools so any MCP-capable agent
can take a text prompt all the way to a published BoTTube video:

    prompt -> Meshy text-to-3D -> Blender turntable -> ffmpeg -> /api/upload

Run it either way:
    python -m meshy_bottube.server
    python /path/to/meshy-bottube-mcp/meshy_bottube/server.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# When this file is the program entry (run directly by path, or `-m`), put the
# ADJACENT source tree first so it wins over any stale installed meshy_bottube.
# When merely imported (console-script `main`, tests, library use), leave the
# process-global import state alone unless the package can't be resolved at all.
if __name__ == "__main__":
    sys.path.insert(0, _PKG_PARENT)
else:
    try:
        import meshy_bottube  # noqa: F401
    except ImportError:
        sys.path.insert(0, _PKG_PARENT)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from meshy_bottube import bottube, meshy, turntable, video  # noqa: E402


def _load_dotenv() -> None:
    """Best-effort .env loader (no dependency). Reads KEY=VALUE lines from the
    repository-owned .env only (next to this package), without overriding real
    env vars. The cwd is deliberately NOT searched: a .env in an untrusted
    working directory must not be able to set BOTTUBE_BASE_URL and redirect a
    real API key. MCP clients normally inject env via their config; this just
    makes the documented `cp .env.example .env` flow work for CLI use too."""
    # Only auto-load when _PKG_PARENT looks like a source checkout (it has a
    # pyproject.toml). For a non-editable install _PKG_PARENT is site-packages,
    # where a stray .env could maliciously set BOTTUBE_BASE_URL — never load it.
    if not os.path.isfile(os.path.join(_PKG_PARENT, "pyproject.toml")):
        return
    candidate = os.path.join(_PKG_PARENT, ".env")
    if not os.path.isfile(candidate):
        return
    try:
        with open(candidate, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass  # a missing/unreadable .env should never crash startup


mcp = FastMCP("meshy-bottube")


def _workdir(tag: str = "") -> str:
    """A working directory for intermediate artifacts (no input to sit beside)."""
    base = os.environ.get("MESHY_BOTTUBE_WORKDIR")
    if base:
        # Full uuid (not a short prefix) so concurrent calls under a fixed
        # MESHY_BOTTUBE_WORKDIR can't collide into a shared directory.
        path = os.path.join(os.path.abspath(base), tag or uuid.uuid4().hex)
        os.makedirs(path, exist_ok=True)
        return path
    return tempfile.mkdtemp(prefix="meshy_bottube_")


def _preflight(*, need_meshy: bool = False, need_blender: bool = False,
               need_ffmpeg: bool = False, need_bottube: bool = False) -> None:
    """Fail before doing billed/expensive work if a dependency is missing."""
    import shutil
    missing = []
    if need_meshy and not os.environ.get("MESHY_API_KEY"):
        missing.append("MESHY_API_KEY env var")
    if need_bottube:
        if not os.environ.get("BOTTUBE_API_KEY"):
            missing.append("BOTTUBE_API_KEY env var")
        else:
            # Validate the destination URL now (https/host) rather than after
            # a billed Meshy generation + render.
            try:
                bottube._base_url()
            except bottube.BoTTubeError as exc:
                missing.append(str(exc))
    if need_blender and shutil.which("blender") is None:
        missing.append("blender on PATH")
    if need_ffmpeg and (shutil.which("ffmpeg") is None
                        or shutil.which("ffprobe") is None):
        missing.append("ffmpeg/ffprobe on PATH")
    if missing:
        raise RuntimeError("preflight failed — missing: " + ", ".join(missing))


@mcp.tool()
def generate_3d_model(prompt: str, art_style: str = "realistic",
                      should_remesh: bool = True, texture_prompt: str = "",
                      enable_pbr: bool = True, timeout: int = 600) -> dict:
    """Generate a 3D model from a text prompt via Meshy.ai (preview → refine).

    art_style: realistic | cartoon | low-poly | sculpture.
    The refine stage TEXTURES the model: enable_pbr (default True) for PBR
    textures, and texture_prompt for extra texturing guidance (e.g. "weathered
    bronze, mossy"). Blocks until the textured model is ready; returns its local
    .glb path and both Meshy task ids.
    """
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "model.glb")
    return meshy.generate(prompt, out, art_style=art_style,
                          should_remesh=should_remesh,
                          texture_prompt=texture_prompt or None,
                          enable_pbr=enable_pbr, timeout=timeout)


@mcp.tool()
def get_meshy_task_status(task_id: str, download: bool = False) -> dict:
    """Inspect any Meshy task by id. If it SUCCEEDED and download=True and it
    has a GLB, download it locally and include the path."""
    status = meshy.get_task(task_id)
    state = status.get("status", "UNKNOWN")
    result = {"task_id": task_id, "status": state,
              "progress": status.get("progress", 0),
              "model_urls": status.get("model_urls", {})}
    if state == "SUCCEEDED" and download and (status.get("model_urls") or {}).get("glb"):
        result["glb_path"] = meshy.download_glb(
            status, os.path.join(_workdir(), "model.glb"))
    return result


@mcp.tool()
def render_turntable(glb_path: str, frames: int = 180,
                     resolution: int = 720) -> dict:
    """Render a GLB model as a 360° turntable to PNG frames (requires Blender).
    Frames are written to a fresh working dir; the path is returned."""
    frames_dir = _workdir()
    return turntable.render(glb_path, frames_dir, frames=frames,
                            resolution=resolution)


@mcp.tool()
def frames_to_video(frames_dir: str, fps: int = 30, duration: int = 6) -> dict:
    """Combine numbered PNG frames into a raw H.264 mp4."""
    out = os.path.join(_workdir(), "turntable.mp4")
    path = video.frames_to_video(frames_dir, out, fps=fps, duration=duration)
    return {"video_path": path}


@mcp.tool()
def prepare_video(video_path: str) -> dict:
    """Make a video meet BoTTube upload constraints (720x720, faststart, audio)."""
    out = os.path.join(_workdir(), "ready.mp4")
    return video.prepare(video_path, out)


@mcp.tool()
def upload_to_bottube(video_path: str, title: str, description: str = "",
                      tags: str = "", category: str = "") -> dict:
    """Upload a finished mp4 to BoTTube. tags is comma-separated; category is an
    optional BoTTube category id (e.g. "comedy", "ai-art", "music").

    Note: this uploads whatever local file you point it at, under your own
    BoTTube API key — intentional, so you can publish videos made elsewhere."""
    _preflight(need_bottube=True)
    return bottube.upload(video_path, title, description=description, tags=tags,
                          category=category)


@mcp.tool()
def meshy_to_bottube(prompt: str, title: str, description: str = "",
                     tags: str = "3d,meshy,turntable", category: str = "",
                     art_style: str = "realistic", should_remesh: bool = True,
                     texture_prompt: str = "", enable_pbr: bool = True,
                     frames: int = 180, resolution: int = 720, fps: int = 30,
                     duration: int = 6, timeout: int = 600) -> dict:
    """One-shot: prompt -> Meshy 3D -> turntable -> video -> BoTTube upload.

    Preflights every dependency up front (so a missing Blender/ffmpeg/key can't
    waste a billed Meshy generation), then runs the whole pipeline in a single
    working directory.

    Always returns a dict. On success: ``ok=True`` plus ``watch_url`` /
    ``watch_url_full`` and every intermediate path. On a known stage failure:
    ``ok=False`` with ``error`` / ``failed_stage`` and whatever artifacts were
    produced before the failure (so nothing is silently lost).
    """
    steps: dict = {"prompt": prompt, "ok": False}
    stage = "validate"
    try:
        # Validate cheap params BEFORE the billed Meshy call, so bad bounds
        # fail for free instead of after two generations.
        if art_style not in meshy.ART_STYLES:
            raise ValueError(f"art_style must be one of {meshy.ART_STYLES}, "
                             f"got {art_style!r}")
        if not turntable.MIN_FRAMES <= frames <= turntable.MAX_FRAMES:
            raise ValueError(f"frames must be in [{turntable.MIN_FRAMES}, "
                             f"{turntable.MAX_FRAMES}], got {frames}")
        if not turntable.MIN_RESOLUTION <= resolution <= turntable.MAX_RESOLUTION:
            raise ValueError(f"resolution must be in [{turntable.MIN_RESOLUTION}, "
                             f"{turntable.MAX_RESOLUTION}], got {resolution}")
        if fps < 1 or duration < 1:
            raise ValueError(f"fps and duration must be >= 1 (got fps={fps}, "
                             f"duration={duration})")
        if duration > video.MAX_DURATION:
            raise ValueError(f"duration must be <= {video.MAX_DURATION}s "
                             f"(BoTTube caps the published clip), got {duration}")
        if frames < fps * duration:
            raise ValueError(
                f"frames ({frames}) < fps*duration ({fps * duration}); the "
                f"video would be shorter than the requested {duration}s. "
                f"Raise frames or lower fps/duration.")
        if not title or not title.strip():
            raise ValueError("title must be a non-empty string")
        if timeout < 1:
            raise ValueError(f"timeout must be >= 1, got {timeout}")

        stage = "preflight"
        _preflight(need_meshy=True, need_blender=True, need_ffmpeg=True,
                   need_bottube=True)
        stage = "workdir"
        work = _workdir()
        steps["workdir"] = work
        stage = "meshy"
        glb = meshy.generate(prompt, os.path.join(work, "model.glb"),
                             art_style=art_style, should_remesh=should_remesh,
                             texture_prompt=texture_prompt or None,
                             enable_pbr=enable_pbr, timeout=timeout)
        steps["glb_path"] = glb["glb_path"]

        stage = "turntable"
        tt = turntable.render(glb["glb_path"], os.path.join(work, "frames"),
                              frames=frames, resolution=resolution)
        steps["frame_count"] = tt["frame_count"]
        steps["frames_dir"] = tt["frames_dir"]

        stage = "frames_to_video"
        raw = video.frames_to_video(tt["frames_dir"],
                                    os.path.join(work, "raw.mp4"),
                                    fps=fps, duration=duration)
        steps["raw_video_path"] = raw

        stage = "prepare_video"
        ready = video.prepare(raw, os.path.join(work, "ready.mp4"))
        steps["video_path"] = ready["output_path"]
        steps["oversize"] = ready["oversize"]

        stage = "upload"
        upload = bottube.upload(ready["output_path"], title,
                                description=description, tags=tags,
                                category=category)
        steps["upload"] = upload
        steps["watch_url"] = upload.get("watch_url")
        steps["watch_url_full"] = upload.get("watch_url_full") or upload.get("watch_url")
        if upload.get("unconfirmed"):
            steps["unconfirmed"] = True
        steps["ok"] = True
        return steps
    except Exception as exc:  # noqa: BLE001 — orchestrator's contract is to
        # always return a structured dict (ok=False + which stage failed +
        # partial artifacts), never to leak a raw exception to the MCP caller.
        steps["error"] = f"{type(exc).__name__}: {exc}"
        steps["failed_stage"] = stage
        return steps


# --- shared one-shot helpers ---------------------------------------------

def _validate_publish_params(*, title: str, frames: int, resolution: int,
                             fps: int, duration: int, timeout: int) -> None:
    """Cheap param validation shared by the one-shot tools — runs BEFORE any
    billed Meshy work so bad input fails for free."""
    if not turntable.MIN_FRAMES <= frames <= turntable.MAX_FRAMES:
        raise ValueError(f"frames must be in [{turntable.MIN_FRAMES}, "
                         f"{turntable.MAX_FRAMES}], got {frames}")
    if not turntable.MIN_RESOLUTION <= resolution <= turntable.MAX_RESOLUTION:
        raise ValueError(f"resolution must be in [{turntable.MIN_RESOLUTION}, "
                         f"{turntable.MAX_RESOLUTION}], got {resolution}")
    if fps < 1 or duration < 1:
        raise ValueError(f"fps and duration must be >= 1 (got fps={fps}, "
                         f"duration={duration})")
    if duration > video.MAX_DURATION:
        raise ValueError(f"duration must be <= {video.MAX_DURATION}s, got {duration}")
    if frames < fps * duration:
        raise ValueError(f"frames ({frames}) < fps*duration ({fps * duration}); "
                         f"video would be shorter than {duration}s")
    if not title or not title.strip():
        raise ValueError("title must be a non-empty string")
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")


def _render_and_publish(work: str, glb_path: str, *, title: str,
                        description: str, tags: str, category: str,
                        frames: int, resolution: int, fps: int, duration: int,
                        steps: dict) -> dict:
    """Shared tail: GLB -> turntable -> video -> prepare -> BoTTube upload.
    Records intermediate paths in ``steps``; returns the upload dict. Raises on
    failure (the caller's try/except records the failed stage)."""
    tt = turntable.render(glb_path, os.path.join(work, "frames"),
                          frames=frames, resolution=resolution)
    steps["frame_count"] = tt["frame_count"]
    steps["frames_dir"] = tt["frames_dir"]
    raw = video.frames_to_video(tt["frames_dir"], os.path.join(work, "raw.mp4"),
                                fps=fps, duration=duration)
    steps["raw_video_path"] = raw
    ready = video.prepare(raw, os.path.join(work, "ready.mp4"))
    steps["video_path"] = ready["output_path"]
    steps["oversize"] = ready["oversize"]
    upload = bottube.upload(ready["output_path"], title, description=description,
                            tags=tags, category=category)
    steps["upload"] = upload
    steps["watch_url"] = upload.get("watch_url")
    steps["watch_url_full"] = upload.get("watch_url_full") or upload.get("watch_url")
    if upload.get("unconfirmed"):
        steps["unconfirmed"] = True
    return upload


@mcp.tool()
def generate_3d_from_image(image: str, texture_prompt: str = "",
                           enable_pbr: bool = True, should_texture: bool = True,
                           should_remesh: bool = True, timeout: int = 600) -> dict:
    """Image-to-3D: a photo/render (public URL or local file path) -> textured
    .glb. Returns the local .glb path and the Meshy task id."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "model.glb")
    return meshy.generate_from_image(
        image, out, enable_pbr=enable_pbr, should_texture=should_texture,
        should_remesh=should_remesh, texture_prompt=texture_prompt or None,
        timeout=timeout)


@mcp.tool()
def generate_3d_from_images(images: list, texture_prompt: str = "",
                            enable_pbr: bool = True, should_texture: bool = True,
                            should_remesh: bool = True, timeout: int = 600) -> dict:
    """Multi-image-to-3D: 1-4 reference images (URLs or local paths) of one
    subject -> a higher-fidelity textured .glb."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "model.glb")
    return meshy.generate_from_images(
        images, out, enable_pbr=enable_pbr, should_texture=should_texture,
        should_remesh=should_remesh, texture_prompt=texture_prompt or None,
        timeout=timeout)


@mcp.tool()
def image_to_bottube(image: str, title: str, description: str = "",
                     tags: str = "3d,meshy,turntable", category: str = "",
                     enable_pbr: bool = True, should_texture: bool = True,
                     should_remesh: bool = True, frames: int = 180,
                     resolution: int = 720, fps: int = 30, duration: int = 6,
                     timeout: int = 600) -> dict:
    """One-shot: an image -> Meshy image-to-3D -> turntable -> BoTTube video.

    Always returns a dict (ok + watch_url, or ok=False + error/failed_stage +
    partial artifacts)."""
    steps: dict = {"source_image": image, "ok": False}
    stage = "validate"
    try:
        _validate_publish_params(title=title, frames=frames, resolution=resolution,
                                 fps=fps, duration=duration, timeout=timeout)
        stage = "preflight"
        _preflight(need_meshy=True, need_blender=True, need_ffmpeg=True,
                   need_bottube=True)
        stage = "workdir"
        work = _workdir()
        steps["workdir"] = work
        stage = "meshy"
        glb = meshy.generate_from_image(
            image, os.path.join(work, "model.glb"), enable_pbr=enable_pbr,
            should_texture=should_texture, should_remesh=should_remesh,
            timeout=timeout)
        steps["glb_path"] = glb["glb_path"]
        stage = "render_publish"
        _render_and_publish(work, glb["glb_path"], title=title,
                            description=description, tags=tags, category=category,
                            frames=frames, resolution=resolution, fps=fps,
                            duration=duration, steps=steps)
        steps["ok"] = True
        return steps
    except Exception as exc:  # noqa: BLE001 — one-shot always returns a dict
        steps["error"] = f"{type(exc).__name__}: {exc}"
        steps["failed_stage"] = stage
        return steps


@mcp.tool()
def retexture_model(text_style_prompt: str = "", image_style_url: str = "",
                    input_task_id: str = "", model_url: str = "",
                    enable_pbr: bool = True, timeout: int = 600) -> dict:
    """Re-texture an existing model into a new variant. Identify the source by
    input_task_id (a prior Meshy task) or a public model_url; describe the look
    with text_style_prompt or image_style_url. Returns the new .glb path."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "model.glb")
    return meshy.retexture(
        out, input_task_id=input_task_id or None, model_url=model_url or None,
        text_style_prompt=text_style_prompt or None,
        image_style_url=image_style_url or None, enable_pbr=enable_pbr,
        timeout=timeout)


@mcp.tool()
def retexture_to_bottube(title: str, text_style_prompt: str = "",
                         image_style_url: str = "", input_task_id: str = "",
                         model_url: str = "", description: str = "",
                         tags: str = "3d,meshy,retexture", category: str = "",
                         enable_pbr: bool = True, frames: int = 180,
                         resolution: int = 720, fps: int = 30, duration: int = 6,
                         timeout: int = 600) -> dict:
    """One-shot: re-texture an existing model -> turntable -> BoTTube video.
    Great for publishing texture variants of one model. Always returns a dict."""
    steps: dict = {"ok": False}
    stage = "validate"
    try:
        _validate_publish_params(title=title, frames=frames, resolution=resolution,
                                 fps=fps, duration=duration, timeout=timeout)
        stage = "preflight"
        _preflight(need_meshy=True, need_blender=True, need_ffmpeg=True,
                   need_bottube=True)
        stage = "workdir"
        work = _workdir()
        steps["workdir"] = work
        stage = "meshy"
        glb = meshy.retexture(
            os.path.join(work, "model.glb"), input_task_id=input_task_id or None,
            model_url=model_url or None,
            text_style_prompt=text_style_prompt or None,
            image_style_url=image_style_url or None, enable_pbr=enable_pbr,
            timeout=timeout)
        steps["glb_path"] = glb["glb_path"]
        stage = "render_publish"
        _render_and_publish(work, glb["glb_path"], title=title,
                            description=description, tags=tags, category=category,
                            frames=frames, resolution=resolution, fps=fps,
                            duration=duration, steps=steps)
        steps["ok"] = True
        return steps
    except Exception as exc:  # noqa: BLE001 — one-shot always returns a dict
        steps["error"] = f"{type(exc).__name__}: {exc}"
        steps["failed_stage"] = stage
        return steps


@mcp.tool()
def rig_model(input_task_id: str = "", model_url: str = "",
              height_meters: float = 1.7, timeout: int = 600) -> dict:
    """Auto-rig a humanoid model for animation (a skeleton). Identify it by
    input_task_id (a prior Meshy generation) or a public model_url. Returns
    rig_task_id — feed it to animate_model."""
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    return meshy.rig(input_task_id=input_task_id or None,
                     model_url=model_url or None, height_meters=height_meters,
                     timeout=timeout)


@mcp.tool()
def animate_model(rig_task_id: str, action_id: int, fps: int = 30,
                  timeout: int = 600) -> dict:
    """Apply a motion to a rigged model -> animated .glb. action_id is from
    Meshy's library (e.g. 0=Idle, 1=Walking, 4=Attack, 22=Dancing).
    fps must be one Meshy supports: 24, 25, 30, or 60."""
    if fps not in (24, 25, 30, 60):
        raise ValueError(f"fps must be one of 24, 25, 30, 60; got {fps}")
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    _preflight(need_meshy=True)
    out = os.path.join(_workdir(), "anim.glb")
    return meshy.animate(rig_task_id, action_id, out, fps=fps, timeout=timeout)


@mcp.tool()
def animate_to_bottube(action_id: int, title: str, input_task_id: str = "",
                       model_url: str = "", description: str = "",
                       tags: str = "3d,meshy,animation", category: str = "",
                       height_meters: float = 1.7, fps: int = 30,
                       resolution: int = 720, timeout: int = 600) -> dict:
    """One-shot: a humanoid model -> Meshy rig -> animate (action_id) -> render
    the MOTION -> BoTTube video. Unlike a turntable, the published clip shows the
    character performing the action. Always returns a dict. The clip length
    follows the animation (capped at BoTTube's max)."""
    steps: dict = {"action_id": action_id, "ok": False}
    stage = "validate"
    try:
        if not (input_task_id or model_url):
            raise ValueError("provide input_task_id or model_url")
        if not isinstance(action_id, int) or action_id < 0:
            raise ValueError("action_id must be a non-negative integer")
        if not turntable.MIN_RESOLUTION <= resolution <= turntable.MAX_RESOLUTION:
            raise ValueError(f"resolution must be in [{turntable.MIN_RESOLUTION}, "
                             f"{turntable.MAX_RESOLUTION}], got {resolution}")
        if fps not in (24, 25, 30, 60):
            # The encode fps + duration math must match what Meshy produces; it
            # only converts to these values, so reject others up front.
            raise ValueError(f"fps must be one of 24, 25, 30, 60; got {fps}")
        if not title or not title.strip():
            raise ValueError("title must be a non-empty string")
        if timeout < 1:
            raise ValueError(f"timeout must be >= 1, got {timeout}")

        stage = "preflight"
        _preflight(need_meshy=True, need_blender=True, need_ffmpeg=True,
                   need_bottube=True)
        stage = "workdir"
        work = _workdir()
        steps["workdir"] = work
        stage = "rigging"
        rigged = meshy.rig(input_task_id=input_task_id or None,
                           model_url=model_url or None,
                           height_meters=height_meters, timeout=timeout)
        steps["rig_task_id"] = rigged["rig_task_id"]
        stage = "animation"
        anim = meshy.animate(rigged["rig_task_id"], action_id,
                             os.path.join(work, "anim.glb"), fps=fps,
                             timeout=timeout)
        steps["glb_path"] = anim["glb_path"]
        stage = "render"
        tt = turntable.render_animation(anim["glb_path"],
                                        os.path.join(work, "frames"),
                                        resolution=resolution)
        steps["frame_count"] = tt["frame_count"]
        steps["frames_dir"] = tt["frames_dir"]
        stage = "frames_to_video"
        duration = max(1, min(video.MAX_DURATION,
                              round(tt["frame_count"] / max(1, fps))))
        raw = video.frames_to_video(tt["frames_dir"],
                                    os.path.join(work, "raw.mp4"),
                                    fps=fps, duration=duration)
        steps["raw_video_path"] = raw
        stage = "prepare_video"
        ready = video.prepare(raw, os.path.join(work, "ready.mp4"))
        steps["video_path"] = ready["output_path"]
        steps["oversize"] = ready["oversize"]
        stage = "upload"
        up = bottube.upload(ready["output_path"], title, description=description,
                            tags=tags, category=category)
        steps["upload"] = up
        steps["watch_url"] = up.get("watch_url")
        steps["watch_url_full"] = up.get("watch_url_full") or up.get("watch_url")
        if up.get("unconfirmed"):
            steps["unconfirmed"] = True
        steps["ok"] = True
        return steps
    except Exception as exc:  # noqa: BLE001 — one-shot always returns a dict
        steps["error"] = f"{type(exc).__name__}: {exc}"
        steps["failed_stage"] = stage
        return steps


def main() -> None:
    """Console-script entry point: run the MCP server over stdio.

    .env is loaded here (when the server actually runs) rather than at import
    time, so merely importing this module never mutates the environment.
    """
    _load_dotenv()
    mcp.run()


if __name__ == "__main__":
    main()
