"""meshy_bottube — the 3D-to-video pipeline behind BoTTube, as a library.

Four stages, each a small module:

    meshy      text prompt        -> Meshy.ai text-to-3D -> .glb model
    turntable  .glb model         -> Blender 360° orbit  -> PNG frames
    video      PNG frames         -> ffmpeg              -> BoTTube-ready .mp4
    bottube    .mp4               -> POST /api/upload     -> published video

The MCP server in ``server.py`` exposes these as tools; the same functions are
importable directly for scripting.
"""

__version__ = "0.1.0"

from . import meshy, turntable, video, bottube  # noqa: F401

__all__ = ["meshy", "turntable", "video", "bottube", "__version__"]
