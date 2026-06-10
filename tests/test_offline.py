"""Offline unit tests — no network, no Blender, no ffmpeg, no API keys.

These cover the input validation and pure helpers that guard every tool, so a
typo or contract drift fails here instead of mid-pipeline. Run with:

    python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshy_bottube import bottube, meshy, turntable, video  # noqa: E402


class TestMeshyValidation(unittest.TestCase):
    def test_headers_requires_key(self):
        old = os.environ.pop("MESHY_API_KEY", None)
        try:
            with self.assertRaises(meshy.MeshyError):
                meshy._headers()
        finally:
            if old is not None:
                os.environ["MESHY_API_KEY"] = old

    def test_bad_art_style_rejected_before_network(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.create_preview_task("a robot", art_style="nope")

    def test_empty_prompt_rejected(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.create_preview_task("   ", art_style="realistic")

    def test_refine_requires_preview_id(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.create_refine_task("")

    def test_download_glb_needs_model_url(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.download_glb({"status": "SUCCEEDED"}, "/tmp/nope.glb")

    def test_art_styles_constant(self):
        self.assertIn("realistic", meshy.ART_STYLES)

    def test_get_task_rejects_injection_id(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.get_task("../../evil?x=1")

    def test_get_task_accepts_uuid_like_id(self):
        # Valid format passes validation, then a mocked GET returns a status.
        resp = mock.MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"status": "PENDING"}
        old = os.environ.get("MESHY_API_KEY")
        os.environ["MESHY_API_KEY"] = "k"
        try:
            with mock.patch("meshy_bottube.meshy.requests.get", return_value=resp):
                self.assertEqual(meshy.get_task("0193b6f2-abcd")["status"],
                                 "PENDING")
        finally:
            if old is None:
                os.environ.pop("MESHY_API_KEY", None)
            else:
                os.environ["MESHY_API_KEY"] = old

    def test_refine_passes_texture_params(self):
        with mock.patch.object(meshy, "_create", return_value="rid") as created:
            meshy.create_refine_task("prev1", texture_prompt="mossy bronze",
                                     enable_pbr=True)
        payload = created.call_args.args[0]
        self.assertEqual(payload["mode"], "refine")
        self.assertTrue(payload["enable_pbr"])
        self.assertEqual(payload["texture_prompt"], "mossy bronze")

    def test_generate_annotates_refine_id_on_download_failure(self):
        # When download fails post-refine, the error must name the refine task
        # id so the caller can re-fetch instead of paying again.
        with mock.patch.object(meshy, "create_preview_task", return_value="prev1"), \
                mock.patch.object(meshy, "wait_for_task", return_value={}), \
                mock.patch.object(meshy, "create_refine_task", return_value="ref42"), \
                mock.patch.object(meshy, "download_glb",
                                  side_effect=meshy.MeshyError("disk full")):
            with self.assertRaises(meshy.MeshyError) as ctx:
                meshy.generate("a dragon", "/tmp/x/model.glb")
        self.assertIn("ref42", str(ctx.exception))


class TestImageInputs(unittest.TestCase):
    def test_url_passthrough(self):
        self.assertEqual(meshy.to_image_source("https://x/y.png"),
                         "https://x/y.png")

    def test_data_uri_passthrough(self):
        uri = "data:image/png;base64,AAAA"
        self.assertEqual(meshy.to_image_source(uri), uri)

    def test_local_image_to_data_uri(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"realpngbody")  # valid PNG magic
            path = fh.name
        try:
            uri = meshy.to_image_source(path)
            self.assertTrue(uri.startswith("data:image/png;base64,"))
        finally:
            os.unlink(path)

    def test_non_image_file_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(b"SECRET=hunter2\n")  # not an image despite .png name
            path = fh.name
        try:
            with self.assertRaises(meshy.MeshyError):
                meshy.to_image_source(path)
        finally:
            os.unlink(path)

    def test_mime_from_bytes_not_extension(self):
        # JPEG bytes in a file named .png must be labeled image/jpeg.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(b"\xff\xd8\xff\xe0realjpegbody")  # JPEG magic
            path = fh.name
        try:
            self.assertTrue(meshy.to_image_source(path).startswith(
                "data:image/jpeg;base64,"))
        finally:
            os.unlink(path)

    def test_missing_file_rejected(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.to_image_source("/no/such/file.png")

    def test_endpoint_allowlist_blocks_foreign_host(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.get_task("validtask", endpoint="https://evil.example.com/v1/x")

    def test_animate_model_rejects_unsupported_fps(self):
        from meshy_bottube import server
        with self.assertRaises(ValueError):
            server.animate_model("rigtask", 1, fps=29)

    def test_multi_image_count_validated(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.generate_from_images([], "/tmp/m.glb")
        with self.assertRaises(meshy.MeshyError):
            meshy.generate_from_images(["a", "b", "c", "d", "e"], "/tmp/m.glb")

    def test_retexture_requires_source_and_style(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.retexture("/tmp/m.glb", text_style_prompt="gold")  # no source
        with self.assertRaises(meshy.MeshyError):
            meshy.retexture("/tmp/m.glb", input_task_id="t")  # no style

    def test_rig_requires_source(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.rig()

    def test_animate_validates_ids(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.animate("../bad", 1, "/tmp/a.glb")
        with self.assertRaises(meshy.MeshyError):
            meshy.animate("validtask", -1, "/tmp/a.glb")


class TestTurntableBounds(unittest.TestCase):
    def test_zero_frames_rejected(self):
        # Bounds are checked before the Blender lookup, so no Blender needed.
        with self.assertRaises(turntable.TurntableError):
            turntable.render("model.glb", "/tmp/frames", frames=0)

    def test_too_many_frames_rejected(self):
        with self.assertRaises(turntable.TurntableError):
            turntable.render("model.glb", "/tmp/frames",
                             frames=turntable.MAX_FRAMES + 1)

    def test_resolution_out_of_range_rejected(self):
        with self.assertRaises(turntable.TurntableError):
            turntable.render("model.glb", "/tmp/frames", resolution=8)


class TestVideoBounds(unittest.TestCase):
    def test_bad_fps_rejected_before_ffmpeg(self):
        with self.assertRaises(video.VideoError):
            video.frames_to_video("/tmp/frames", "/tmp/out.mp4", fps=0)

    def test_bad_duration_rejected(self):
        with self.assertRaises(video.VideoError):
            video.frames_to_video("/tmp/frames", "/tmp/out.mp4", duration=0)

    def test_scale_pad_targets_720(self):
        self.assertIn("720", video._SCALE_PAD)


class TestFrameNormalization(unittest.TestCase):
    """Real-filesystem coverage of the frame-renaming logic (no Blender)."""

    def test_numeric_sort_and_contiguous_rename(self):
        d = tempfile.mkdtemp()
        # Unpadded names, deliberately out of lexical order.
        for name, content in [("1.png", b"A"), ("2.png", b"B"), ("10.png", b"C")]:
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(content)
        count = turntable._normalize_frame_sequence(d, 3)
        self.assertEqual(count, 3)
        # Numeric order: 1 -> 0000, 2 -> 0001, 10 -> 0002 (NOT lexical).
        for seq, expected in [("0000.png", b"A"), ("0001.png", b"B"),
                              ("0002.png", b"C")]:
            with open(os.path.join(d, seq), "rb") as fh:
                self.assertEqual(fh.read(), expected)

    def test_count_mismatch_raises(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "0.png"), "wb") as fh:
            fh.write(b"x")
        with self.assertRaises(turntable.TurntableError):
            turntable._normalize_frame_sequence(d, 5)

    def test_no_frames_raises(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(turntable.TurntableError):
            turntable._normalize_frame_sequence(d, 0)

    def test_render_missing_glb_raises(self):
        # GLB existence is checked before the Blender lookup, so this is offline.
        with self.assertRaises(turntable.TurntableError):
            turntable.render("/no/such/model.glb", tempfile.mkdtemp())

    @unittest.skipUnless(shutil.which("blender"), "needs Blender on PATH")
    def test_render_refuses_dir_with_existing_pngs(self):
        glb = tempfile.NamedTemporaryFile(suffix=".glb", delete=False)
        glb.write(b"fake-glb")
        glb.close()
        out = tempfile.mkdtemp()
        with open(os.path.join(out, "mine.png"), "wb") as fh:
            fh.write(b"important")
        try:
            with self.assertRaises(turntable.TurntableError):
                turntable.render(glb.name, out)
            # The caller's PNG must be left untouched.
            self.assertTrue(os.path.isfile(os.path.join(out, "mine.png")))
        finally:
            os.unlink(glb.name)


class TestBoTTube(unittest.TestCase):
    def test_base_url_default_and_strip(self):
        old = os.environ.pop("BOTTUBE_BASE_URL", None)
        try:
            self.assertEqual(bottube._base_url(), "https://bottube.ai")
            os.environ["BOTTUBE_BASE_URL"] = "https://example.com/"
            self.assertEqual(bottube._base_url(), "https://example.com")
        finally:
            os.environ.pop("BOTTUBE_BASE_URL", None)
            if old is not None:
                os.environ["BOTTUBE_BASE_URL"] = old

    def test_upload_missing_file(self):
        with self.assertRaises(bottube.BoTTubeError):
            bottube.upload("/tmp/does-not-exist.mp4", "Title")

    def test_upload_requires_title(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with self.assertRaises(bottube.BoTTubeError):
                bottube.upload(fh.name, "")

    def test_full_watch_url_from_relative(self):
        body = bottube._add_full_watch_url(
            {"watch_url": "/watch/abc"}, "https://bottube.ai")
        self.assertEqual(body["watch_url_full"], "https://bottube.ai/watch/abc")

    def test_full_watch_url_leaves_absolute_alone(self):
        body = bottube._add_full_watch_url(
            {"watch_url": "https://x/watch/abc"}, "https://bottube.ai")
        self.assertNotIn("watch_url_full", body)


def _streaming_resp(chunks, url="https://cdn.example.com/m.glb"):
    """A mock requests response usable as a streaming context manager."""
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.url = url
    m.raise_for_status.return_value = None
    m.iter_content.return_value = iter(chunks)
    return m


class TestMeshyDownloadMocked(unittest.TestCase):
    def test_stream_download_writes_file(self):
        out = os.path.join(tempfile.mkdtemp(), "m.glb")
        with mock.patch("meshy_bottube.meshy.requests.get",
                        return_value=_streaming_resp([b"abc", b"def"])):
            path = meshy.download_glb({"model_urls": {"glb": "https://x/m.glb"}}, out)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"abcdef")

    def test_stream_download_aborts_over_cap(self):
        out = os.path.join(tempfile.mkdtemp(), "big.glb")
        chunks = [b"12345", b"67890", b"OVER"]
        with mock.patch.object(meshy, "_MAX_GLB_BYTES", 8):
            with mock.patch("meshy_bottube.meshy.requests.get",
                            return_value=_streaming_resp(chunks)):
                with self.assertRaises(meshy.MeshyError):
                    meshy.download_glb({"model_urls": {"glb": "https://x"}}, out)
        self.assertFalse(os.path.exists(out))  # partial file cleaned up


class TestWaitForTaskMocked(unittest.TestCase):
    def test_succeeds_after_polling(self):
        seq = [
            {"status": "IN_PROGRESS", "progress": 40},
            {"status": "SUCCEEDED", "progress": 100, "model_urls": {"glb": "u"}},
        ]
        with mock.patch("meshy_bottube.meshy.get_task", side_effect=seq), \
                mock.patch("meshy_bottube.meshy.time.sleep"):
            status = meshy.wait_for_task("t", poll_interval=0, timeout=30)
        self.assertEqual(status["status"], "SUCCEEDED")

    def test_failed_raises(self):
        with mock.patch("meshy_bottube.meshy.get_task",
                        return_value={"status": "FAILED", "message": "boom"}):
            with self.assertRaises(meshy.MeshyError):
                meshy.wait_for_task("t", poll_interval=0, timeout=30)

    def test_tolerates_transient_poll_failures(self):
        # Two network blips then success — must recover, not abandon the wait.
        seq = [meshy.MeshyError("blip"), meshy.MeshyError("blip2"),
               {"status": "SUCCEEDED", "progress": 100}]
        with mock.patch("meshy_bottube.meshy.get_task", side_effect=seq), \
                mock.patch("meshy_bottube.meshy.time.sleep"):
            status = meshy.wait_for_task("t", poll_interval=0, timeout=30)
        self.assertEqual(status["status"], "SUCCEEDED")

    def test_gives_up_after_too_many_failures(self):
        with mock.patch("meshy_bottube.meshy.get_task",
                        side_effect=meshy.MeshyError("network down")), \
                mock.patch("meshy_bottube.meshy.time.sleep"):
            with self.assertRaises(meshy.MeshyError):
                meshy.wait_for_task("t", poll_interval=0, timeout=30)

    def test_progress_callback_fires(self):
        seq = [{"status": "IN_PROGRESS", "progress": 10},
               {"status": "SUCCEEDED", "progress": 100}]
        calls = []
        with mock.patch("meshy_bottube.meshy.get_task", side_effect=seq), \
                mock.patch("meshy_bottube.meshy.time.sleep"):
            meshy.wait_for_task("t", poll_interval=0, timeout=30, stage="preview",
                                on_progress=lambda s, st, p: calls.append((s, st, p)))
        self.assertEqual(calls[0][0], "preview")
        self.assertTrue(any(c[1] == "SUCCEEDED" for c in calls))


class TestUploadMocked(unittest.TestCase):
    def setUp(self):
        # Snapshot and restore env so a test never mutates the caller's shell.
        self._env = {k: os.environ.get(k)
                     for k in ("BOTTUBE_API_KEY", "BOTTUBE_BASE_URL")}

        def _restore():
            for k, v in self._env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore)

    def test_upload_promotes_relative_watch_url(self):
        os.environ["BOTTUBE_API_KEY"] = "k"
        os.environ.pop("BOTTUBE_BASE_URL", None)
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"watch_url": "/watch/abc", "video_id": "v1"}
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with mock.patch("meshy_bottube.bottube.requests.post",
                            return_value=resp):
                body = bottube.upload(fh.name, "Title")
        self.assertEqual(body["watch_url_full"], "https://bottube.ai/watch/abc")
        self.assertEqual(body["video_id"], "v1")

    def test_upload_rejects_non_video_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as fh:
            with self.assertRaises(bottube.BoTTubeError):
                bottube.upload(fh.name, "Title")

    def test_upload_4xx_raises(self):
        os.environ["BOTTUBE_API_KEY"] = "k"
        os.environ.pop("BOTTUBE_BASE_URL", None)
        resp = mock.MagicMock(status_code=403, text="forbidden")
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with mock.patch("meshy_bottube.bottube.requests.post",
                            return_value=resp):
                with self.assertRaises(bottube.BoTTubeError):
                    bottube.upload(fh.name, "Title")

    def test_upload_missing_ids_raises(self):
        os.environ["BOTTUBE_API_KEY"] = "k"
        os.environ.pop("BOTTUBE_BASE_URL", None)
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"status": "queued"}  # no video_id/watch_url
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with mock.patch("meshy_bottube.bottube.requests.post",
                            return_value=resp):
                with self.assertRaises(bottube.BoTTubeError):
                    bottube.upload(fh.name, "Title")

    def test_upload_empty_body_returns_unconfirmed(self):
        os.environ["BOTTUBE_API_KEY"] = "k"
        os.environ.pop("BOTTUBE_BASE_URL", None)
        resp = mock.MagicMock(status_code=200, text="")
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with mock.patch("meshy_bottube.bottube.requests.post",
                            return_value=resp):
                body = bottube.upload(fh.name, "Title")
        self.assertTrue(body["ok"])
        self.assertTrue(body["unconfirmed"])

    def test_upload_readtimeout_returns_unconfirmed(self):
        os.environ["BOTTUBE_API_KEY"] = "k"
        os.environ.pop("BOTTUBE_BASE_URL", None)
        import requests
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with mock.patch("meshy_bottube.bottube.requests.post",
                            side_effect=requests.ReadTimeout("read timed out")):
                body = bottube.upload(fh.name, "Title")
        self.assertTrue(body["ok"])
        self.assertTrue(body["unconfirmed"])

    def test_upload_connecttimeout_still_raises(self):
        # A connect timeout = nothing was uploaded -> hard failure, not "ok".
        os.environ["BOTTUBE_API_KEY"] = "k"
        os.environ.pop("BOTTUBE_BASE_URL", None)
        import requests
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with mock.patch("meshy_bottube.bottube.requests.post",
                            side_effect=requests.ConnectTimeout("no route")):
                with self.assertRaises(bottube.BoTTubeError):
                    bottube.upload(fh.name, "Title")

    def test_upload_passes_category_in_form(self):
        os.environ["BOTTUBE_API_KEY"] = "k"
        os.environ.pop("BOTTUBE_BASE_URL", None)
        resp = mock.MagicMock(status_code=200,
                              text='{"video_id":"v","watch_url":"/watch/v"}')
        resp.json.return_value = {"video_id": "v", "watch_url": "/watch/v"}
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with mock.patch("meshy_bottube.bottube.requests.post",
                            return_value=resp) as post:
                bottube.upload(fh.name, "Title", category="comedy")
        self.assertEqual(post.call_args.kwargs["data"].get("category"), "comedy")

    def test_upload_redirect_rejected(self):
        os.environ["BOTTUBE_API_KEY"] = "k"
        os.environ.pop("BOTTUBE_BASE_URL", None)
        resp = mock.MagicMock(status_code=302)
        resp.headers = {"Location": "https://evil.example.com/steal"}
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with mock.patch("meshy_bottube.bottube.requests.post",
                            return_value=resp):
                with self.assertRaises(bottube.BoTTubeError):
                    bottube.upload(fh.name, "Title")


class TestBaseUrlSecurity(unittest.TestCase):
    def test_http_remote_rejected(self):
        old = os.environ.get("BOTTUBE_BASE_URL")
        os.environ["BOTTUBE_BASE_URL"] = "http://evil.example.com"
        try:
            with self.assertRaises(bottube.BoTTubeError):
                bottube._base_url()
        finally:
            if old is None:
                os.environ.pop("BOTTUBE_BASE_URL", None)
            else:
                os.environ["BOTTUBE_BASE_URL"] = old

    def test_http_localhost_allowed(self):
        old = os.environ.get("BOTTUBE_BASE_URL")
        os.environ["BOTTUBE_BASE_URL"] = "http://localhost:8000"
        try:
            self.assertEqual(bottube._base_url(), "http://localhost:8000")
        finally:
            if old is None:
                os.environ.pop("BOTTUBE_BASE_URL", None)
            else:
                os.environ["BOTTUBE_BASE_URL"] = old

    def _assert_base_url_rejected(self, value):
        old = os.environ.get("BOTTUBE_BASE_URL")
        os.environ["BOTTUBE_BASE_URL"] = value
        try:
            with self.assertRaises(bottube.BoTTubeError):
                bottube._base_url()
        finally:
            if old is None:
                os.environ.pop("BOTTUBE_BASE_URL", None)
            else:
                os.environ["BOTTUBE_BASE_URL"] = old

    def test_base_url_without_host_rejected(self):
        self._assert_base_url_rejected("https:///api")

    def test_base_url_with_path_rejected(self):
        self._assert_base_url_rejected("https://bottube.ai/extra/path")


class TestOneShotComposition(unittest.TestCase):
    """Mocked end-to-end coverage of the flagship meshy_to_bottube tool."""

    def _patches(self, **overrides):
        from meshy_bottube import server
        defaults = {
            "generate": {"glb_path": "/tmp/x/model.glb"},
            "render": {"frames_dir": "/tmp/x/frames", "frame_count": 180},
            "frames_to_video": "/tmp/x/raw.mp4",
            "prepare": {"output_path": "/tmp/x/ready.mp4", "oversize": False},
            "upload": {"watch_url": "/watch/abc",
                       "watch_url_full": "https://bottube.ai/watch/abc",
                       "video_id": "v1"},
        }
        return server, defaults, overrides

    def test_success_path(self):
        server, d, _ = self._patches()
        with mock.patch.object(server, "_preflight"), \
                mock.patch.object(server, "_workdir", return_value="/tmp/x"), \
                mock.patch.object(meshy, "generate", return_value=d["generate"]), \
                mock.patch.object(turntable, "render", return_value=d["render"]), \
                mock.patch.object(video, "frames_to_video",
                                  return_value=d["frames_to_video"]), \
                mock.patch.object(video, "prepare", return_value=d["prepare"]), \
                mock.patch.object(bottube, "upload", return_value=d["upload"]):
            res = server.meshy_to_bottube("a dragon", "Title")
        self.assertTrue(res["ok"])
        self.assertEqual(res["watch_url_full"], "https://bottube.ai/watch/abc")
        self.assertEqual(res["frame_count"], 180)
        self.assertEqual(res["video_path"], "/tmp/x/ready.mp4")

    def test_failure_preserves_partial_state(self):
        server, d, _ = self._patches()
        with mock.patch.object(server, "_preflight"), \
                mock.patch.object(server, "_workdir", return_value="/tmp/x"), \
                mock.patch.object(meshy, "generate", return_value=d["generate"]), \
                mock.patch.object(turntable, "render",
                                  side_effect=turntable.TurntableError("boom")):
            res = server.meshy_to_bottube("a dragon", "Title")
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_stage"], "turntable")
        self.assertEqual(res["glb_path"], "/tmp/x/model.glb")  # partial kept
        self.assertIn("error", res)

    def test_bad_frames_fails_before_billed_meshy(self):
        server, _, _ = self._patches()
        billed = {"called": False}

        def _gen(*a, **k):
            billed["called"] = True
            return {"glb_path": "x"}

        with mock.patch.object(server, "_preflight"), \
                mock.patch.object(meshy, "generate", side_effect=_gen):
            res = server.meshy_to_bottube("a dragon", "Title", frames=0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_stage"], "validate")
        self.assertFalse(billed["called"])  # never reached Meshy


class TestGetStatusTool(unittest.TestCase):
    def test_download_on_success(self):
        from meshy_bottube import server
        status = {"status": "SUCCEEDED", "progress": 100,
                  "model_urls": {"glb": "https://x/m.glb"}}
        with mock.patch.object(meshy, "get_task", return_value=status), \
                mock.patch.object(meshy, "download_glb",
                                  return_value="/tmp/x/model.glb"), \
                mock.patch.object(server, "_workdir", return_value="/tmp/x"):
            res = server.get_meshy_task_status("0193-abc", download=True)
        self.assertEqual(res["status"], "SUCCEEDED")
        self.assertEqual(res["glb_path"], "/tmp/x/model.glb")

    def test_no_download_when_pending(self):
        from meshy_bottube import server
        with mock.patch.object(meshy, "get_task",
                               return_value={"status": "IN_PROGRESS",
                                             "progress": 30}):
            res = server.get_meshy_task_status("0193-abc", download=True)
        self.assertNotIn("glb_path", res)


class TestServerHelpers(unittest.TestCase):
    def test_preflight_missing_key_raises(self):
        from meshy_bottube import server
        old = os.environ.pop("MESHY_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                server._preflight(need_meshy=True)
        finally:
            if old is not None:
                os.environ["MESHY_API_KEY"] = old

    def test_preflight_passes_when_nothing_required(self):
        from meshy_bottube import server
        server._preflight()  # no requirements → no raise


if __name__ == "__main__":
    unittest.main()
