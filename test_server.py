"""Offline contract tests: HTTP is replaced at the transport boundary; no paid API calls."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("rodin_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
ASSET = "123e4567-e89b-12d3-a456-426614174000"


class RodinContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.image = self.root / "reference.png"
        self.image.write_bytes(PNG)
        self.model = self.root / "model.glb"
        self.model.write_bytes(b"glTF fixture")
        self.animation = self.root / "walk.fbx"
        self.animation.write_bytes(b"FBX fixture")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"RODIN_OUTPUT_DIR": str(self.root / "output")}).start()
        patch.object(server, "_key", return_value="test-key-not-a-credential").start()
        self.http = patch.object(server.requests, "request", side_effect=AssertionError("Unexpected HTTP call")).start()

    def tool(self, name):
        fn = getattr(server, name, None)
        self.assertTrue(callable(fn), f"Missing tool: {name}")
        return fn

    def response(self, payload, capture=None):
        def transport(method, url, **kwargs):
            self.assertEqual(method, "POST")
            if capture:
                capture(url, kwargs)
            response = Mock()
            response.json.return_value = payload
            return response
        self.http.side_effect = transport

    def test_new_tools_default_to_preview_without_network_or_files(self):
        for name, args in [("rodin_rigging", {"asset_id": ASSET, "bone_template": "human"}),
                           ("rodin_images", {"prompt": "An owl"}),
                           ("rodin_images", {"prompt": "Change background", "image_paths": [str(self.image)]})]:
            with self.subTest(name=name, args=args):
                result = self.tool(name)(**args)
                self.assertFalse(result["submitted"])
                self.assertEqual(result["estimated_credits"], 0.5)
        self.http.assert_not_called()
        self.assertFalse((self.root / "output").exists())

    def test_new_tools_reject_insufficient_or_nonfinite_budget_without_http(self):
        for name, args in [("rodin_rigging", {"asset_id": ASSET}), ("rodin_images", {"prompt": "An owl"})]:
            for budget in [0, 0.49, float("nan"), float("inf")]:
                with self.subTest(name=name, budget=budget), self.assertRaises(ValueError):
                    self.tool(name)(**args, approved_spend=True, max_credits=budget)
        self.http.assert_not_called()

    def test_image_generation_sends_json_and_saves_decoded_image(self):
        def capture(url, kwargs):
            self.assertEqual(url, "https://api.hyper3d.com/api/v2/images")
            self.assertEqual(kwargs["json"], {"prompt": "An owl", "size": "1024x1024"})
            self.assertNotIn("files", kwargs)
        self.response({"created": 1728547200, "consumed": 0.5,
                       "data": [{"b64_json": base64.b64encode(PNG).decode(), "revised_prompt": "A snowy owl"}]}, capture)
        result = self.tool("rodin_images")("An owl", size="1024x1024", approved_spend=True, max_credits=0.5)
        self.assertTrue(result["submitted"])
        self.assertEqual(result["actual_credits"], 0.5)
        self.assertEqual(Path(result["saved_files"][0]["path"]).read_bytes(), PNG)
        self.assertEqual(result["saved_files"][0]["revised_prompt"], "A snowy owl")
        self.assertNotIn("b64_json", json.dumps(result))

    def test_image_edits_use_repeated_multipart_images_and_do_not_overwrite(self):
        def capture(url, kwargs):
            self.assertEqual(url, "https://api.hyper3d.com/api/v2/images")
            self.assertNotIn("json", kwargs)
            parts = kwargs["files"]
            fields = {k: v[1] for k, v in parts if v[0] is None}
            self.assertEqual(fields, {"prompt": "Add moon"})
            images = [v for k, v in parts if k == "images"]
            self.assertEqual(len(images), 2)
            self.assertEqual(images[0][1].read(), PNG)
        self.response({"created": 1728547200, "consumed": 0.5,
                       "data": [{"b64_json": base64.b64encode(PNG).decode()}]}, capture)
        paths = []
        for _ in range(2):
            r = self.tool("rodin_images")("Add moon", image_paths=[str(self.image)] * 2,
                                         approved_spend=True, max_credits=0.5)
            paths.append(r["saved_files"][0]["path"])
        self.assertNotEqual(*paths)
        self.assertTrue(all(Path(p).is_file() for p in paths))

    def test_images_reject_invalid_inputs_before_network(self):
        bad = self.root / "bad.gif"
        bad.write_bytes(b"GIF89a")
        large = self.root / "large.png"
        with large.open("wb") as out:
            out.truncate(10 * 1024 * 1024 + 1)
        cases = [{"prompt": " "}, {"prompt": "owl", "size": "10x10"},
                 {"prompt": "owl", "size": "4096x4096"}, {"prompt": "owl", "size": "x"},
                 {"prompt": "owl", "image_paths": [str(self.image)] * 4},
                 {"prompt": "owl", "image_paths": [str(bad)]},
                 {"prompt": "owl", "image_paths": [str(large)]}]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tool("rodin_images")(**args)
        self.http.assert_not_called()

    def test_images_handle_application_errors_and_malformed_data_without_output(self):
        for payload in [{"error": "API_INSUFFICIENT_FUNDS", "message": "Insufficient balance"},
                        {"data": []}, {"data": [{"b64_json": "INVALID!!"}]},
                        {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}]:
            with self.subTest(payload=payload), self.assertRaises((RuntimeError, ValueError)):
                self.response(payload)
                self.tool("rodin_images")("Owl", approved_spend=True, max_credits=0.5)
        self.assertFalse(list((self.root / "output").glob("*")))

    def test_rigging_serializes_asset_and_stock_animations(self):
        def capture(url, kwargs):
            self.assertEqual(url, "https://api.hyper3d.com/api/v2/rigging")
            fields = [(k, v[1]) for k, v in kwargs["files"]]
            self.assertIn(("asset_id", ASSET), fields)
            self.assertIn(("bone_template", "human"), fields)
            self.assertIn(("skinning_method", "core"), fields)
            self.assertEqual([v for k, v in fields if k == "animation_preset"], ["human/Walking", "human/Flair"])
        self.response({"uuid": ASSET, "consumed": 0.5, "jobs": {"subscription_key": "test-subscription"}}, capture)
        r = self.tool("rodin_rigging")(asset_id=ASSET, bone_template="human", skinning_method="core",
                                       animation_presets=["human/Walking", "human/Flair"],
                                       approved_spend=True, max_credits=0.5)
        self.assertTrue(r["submitted"])
        self.assertEqual(r["result"]["jobs"]["subscription_key"], "test-subscription")

    def test_rigging_uploads_custom_hierarchy_and_animation_files(self):
        hierarchy = [{"name": "root", "parent": -1}, {"name": "spine", "parent": 0}]
        def capture(url, kwargs):
            parts = kwargs["files"]
            fields = {k: v[1] for k, v in parts if v[0] is None}
            self.assertEqual(json.loads(fields["hierarchy"]), hierarchy)
            self.assertEqual(fields["material"], "None")
            self.assertEqual([k for k, v in parts if v[0] is not None], ["model", "animation_files"])
            self.assertEqual(next(v for k, v in parts if k == "animation_files")[1].read(), b"FBX fixture")
        self.response({"uuid": ASSET, "consumed": 0.5, "jobs": {}}, capture)
        r = self.tool("rodin_rigging")(model_path=str(self.model), bone_template="custom", hierarchy=hierarchy,
                                       material="None", animation_paths=[str(self.animation)],
                                       approved_spend=True, max_credits=0.5)
        self.assertTrue(r["submitted"])

    def test_rigging_modify_uses_description_without_hierarchy(self):
        result = self.tool("rodin_rigging")(asset_id=ASSET, bone_template="human", rigging_mode="modify",
                                             skeleton_description="Full baseline skeleton; extend the arms")
        self.assertEqual(result["parameters"]["rigging_mode"], "modify")
        self.assertNotIn("hierarchy", result["parameters"])

    def test_rigging_rejects_conflicting_inputs_templates_and_animations(self):
        cases = [{}, {"asset_id": ASSET, "model_path": str(self.model)},
                 {"asset_id": "invalid-uuid"}, {"asset_id": ASSET, "bone_template": "custom"},
                 {"asset_id": ASSET, "bone_template": "human", "skeleton_description": "limbs"},
                 {"asset_id": ASSET, "rigging_mode": "modify"},
                 {"asset_id": ASSET, "bone_template": "auto", "rigging_mode": "modify", "skeleton_description": "limbs"},
                 {"asset_id": ASSET, "animation_presets": ["human/Walking"] * 11},
                 {"asset_id": ASSET, "animation_presets": ["human/Walking"] * 2},
                 {"asset_id": ASSET, "animation_presets": ["human/Walking"], "animation_paths": [str(self.animation)]},
                 {"model_path": str(self.model), "bone_template": "human", "material": "PBR"},
                 {"model_path": str(self.model), "bone_template": "auto", "material": "None"}]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tool("rodin_rigging")(**args)
        self.http.assert_not_called()

    def test_rigging_rejects_failed_or_unconfirmed_submissions(self):
        for payload in [{"error": "INVALID_REQUEST", "message": "Invalid skeleton"}, {"message": "Not accepted"}]:
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                self.response(payload)
                self.tool("rodin_rigging")(asset_id=ASSET, approved_spend=True, max_credits=0.5)

    def test_rigging_rejects_invalid_hierarchies_and_animation_uploads(self):
        for hierarchy in [[{"name": "root", "parent": 0}],
                          [{"name": "root", "parent": 1}, {"name": "spine", "parent": 0}],
                          [{"name": "root", "parent": 3}], [{"name": "", "parent": -1}]]:
            with self.subTest(hierarchy=hierarchy), self.assertRaises(ValueError):
                self.tool("rodin_rigging")(asset_id=ASSET, bone_template="custom", hierarchy=hierarchy)
        large = self.root / "large.fbx"
        with large.open("wb") as out:
            out.truncate(25 * 1024 * 1024 + 1)
        for paths in [[str(large)], [str(self.image)], [str(self.animation)] * 11]:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.tool("rodin_rigging")(asset_id=ASSET, animation_paths=paths)
        self.http.assert_not_called()

    def test_rigging_accepts_auto_image_and_text_only_custom_skeleton(self):
        auto = self.tool("rodin_rigging")(model_path=str(self.model), image_path=str(self.image), material="PBR")
        self.assertEqual([f["field"] for f in auto["files"]], ["model", "image"])
        custom = self.tool("rodin_rigging")(model_path=str(self.model), material="None", bone_template="custom",
                                           skeleton_description="Four legs and one tail")
        self.assertEqual(custom["parameters"]["skeleton_description"], "Four legs and one tail")
        self.http.assert_not_called()

    def test_images_save_jpeg_and_webp_with_matching_extensions(self):
        for content, suffix in [(b"\xff\xd8\xfffixture", ".jpg"), (b"RIFF0000WEBPfixture", ".webp")]:
            with self.subTest(suffix=suffix):
                self.response({"consumed": 0.5, "data": [{"b64_json": base64.b64encode(content).decode()}]})
                r = self.tool("rodin_images")("Owl", approved_spend=True, max_credits=0.5)
                path = Path(r["saved_files"][0]["path"])
                self.assertEqual(path.suffix, suffix)
                self.assertEqual(path.read_bytes(), content)

    def test_estimates_include_new_operations(self):
        for operation in ["rigging", "images"]:
            with self.subTest(operation=operation):
                self.assertEqual(server.rodin_estimate(operation=operation)["estimated_credits"], 0.5)
        self.http.assert_not_called()

    def test_existing_generation_still_previews_without_http(self):
        r = server.rodin_generate(prompt="An owl")
        self.assertFalse(r["submitted"])
        self.assertEqual(r["estimated_credits"], 0.5)
        self.http.assert_not_called()

    def get_response(self, payload, capture=None):
        def transport(method, url, **kwargs):
            self.assertEqual(method, "GET")
            if capture:
                capture(url, kwargs)
            response = Mock()
            response.json.return_value = payload
            return response
        self.http.side_effect = transport

    def test_polygen_previews_without_network(self):
        result = self.tool("rodin_polygen")(asset_id=ASSET)
        self.assertFalse(result["submitted"])
        self.assertEqual(result["endpoint"], "polygen")
        self.assertEqual(result["estimated_credits"], 0.5)
        self.http.assert_not_called()

    def test_polygen_serializes_asset_with_generated_textures(self):
        def capture(url, kwargs):
            self.assertEqual(url, "https://api.hyper3d.com/api/v2/polygen")
            fields = {k: v[1] for k, v in kwargs["files"]}
            self.assertEqual(fields["asset_id"], ASSET)
            self.assertEqual(fields["face_count"], "4000")
            self.assertEqual(fields["mesh_mode"], "NGon")
            self.assertEqual(fields["material"], "PBR")
            self.assertEqual(fields["uhd_texture"], "true")
            self.assertNotIn("transfer_texture", fields)
        self.response({"uuid": ASSET, "consumed": 0.5, "jobs": {"subscription_key": "key"}}, capture)
        r = self.tool("rodin_polygen")(asset_id=ASSET, face_count=4000, mesh_mode="NGon", material="PBR",
                                       uhd_texture=True, approved_spend=True, max_credits=0.5)
        self.assertTrue(r["submitted"])
        self.assertEqual(r["actual_credits"], 0.5)

    def test_polygen_uploads_model_with_reference_images_and_labels(self):
        def capture(url, kwargs):
            parts = kwargs["files"]
            fields = {k: v[1] for k, v in parts if v[0] is None}
            self.assertEqual(fields["transfer_texture"], "false")
            self.assertEqual(json.loads(fields["image_label"]), ["F", "B"])
            self.assertEqual([k for k, v in parts if v[0] is not None], ["model", "images", "images"])
        self.response({"uuid": ASSET, "consumed": 0.5, "jobs": {}}, capture)
        r = self.tool("rodin_polygen")(model_path=str(self.model), image_paths=[str(self.image)] * 2,
                                       image_labels=["F", "B"], transfer_texture=False,
                                       approved_spend=True, max_credits=0.5)
        self.assertTrue(r["submitted"])

    def test_polygen_accepts_text_only_input(self):
        r = self.tool("rodin_polygen")(prompt="A wooden toy robot")
        self.assertEqual(r["parameters"]["prompt"], "A wooden toy robot")
        self.assertEqual(r["files"], [])

    def test_polygen_rejects_invalid_inputs_before_network(self):
        cases = [{}, {"asset_id": ASSET, "model_path": str(self.model)}, {"asset_id": "not-a-uuid"},
                 {"asset_id": ASSET, "face_count": 199}, {"asset_id": ASSET, "face_count": 10001},
                 {"model_path": str(self.model), "material": "PBR"},
                 {"model_path": str(self.model), "prompt": "robot"},
                 {"asset_id": ASSET, "transfer_texture": False},
                 {"asset_id": ASSET, "image_labels": ["F"]},
                 {"prompt": "robot", "image_paths": [str(self.image)] * 6},
                 {"prompt": "x" * 1025}]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tool("rodin_polygen")(**args)
        self.http.assert_not_called()

    def test_polygen_rejects_failed_submission(self):
        self.response({"error": "ASSET_NOT_AVAILABLE", "message": "Not finished"})
        with self.assertRaises(RuntimeError):
            self.tool("rodin_polygen")(asset_id=ASSET, approved_spend=True, max_credits=0.5)

    def test_animation_catalog_filters_by_category(self):
        catalog = [{"animation_id": "canine_000002", "category": "canine"}]
        def capture(url, kwargs):
            self.assertEqual(url, "https://api.hyper3d.com/api/v2/rigging_animations")
            self.assertEqual(kwargs["params"], {"category": "canine"})
        self.get_response(catalog, capture)
        result = self.tool("rodin_animation_catalog")(category="canine")
        self.assertEqual(result["animations"], catalog)
        self.assertEqual(result["count"], 1)

    def test_animation_catalog_without_category_sends_no_filter(self):
        self.get_response([], lambda url, kwargs: self.assertIsNone(kwargs.get("params")))
        self.assertEqual(self.tool("rodin_animation_catalog")()["count"], 0)

    def test_animation_catalog_rejects_unknown_category_without_http(self):
        for category in ["Human", "dog", ""]:
            with self.subTest(category=category), self.assertRaises(ValueError):
                self.tool("rodin_animation_catalog")(category=category)
        self.http.assert_not_called()

    def test_rigging_accepts_new_templates_in_place_and_engine_skeletons(self):
        for template in ["bird", "dragon", "kaiju"]:
            with self.subTest(template=template):
                r = self.tool("rodin_rigging")(asset_id=ASSET, bone_template=template)
                self.assertEqual(r["parameters"]["bone_template"], template)
        r = self.tool("rodin_rigging")(asset_id=ASSET, bone_template="human", geometry_file_format="fbx",
                                       export_skeleton_type="ue5", animation_presets=["human_000069"],
                                       in_place=True)
        self.assertEqual(r["parameters"]["export_skeleton_type"], "ue5")
        self.assertTrue(r["parameters"]["in_place"])
        default = self.tool("rodin_rigging")(asset_id=ASSET, bone_template="human")
        self.assertNotIn("export_skeleton_type", default["parameters"])
        self.assertNotIn("in_place", default["parameters"])
        self.http.assert_not_called()

    def test_rigging_rejects_invalid_engine_skeleton_and_in_place_combinations(self):
        cases = [{"asset_id": ASSET, "export_skeleton_type": "unity"},
                 {"model_path": str(self.model), "material": "None", "bone_template": "human",
                  "geometry_file_format": "fbx", "export_skeleton_type": "unity"},
                 {"asset_id": ASSET, "bone_template": "quadruped", "geometry_file_format": "fbx",
                  "export_skeleton_type": "actorcore"},
                 {"asset_id": ASSET, "in_place": True, "animation_paths": [str(self.animation)]},
                 {"asset_id": ASSET, "geometry_file_format": "obj"}]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tool("rodin_rigging")(**args)
        self.http.assert_not_called()

    def test_old_generators_are_removed(self):
        for name in ("rodin_generate_gen2", "rodin_generate_legacy"):
            self.assertFalse(hasattr(server, name))
            self.assertNotIn(name, server.rodin_capabilities()["tools"])
        for operation in ("gen2", "legacy"):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                server.rodin_estimate(operation=operation)

    def test_estimate_includes_polygen(self):
        self.assertEqual(server.rodin_estimate(operation="polygen")["estimated_credits"], 0.5)

    def test_capabilities_use_new_server_name(self):
        caps = server.rodin_capabilities()
        self.assertEqual(caps["server"], "Rodin-Business")
        self.assertIn("rodin_polygen", caps["tools"])
        self.assertIn("rodin_animation_catalog", caps["tools"])

    def test_api_only_catalog_has_no_browser_tools(self):
        caps = server.rodin_capabilities()
        self.assertEqual(len(caps['tools']), 15)
        self.assertFalse(caps['browser_required'])
        self.assertFalse(hasattr(server, 'web_images'))
        self.assertFalse(hasattr(server, 'omnicraft'))
        for name in ('rodin_image_connect', 'rodin_image_to_image', 'rodin_hdri_generate', 'rodin_model_pack'):
            self.assertFalse(hasattr(server, name))


if __name__ == "__main__":
    unittest.main()
