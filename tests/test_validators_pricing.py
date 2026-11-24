"""Unit tests for pricing guards, OpenAPI validators, rigging hierarchy, and SSRF/security."""

from __future__ import annotations

from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rodin_business_mcp.pricing import (
    estimate_gen25_credits,
    estimate_operation_credits,
    verify_spend_budget,
)
from rodin_business_mcp.security import safe_filename_component, validate_safe_https_url
from rodin_business_mcp.validators import (
    detect_image_extension,
    parse_bbox_condition,
    parse_image_labels,
    parse_image_size,
    parse_prompt,
    parse_rig_hierarchy,
    parse_seed,
    parse_uuid,
)


class PricingTests(unittest.TestCase):
    def test_gen25_estimates_cover_all_surcharges(self):
        self.assertEqual(estimate_gen25_credits("Gen-2.5-Low", "low"), 0.5)
        self.assertEqual(estimate_gen25_credits("Gen-2.5-High", "high"), 0.5)
        self.assertEqual(estimate_gen25_credits("Gen-2.5-Extreme-High", "high"), 1.0)
        self.assertEqual(estimate_gen25_credits("Gen-2.5-High", "extreme-high"), 2.5)
        self.assertEqual(estimate_gen25_credits("Gen-2.5-Extreme-High", "extreme-high"), 3.0)

    def test_operation_estimates_cover_all_operations(self):
        for op in ("bang", "rigging", "polygen", "images"):
            self.assertEqual(estimate_operation_credits(op), 0.5)
        self.assertEqual(estimate_operation_credits("texture", texture_mode="high"), 0.5)
        self.assertEqual(estimate_operation_credits("texture", texture_mode="extreme-high"), 2.0)
        self.assertEqual(estimate_operation_credits("agentic", agentic_tier="basic"), 0.5)
        self.assertEqual(estimate_operation_credits("agentic", agentic_tier="standard"), 1.0)
        self.assertEqual(estimate_operation_credits("agentic", agentic_tier="pro"), 2.0)

    def test_invalid_operations_and_budgets_raise(self):
        with self.assertRaises(ValueError):
            estimate_operation_credits("legacy")
        for bad_budget in (0, -1, 0.49, float("nan"), float("inf"), float("-inf"), True):
            with self.subTest(bad_budget=bad_budget), self.assertRaises(ValueError):
                verify_spend_budget(0.5, bad_budget)
        verify_spend_budget(0.5, 0.5)


