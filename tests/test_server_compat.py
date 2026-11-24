"""Backwards compatibility, catalog completeness, and secret-leakage canary tests."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("rodin_entrypoint", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EntrypointCompatibilityTests(unittest.TestCase):
    def test_entrypoint_exports_25_clean_tools_and_no_browser_modules(self):
        mod = _load_entrypoint()
        self.assertEqual(mod.SERVER_NAME, "Rodin-Business")
        self.assertEqual(mod.VERSION, "3.0.0")

        caps = mod.rodin_capabilities()
        self.assertEqual(len(caps["rest_tools"]), 17)
        self.assertEqual(len(caps["oauth_tools"]), 8)
        self.assertEqual(len(caps["tools"]), 25)
        self.assertFalse(caps["browser_required"])

        for tool_name in caps["tools"]:
            self.assertTrue(callable(getattr(mod, tool_name, None)), f"Missing export: {tool_name}")

        # Legacy browser/scraping helpers must remain absent
        for legacy in ("web_images", "omnicraft", "rodin_image_connect", "rodin_hdri_generate"):
            self.assertFalse(hasattr(mod, legacy))

    def test_secret_canary_never_leaks_in_capabilities_or_errors(self):
        canary = "CANARY-SECRET-API-KEY-XYZ"
        with patch.dict(os.environ, {"RODIN_API_KEY": canary}):
            mod = _load_entrypoint()
            caps = mod.rodin_capabilities()
            self.assertTrue(caps["auth_status"]["rest_api_key_configured"])
            self.assertNotIn(canary, json.dumps(caps))

            preview = mod.rodin_generate(prompt="Test canary")
            self.assertNotIn(canary, json.dumps(preview))


if __name__ == "__main__":
    unittest.main()
