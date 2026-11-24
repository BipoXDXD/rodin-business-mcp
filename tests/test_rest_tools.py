"""Offline contract tests for all 17 REST API v2 tools, retries, polling, and downloads."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rodin_business_mcp.app import create_server
from rodin_business_mcp.config import ApiKeyProvider, ServerConfig

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
ASSET_UUID = "123e4567-e89b-12d3-a456-426614174000"


class FakeResponse:
    def __init__(self, payload=None, *, status_code=201, headers=None, chunks=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RestToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "front.png"
        self.image.write_bytes(PNG)
        self.image2 = self.root / "back.png"
        self.image2.write_bytes(PNG)
        self.model = self.root / "model.glb"
        self.model.write_bytes(b"glTF fixture")
        self.gltf = self.root / "model.gltf"
        self.gltf.write_text("{}", encoding="utf-8")
        self.animation = self.root / "walk.fbx"
        self.animation.write_bytes(b"FBX fixture")

        self.clock = FakeClock()
        self.http = Mock(side_effect=AssertionError("Unexpected REST HTTP call"))
        self.dl_get = Mock(side_effect=AssertionError("Unexpected download GET call"))
        self.env_patch = patch.dict(
            os.environ,
            {
                "RODIN_API_KEY": "CANARY-RODIN-API-KEY-999",
                "RODIN_OUTPUT_DIR": str(self.root / "output"),
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.bundle = create_server(
            config=ServerConfig(),
            key_provider=ApiKeyProvider(),
            rest_request_fn=self.http,
            download_get_fn=self.dl_get,
            sleep_fn=self.clock.sleep,
            monotonic_fn=self.clock.monotonic,
            resolve_dns=False,
        )
        self.tools = self.bundle.tools

    def set_rest_response(self, payload, *, status_code=201, capture=None):
        def transport(method, url, **kwargs):
            if capture:
                capture(method, url, kwargs)
            return FakeResponse(payload, status_code=status_code)
        self.http.side_effect = transport

    def test_texture_sends_images_field_uhd_labels_hybrid_and_preview_render(self):
        def capture(method, url, kwargs):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://api.hyper3d.com/api/v2/rodin_texture_only")
            parts = kwargs["files"]
            file_fields = [k for k, v in parts if v[0] is not None]
            self.assertEqual(file_fields, ["images", "images", "model"])
            text_fields = [(k, v[1]) for k, v in parts if v[0] is None]
            field_map = {k: v for k, v in text_fields if k != "image_label"}
            self.assertEqual(field_map["material"], "Hybrid")
            self.assertEqual(field_map["uhd_texture"], "true")
            self.assertEqual(field_map["preview_render"], "true")
            self.assertEqual(field_map["resolution"], "High")
            self.assertEqual([v for k, v in text_fields if k == "image_label"], ["F", "B"])

        self.set_rest_response(
            {"uuid": ASSET_UUID, "consumed": 2.0, "jobs": {"subscription_key": "sub-tex"}},
            capture=capture,
        )
        res = self.tools["rodin_texture"](
            model_path=str(self.model),
            image_paths=[str(self.image), str(self.image2)],
            image_labels=["F", "B"],
            material="Hybrid",
            texture_mode="extreme-high",
            resolution="High",
            uhd_texture=True,
            preview_render=True,
            approved_spend=True,
            max_credits=2.0,
        )
        self.assertTrue(res["submitted"])
        self.assertEqual(res["actual_credits"], 2.0)

        # Backwards-compatible single image_path preview works without HTTP
        self.http.reset_mock()
        preview = self.tools["rodin_texture"](model_path=str(self.model), image_path=str(self.image))
        self.assertFalse(preview["submitted"])
        self.assertEqual(preview["files"][0]["field"], "images")
        self.http.assert_not_called()

    def test_rigging_matches_openapi_v3_contract(self):
        # 1) auto without image on uploaded model is allowed (clay preview rendered by API)
        preview = self.tools["rodin_rigging"](model_path=str(self.model), bone_template="auto")
        self.assertFalse(preview["submitted"])
        self.assertNotIn("rigging_mode", preview["parameters"])
        self.assertNotIn("material", preview["parameters"])

        # 2) custom hierarchy with transform & length serializes cleanly
        hierarchy = [
            {"name": "Hips", "parent": -1, "length": 0.5},
            {"name": "Spine", "parent": 0},
        ]
        def capture(method, url, kwargs):
            self.assertEqual(url, "https://api.hyper3d.com/api/v2/rigging")
            parts = kwargs["files"]
            fields = {k: v[1] for k, v in parts if v[0] is None}
            self.assertNotIn("rigging_mode", fields)
            self.assertNotIn("material", fields)
            self.assertEqual(json.loads(fields["hierarchy"]), hierarchy)
            self.assertEqual([k for k, v in parts if v[0] is not None], ["model", "animation_files"])

        self.set_rest_response({"uuid": ASSET_UUID, "consumed": 0.5, "jobs": {"subscription_key": "sub-rig"}}, capture=capture)
        res = self.tools["rodin_rigging"](
            model_path=str(self.model),
            bone_template="custom",
            hierarchy=hierarchy,
            animation_paths=[str(self.animation)],
            approved_spend=True,
            max_credits=0.5,
        )
        self.assertTrue(res["submitted"])

    def test_bang_asset_and_model_support_instruction_hybrid_and_preview_render(self):
        def capture(method, url, kwargs):
            fields = {k: v[1] for k, v in kwargs["files"] if v[0] is None}
            self.assertEqual(fields["asset_id"], ASSET_UUID)
            self.assertEqual(fields["instruction"], "Split helmet and visor")
            self.assertEqual(fields["material"], "Hybrid")
            self.assertEqual(fields["preview_render"], "true")

        self.set_rest_response({"uuid": ASSET_UUID, "consumed": 0.5, "jobs": {"subscription_key": "sub-bang"}}, capture=capture)
        res = self.tools["rodin_bang_asset"](
            asset_id=ASSET_UUID,
            instruction="Split helmet and visor",
            material="Hybrid",
            preview_render=True,
            approved_spend=True,
            max_credits=0.5,
        )
        self.assertTrue(res["submitted"])

        # .gltf must be rejected by rodin_bang_model before HTTP
        self.http.reset_mock()
        with self.assertRaises(ValueError):
            self.tools["rodin_bang_model"](model_path=str(self.gltf), material="None")
        self.http.assert_not_called()

    def test_agentic_supports_explicit_route(self):
        def capture(method, url, kwargs):
            self.assertEqual(url, "https://api.hyper3d.com/api/v2/rodin_agentic")
            fields = {k: v[1] for k, v in kwargs["files"] if v[0] is None}
            self.assertEqual(fields["route"], "n-gons")
            self.assertEqual(fields["tier"], "pro")

        self.set_rest_response(
            {"uuid": ASSET_UUID, "producer": "n-gons", "job": {"subscription_key": "sub-ag"}},
            capture=capture,
        )
        res = self.tools["rodin_agentic"](
            image_paths=[str(self.image)],
            tier="pro",
            route="n-gons",
            approved_spend=True,
            max_credits=2.0,
        )
        self.assertTrue(res["submitted"])
        recent = self.tools["rodin_recent_jobs"]()
        self.assertEqual(recent["count"], 1)
        self.assertEqual(recent["jobs"][0]["subscription_key"], "sub-ag")
        self.assertEqual(recent["jobs"][0]["producer"], "n-gons")

    def test_balance_supports_usage_page_and_retries_on_429(self):
        responses = iter([
            FakeResponse({"error": "RATE_LIMIT"}, status_code=429, headers={"Retry-After": "4"}),
            FakeResponse({"balance": 230, "usage": [{"task_uuid": ASSET_UUID, "amount": 0.5}]}, status_code=200),
        ])
        captured_params = []

        def transport(method, url, **kwargs):
            captured_params.append(kwargs.get("params"))
            return next(responses)

        self.http.side_effect = transport
        res = self.tools["rodin_balance"](usage_page=2)
        self.assertEqual(res["balance"], 230)
        self.assertEqual(len(res["usage"]), 1)
        self.assertEqual(captured_params, [{"usage_page": 2}, {"usage_page": 2}])
        self.assertEqual(self.clock.sleeps, [4.0])

        with self.assertRaises(ValueError):
            self.tools["rodin_balance"](usage_page=0)

    def test_paid_submission_never_retries_on_429_or_500(self):
        self.http.side_effect = lambda m, u, **kw: FakeResponse({"message": "busy"}, status_code=503)
        with self.assertRaises(RuntimeError):
            self.tools["rodin_generate"](prompt="A chair", approved_spend=True, max_credits=0.5)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_rodin_wait_polls_with_backoff_until_done_failed_or_timeout(self):
        # 1) Done after 3 polls
        seq = iter([
            FakeResponse({"jobs": [{"uuid": "j1", "status": "Waiting", "queue_length": 2}]}),
            FakeResponse({"jobs": [{"uuid": "j1", "status": "Generating"}]}),
            FakeResponse({"jobs": [{"uuid": "j1", "status": "Done"}]}),
        ])
        self.http.side_effect = lambda m, u, **kw: next(seq)
        res = self.tools["rodin_wait"]("sub-key", timeout_seconds=120, poll_interval_seconds=5, max_interval_seconds=30)
        self.assertTrue(res["done"])
        self.assertEqual(res["state"], "Done")
        self.assertEqual(res["polls"], 3)
        self.assertEqual(self.clock.sleeps, [5.0, 10.0])

        # 2) Stops immediately on Failed
        self.clock.sleeps.clear()
        self.http.side_effect = lambda m, u, **kw: FakeResponse({"jobs": [{"uuid": "j1", "status": "Failed"}]})
        failed = self.tools["rodin_wait"]("sub-key", timeout_seconds=60)
        self.assertTrue(failed["failed"])
        self.assertEqual(failed["polls"], 1)
        self.assertEqual(self.clock.sleeps, [])

        # 3) Timeout reports Pending without raising
        self.clock.sleeps.clear()
        self.http.side_effect = lambda m, u, **kw: FakeResponse({"jobs": [{"uuid": "j1", "status": "Generating"}]})
        timed_out = self.tools["rodin_wait"]("sub-key", timeout_seconds=12, poll_interval_seconds=5, max_interval_seconds=30)
        self.assertTrue(timed_out["timed_out"])
        self.assertEqual(timed_out["state"], "Pending")

    def test_save_results_downloads_atomically_and_blocks_redirects_and_ssrf(self):
        self.set_rest_response({
            "list": [{"name": "../../mesh.glb", "url": "https://file.hyper3d.com/out/mesh.glb"}]
        })
        self.dl_get.side_effect = lambda url, **kw: FakeResponse(status_code=200, chunks=[b"glb-bytes"])
        res = self.tools["rodin_save_results"](ASSET_UUID)
        saved_path = Path(res["saved_files"][0]["path"])
        self.assertTrue(saved_path.is_file())
        self.assertEqual(saved_path.read_bytes(), b"glb-bytes")
        self.assertEqual(saved_path.parent, (self.root / "output").resolve())

        # Redirect must be rejected
        self.dl_get.side_effect = lambda url, **kw: FakeResponse(status_code=302)
        with self.assertRaises(RuntimeError):
            self.tools["rodin_save_results"](ASSET_UUID)

        # Internal SSRF download URL rejected before GET
        self.dl_get.reset_mock()
        self.set_rest_response({"list": [{"name": "m.glb", "url": "https://127.0.0.1/secret"}]})
        with self.assertRaises(ValueError):
            self.tools["rodin_save_results"](ASSET_UUID)
        self.dl_get.assert_not_called()

    def test_images_sync_saves_png_and_rejects_bad_base64(self):
        self.set_rest_response({
            "created": 1728547200,
            "consumed": 0.5,
            "data": [{"b64_json": base64.b64encode(PNG).decode(), "revised_prompt": "Detailed owl"}],
        })
        res = self.tools["rodin_images"]("An owl", size="1024x1024", approved_spend=True, max_credits=0.5)
        self.assertTrue(res["submitted"])
        self.assertEqual(Path(res["saved_files"][0]["path"]).read_bytes(), PNG)
        self.assertNotIn("b64_json", json.dumps(res))

        self.set_rest_response({"consumed": 0.5, "data": [{"b64_json": "NOT-BASE64!!!"}]})
        with self.assertRaises(RuntimeError):
            self.tools["rodin_images"]("An owl", approved_spend=True, max_credits=0.5)

    def test_polygen_and_animation_catalog(self):
        self.set_rest_response([{"animation_id": "human_000017", "category": "human"}], status_code=200)
        cat = self.tools["rodin_animation_catalog"](category="human")
        self.assertEqual(cat["count"], 1)

        poly = self.tools["rodin_polygen"](asset_id=ASSET_UUID, face_count=4000, material="None")
        self.assertFalse(poly["submitted"])
        self.assertEqual(poly["parameters"]["material"], "None")


if __name__ == "__main__":
    unittest.main()
