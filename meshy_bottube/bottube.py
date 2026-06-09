"""Upload a finished video to BoTTube via the public upload API.

``POST {BOTTUBE_BASE_URL}/api/upload`` with an ``X-API-Key`` header and a
multipart body (title, description, tags, video). Returns the parsed JSON,
which includes ``video_id`` and ``watch_url`` on success.
"""
from __future__ import annotations

import os
import urllib.parse

import requests

DEFAULT_BASE_URL = "https://bottube.ai"
_UPLOAD_TIMEOUT = 120
# requests buffers the multipart body in memory; cap the file size so a huge
# input can't exhaust RAM. BoTTube clips are tiny (720p, <=8s), so this is
# generous headroom, not a real constraint.
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

# A light guardrail: this tool uploads whatever local path you point it at
# (intentional — so you can publish videos made elsewhere), but it should still
# refuse obvious non-videos rather than POST arbitrary file contents. The map
# also gives each extension its correct multipart Content-Type.
_EXT_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".m4v": "video/x-m4v",
}
_VIDEO_EXTS = set(_EXT_MIME)


class BoTTubeError(RuntimeError):
    """Missing key, missing file, or a non-2xx upload response."""


def _base_url() -> str:
    url = os.environ.get("BOTTUBE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise BoTTubeError(f"BOTTUBE_BASE_URL has no host: {url!r}")
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        raise BoTTubeError(
            f"BOTTUBE_BASE_URL must be a bare scheme://host[:port]: {url!r}")
    is_local = host in ("localhost", "127.0.0.1", "::1")
    # Refuse cleartext HTTP to anything but localhost — the X-API-Key header
    # must never travel unencrypted to a remote host.
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        raise BoTTubeError(
            f"BOTTUBE_BASE_URL must be https (got {url!r}); refusing to send "
            f"the API key over cleartext"
        )
    return url


def _api_key() -> str:
    key = os.environ.get("BOTTUBE_API_KEY")
    if not key:
        raise BoTTubeError(
            "BOTTUBE_API_KEY environment variable not set. "
            "Get one from your BoTTube agent settings."
        )
    return key


def _add_full_watch_url(body: dict, base_url: str) -> dict:
    """Return a copy of ``body`` with a relative ``watch_url`` promoted to an
    absolute ``watch_url_full`` (never mutates the caller's dict)."""
    result = dict(body)
    watch = result.get("watch_url")
    if isinstance(watch, str) and watch.startswith("/"):
        result["watch_url_full"] = f"{base_url}{watch}"
    return result


def upload(video_path: str, title: str, description: str = "",
           tags: str = "") -> dict:
    """Upload ``video_path`` to BoTTube. ``tags`` is a comma-separated string."""
    video_path = os.path.abspath(video_path)
    if not os.path.isfile(video_path):
        raise BoTTubeError(f"video file not found: {video_path}")
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in _VIDEO_EXTS:
        raise BoTTubeError(
            f"refusing to upload non-video file (extension {ext or 'none'!r}); "
            f"allowed: {', '.join(sorted(_VIDEO_EXTS))}"
        )
    if not title or not title.strip():
        raise BoTTubeError("a non-empty title is required")

    base = _base_url()
    url = f"{base}/api/upload"
    mime = _EXT_MIME[ext]
    try:
        with open(video_path, "rb") as fh:
            # Size the OPEN handle (the one we upload) to close the TOCTOU race
            # where the file is swapped/grown between a path-based size check
            # and the read.
            size = os.fstat(fh.fileno()).st_size
            if size > _MAX_UPLOAD_BYTES:
                raise BoTTubeError(
                    f"video is {size} bytes, over the "
                    f"{_MAX_UPLOAD_BYTES}-byte cap")
            resp = requests.post(
                url,
                headers={"X-API-Key": _api_key()},
                data={"title": title, "description": description, "tags": tags},
                files={"video": (os.path.basename(video_path), fh, mime)},
                timeout=_UPLOAD_TIMEOUT,
                # Don't follow redirects: a cross-origin 3xx would resend the
                # X-API-Key header (and the upload) to another host.
                allow_redirects=False,
            )
    except requests.RequestException as exc:
        raise BoTTubeError(f"upload request failed: {exc}") from exc

    if 300 <= resp.status_code < 400:
        raise BoTTubeError(
            f"upload got an unexpected redirect (HTTP {resp.status_code} -> "
            f"{resp.headers.get('Location', '?')}); not following it to protect "
            f"the API key"
        )
    if resp.status_code >= 400:
        raise BoTTubeError(
            f"upload failed (HTTP {resp.status_code}): {resp.text[:300]}"
        )
    try:
        body = resp.json()
    except ValueError:
        raise BoTTubeError(f"upload returned non-JSON response: {resp.text[:300]}")
    if not isinstance(body, dict):
        raise BoTTubeError(
            f"upload returned unexpected JSON shape: {str(body)[:300]}")

    # A 2xx with an application-level error, or with no id/url at all, is not a
    # successful upload — don't let it masquerade as one.
    if body.get("ok") is False or body.get("error"):
        raise BoTTubeError(
            f"BoTTube rejected the upload: {body.get('error') or body}")
    if not body.get("video_id") and not body.get("watch_url"):
        raise BoTTubeError(
            f"upload response missing video_id/watch_url: {str(body)[:300]}")

    return _add_full_watch_url(body, base)