class ValidatorTests(unittest.TestCase):
    def test_uuid_and_prompt_boundaries(self):
        valid = "123e4567-e89b-12d3-a456-426614174000"
        self.assertEqual(parse_uuid(f" {valid} "), valid)
        for bad in ("", "   ", "not-a-uuid", 123):
            with self.assertRaises(ValueError):
                parse_uuid(bad)  # type: ignore[arg-type]

        self.assertEqual(parse_prompt("x" * 1024), "x" * 1024)
        with self.assertRaises(ValueError):
            parse_prompt("x" * 1025)
        with self.assertRaises(ValueError):
            parse_prompt("bad\x00prompt")

    def test_seed_bbox_image_size_and_labels_boundaries(self):
        self.assertEqual(parse_seed(0), 0)
        self.assertEqual(parse_seed(65535), 65535)
        for bad in (-1, 65536, True, 1.5):
            with self.assertRaises(ValueError):
                parse_seed(bad)  # type: ignore[arg-type]

        self.assertEqual(parse_bbox_condition([1, 100, 2048]), [1, 100, 2048])
        for bad in ([0, 10, 10], [1, 2049, 10], [10, 10], [True, 10, 10]):
            with self.assertRaises(ValueError):
                parse_bbox_condition(bad)  # type: ignore[arg-type]

        self.assertEqual(parse_image_size("512x512"), "512x512")
        self.assertEqual(parse_image_size("2048x2048"), "2048x2048")
        for bad in ("511x512", "2049x2048", "0x1024", "1024*1024", "abc"):
            with self.assertRaises(ValueError):
                parse_image_size(bad)

        self.assertEqual(parse_image_labels(["F", "B"], 2), ["F", "B"])
        with self.assertRaises(ValueError):
            parse_image_labels(["F", "B"], 1)
        with self.assertRaises(ValueError):
            parse_image_labels(["FRONT"], 1)

    def test_rig_hierarchy_validates_nodes_cycles_transforms_and_length(self):
        identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        valid = [
            {"name": "Hips", "parent": -1, "transform": identity, "length": 0.4},
            {"name": "Spine", "parent": 0, "transform": [[1, 0, 0, 0]] * 4},
        ]
        parsed = parse_rig_hierarchy(valid)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["length"], 0.4)

        # Max 256 nodes succeeds; 257 fails
        max_chain = [{"name": f"b{i}", "parent": i - 1} for i in range(256)]
        self.assertEqual(len(parse_rig_hierarchy(max_chain)), 256)
        with self.assertRaises(ValueError):
            parse_rig_hierarchy([{"name": f"b{i}", "parent": i - 1} for i in range(257)])

        bad_cases = [
            [],
            [{"name": "", "parent": -1}],
            [{"name": "Hips", "parent": -1}, {"name": "Hips", "parent": 0}],
            [{"name": "Root", "parent": 0}],
            [{"name": "A", "parent": 1}, {"name": "B", "parent": 0}],
            [{"name": "A", "parent": 5}],
            [{"name": "A", "parent": -1, "length": 0}],
            [{"name": "A", "parent": -1, "length": -1.0}],
            [{"name": "A", "parent": -1, "length": None}],
            [{"name": "A", "parent": -1, "length": float("nan")}],
            [{"name": "A", "parent": -1, "transform": [1, 2, 3]}],
            [{"name": "A", "parent": -1, "transform": [float("inf")] * 16}],
        ]
        for case in bad_cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                parse_rig_hierarchy(case)

    def test_detect_image_extension_magic_bytes(self):
        self.assertEqual(detect_image_extension(b"\x89PNG\r\n\x1a\nrest"), ".png")
        self.assertEqual(detect_image_extension(b"\xff\xd8\xffrest"), ".jpg")
        self.assertEqual(detect_image_extension(b"RIFF1234WEBPrest"), ".webp")
        with self.assertRaises(RuntimeError):
            detect_image_extension(b"GIF89a")


class SecurityTests(unittest.TestCase):
    @staticmethod
    def _fake_resolver(ip: str):
        def _resolve(host: str, port: int):
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]
        return _resolve

    def test_ssrf_blocks_private_loopback_metadata_and_dns_rebinding(self):
        blocked_urls = [
            "http://file.hyper3d.com/model.glb",
            "https://user:pass@file.hyper3d.com/model.glb",
            "https://file.hyper3d.com:8443/model.glb",
            "https://localhost/model.glb",
            "https://sub.localhost/model.glb",
            "https://127.0.0.1/model.glb",
            "https://10.0.0.1/model.glb",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/model.glb",
            "https://[::ffff:127.0.0.1]/model.glb",
            "https://file.hyper3d.com/a\r\nb",
        ]
        for url in blocked_urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_safe_https_url(url, resolve_dns=False)

        # Hostname resolving via DNS to internal/metadata IP must be rejected
        for private_ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:10.0.0.1"):
            with self.subTest(private_ip=private_ip), self.assertRaises(ValueError):
                validate_safe_https_url(
                    "https://evil.example/model.glb",
                    resolve_dns=True,
                    resolver=self._fake_resolver(private_ip),
                )

        # Public DNS IP succeeds
        ok = validate_safe_https_url(
            "https://file.hyper3d.com/model.glb",
            resolve_dns=True,
            resolver=self._fake_resolver("93.184.216.34"),
        )
        self.assertEqual(ok, "https://file.hyper3d.com/model.glb")

    def test_safe_filename_component_strips_traversal(self):
        self.assertEqual(safe_filename_component("../../etc/passwd", fallback="f.bin"), "passwd")
        self.assertEqual(safe_filename_component("...", fallback="f.bin"), "f.bin")
        self.assertEqual(safe_filename_component("mesh final (1).glb", fallback="f.bin"), "mesh_final__1_.glb")


if __name__ == "__main__":
    unittest.main()
