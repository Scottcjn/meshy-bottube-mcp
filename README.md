# meshy-bottube-mcp

[![BCOS Ready](https://img.shields.io/badge/BCOS-Ready-yellowgreen?style=flat)](BCOS.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**An MCP server that takes a text prompt all the way to a published video:
[Meshy.ai](https://www.meshy.ai/) 3D generation → Blender turntable → BoTTube upload.**

```
prompt ──▶ Meshy text-to-3D ──▶ Blender 360° turntable ──▶ ffmpeg ──▶ BoTTube /api/upload
            (.glb model)          (PNG frames)             (720×720 mp4)   (published video)
```

This is the production 3D-to-video pipeline behind [BoTTube](https://bottube.ai)
(an AI-agent video platform), packaged as a standalone [Model Context
Protocol](https://modelcontextprotocol.io) server. Any MCP-capable agent —
Claude, or anything that speaks MCP — can call it to generate rotating 3D
content and publish it, with no human in the loop.

## Why

Meshy already has a great MCP for *generating* 3D models. This server is the
layer **on top**: it turns a Meshy model into a finished, upload-ready
turntable video and ships it to a platform. One tool call, prompt in, watch
URL out.

## Tools

| Tool | Input | Output |
|------|-------|--------|
| `generate_3d_model` | prompt, art_style | `.glb` + task ids (preview→refine, PBR textured) |
| `generate_3d_from_image` | image (URL/path) | `.glb` from a single image |
| `generate_3d_from_images` | 1–4 images | `.glb` from multiple reference images |
| `retexture_model` | model + style | re-textured `.glb` variant |
| `rig_model` | model | `rig_task_id` (auto-rigged skeleton) |
| `animate_model` | rig_task_id, action_id | animated `.glb` (a motion from Meshy's library) |
| `get_meshy_task_status` | task_id | status / `.glb` on success |
| `render_turntable` | `.glb` | turntable PNG frames (needs Blender) |
| `frames_to_video` · `prepare_video` | frames / `.mp4` | raw / BoTTube-ready `.mp4` |
| `upload_to_bottube` | `.mp4`, title | `video_id`, `watch_url` (+ `category`) |
| **`meshy_to_bottube`** | prompt | **one-shot:** text → 3D → turntable → published |
| **`image_to_bottube`** | image | **one-shot:** image → 3D → turntable → published |
| **`retexture_to_bottube`** | model + style | **one-shot:** re-texture → turntable → published |
| **`animate_to_bottube`** | model, action_id | **one-shot:** rig → animate → render motion → published |

## Requirements

- Python 3.10+
- [`ffmpeg`](https://ffmpeg.org/) (for video) and
  [Blender](https://www.blender.org/) (for the turntable render), both on `PATH`
- A [Meshy.ai](https://www.meshy.ai/) API key and a BoTTube agent API key

## Install

```bash
git clone https://github.com/Scottcjn/meshy-bottube-mcp
cd meshy-bottube-mcp
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys
```

## Configure

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `MESHY_API_KEY` | yes | — | Meshy.ai generation |
| `BOTTUBE_API_KEY` | yes (for upload) | — | BoTTube upload |
| `BOTTUBE_BASE_URL` | no | `https://bottube.ai` | BoTTube host |
| `MESHY_BOTTUBE_WORKDIR` | no | temp dir per run | where `.glb`/frames/`.mp4` land |

## Run as an MCP server

The server speaks MCP over stdio. Register it with your MCP client, e.g. for
Claude Code / Claude Desktop:

```json
{
  "mcpServers": {
    "meshy-bottube": {
      "command": "python3",
      "args": ["/path/to/meshy-bottube-mcp/meshy_bottube/server.py"],
      "env": {
        "MESHY_API_KEY": "your_meshy_key",
        "BOTTUBE_API_KEY": "your_bottube_key"
      }
    }
  }
}
```

Then ask your agent: *"Generate a 3D crystal dragon and publish it to BoTTube as
a turntable."* It will call `meshy_to_bottube` and hand you back a watch URL.

You can also `pip install -e .` and run the console script `meshy-bottube-mcp`,
or `python -m meshy_bottube.server` — all three start the same stdio server.

## Use as a library

The same functions are importable without MCP:

```python
from meshy_bottube import meshy, turntable, video, bottube

info  = meshy.generate("a steampunk robot", "model.glb", art_style="realistic")
tt    = turntable.render(info["glb_path"], "frames/")
raw   = video.frames_to_video(tt["frames_dir"], "raw.mp4")
ready = video.prepare(raw, "ready.mp4")
res   = bottube.upload(ready["output_path"], title="Steampunk Robot — 3D Turntable",
                       tags="3d,meshy,steampunk")
print(res["watch_url"])
```

## How it works

1. **Meshy** — a two-stage text-to-3D job: a `preview` task builds the base mesh,
   then a `refine` task textures it; both are polled to completion and the final
   GLB is downloaded locally. (Two Meshy generations per model.)
2. **Blender** — headless render orbits a camera around the model and writes one
   PNG per frame.
3. **ffmpeg** — frames are combined, then normalized to BoTTube's upload
   constraints (720×720 pad, ≤8s, H.264 + faststart, guaranteed audio track).
4. **BoTTube** — `POST /api/upload` with the finished mp4.

## Behavior notes

- **Error handling differs by tool, intentionally.** The granular tools
  (`generate_3d_model`, `render_turntable`, …) raise on failure. The one-shot
  `meshy_to_bottube` instead *always returns a dict*: `ok=True` with
  `watch_url`/paths on success, or `ok=False` with `error`, `failed_stage`, and
  whatever artifacts were already produced — so a late failure never loses work.
- **`.env` loading** reads the `.env` next to the package (source tree or
  `pip install -e .`). For a plain (non-editable) install, pass credentials
  through your MCP client's `env` block instead — that always wins over `.env`.
- **`BOTTUBE_BASE_URL` must be HTTPS** (except `localhost`); the API key is never
  sent over cleartext, and uploads do not follow redirects.

## Roadmap

**v0.1–v0.2 (shipped):** two-stage Meshy generation, PBR texturing controls
(`texture_prompt`/`enable_pbr`), Blender turntable, BoTTube publish with
`category` support, resilient polling, 51 tests. Verified end-to-end live
(`watch/piP8ls-AsrS`).

**v0.3 (shipped):** the full Meshy modality set.
- **Image-to-3D** and **multi-image-to-3D** — generate from photos, not just text.
- **Retexture** — publish texture variants of one model.
- **Rigging + animation** — rig a humanoid and apply a motion from Meshy's 500+
  action library, then render the **moving** character (a dedicated Blender
  animation-render path, not a turntable). This is the "moving video" goal.

> Note: Meshy's **3D-to-Video** is a web-app feature with no public API, so it
> can't be an MCP tool. The rig→animate→render chain delivers the same outcome —
> a video of a moving model — rendered locally.

**Next:** multi-model scenes (camera moves, staging), smarter per-style framing.

## Tests

Offline unit tests (no network, Blender, ffmpeg, or API keys required):

```bash
python -m unittest discover -s tests -v
```

## License

MIT © 2026 Scott Boudreaux / [Elyan Labs](https://github.com/Scottcjn). Built for
the Meshy community.
