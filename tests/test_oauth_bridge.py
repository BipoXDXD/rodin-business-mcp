"""Offline tests for the official OAuth 2.1 Device Flow, account wallet, and Streamable HTTP MCP bridge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rodin_business_mcp.app import create_server
from rodin_business_mcp.config import ApiKeyProvider, ServerConfig
from rodin_business_mcp.oauth_bridge import KeychainOrFileTokenStore

ASSET_UUID = "123e4567-e89b-12d3-a456-426614174000"


class MemoryTokenStore:
    def __init__(self, initial=None):
        self.data = dict(initial) if initial else None

    def load(self):
        return dict(self.data) if self.data else None

    def save(self, data):
        self.data = dict(data)

    def delete(self):
        existed = self.data is not None
        self.data = None
        return existed


class FakeHttpResponse:
    def __init__(self, payload=None, *, status_code=200, headers=None, text=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload


class OAuthBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "ref.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

        self.now_val = 1_000_000.0
        self.sleeps: list[float] = []
        self.opened_urls: list[str] = []
        self.store = MemoryTokenStore()
        self.http = Mock(side_effect=AssertionError("Unexpected OAuth HTTP call"))

        self.bundle = create_server(
            config=ServerConfig(),
            key_provider=ApiKeyProvider(),
            token_store=self.store,
            oauth_request_fn=self.http,
            sleep_fn=self._sleep,
            now_fn=lambda: self.now_val,
            open_browser_fn=lambda url: (self.opened_urls.append(url) or True),
            resolve_dns=False,
        )
        self.tools = self.bundle.tools

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_val += seconds

    def test_device_flow_two_step_login_and_logout(self):
        def transport(method, url, **kwargs):
            if url.endswith("/.well-known/oauth-protected-resource/api/mcp"):
                return FakeHttpResponse({
                    "resource": "https://api.hyper3d.com/api/mcp",
                    "authorization_servers": ["https://api.hyper3d.com/api/grant/oauth"],
                })
            if url.endswith("/.well-known/oauth-authorization-server"):
                return FakeHttpResponse({
                    "issuer": "https://api.hyper3d.com/api/grant/oauth",
                    "device_authorization_endpoint": "https://api.hyper3d.com/api/grant/oauth/device/code",
                    "token_endpoint": "https://api.hyper3d.com/api/grant/oauth/oauth2/token",
                })
            if url.endswith("/device/code"):
                return FakeHttpResponse({
                    "device_code": "dev-123",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://hyper3d.ai/workspace/oauth/device",
                    "verification_uri_complete": "https://hyper3d.ai/workspace/oauth/device?code=ABCD-1234",
                    "expires_in": 300,
                    "interval": 5,
                })
            if url.endswith("/oauth2/token"):
                # First poll pending, second poll slow_down, third succeeds
                if len(self.sleeps) == 1:
                    return FakeHttpResponse({"error": "authorization_pending"}, status_code=400)
                if len(self.sleeps) == 2:
                    return FakeHttpResponse({"error": "slow_down"}, status_code=400)
                return FakeHttpResponse({
                    "access_token": "CANARY-ACCESS-TOKEN-777",
                    "refresh_token": "CANARY-REFRESH-TOKEN-888",
                    "expires_in": 3600,
                    "scope": "rodin:generate rodin:read account:read offline_access",
                })
            raise AssertionError(f"Unexpected URL: {url}")

        self.http.side_effect = transport

        step1 = self.tools["rodin_oauth_login"](open_browser=True, wait_for_approval=False)
        self.assertTrue(step1["pending"])
        self.assertEqual(step1["user_code"], "ABCD-1234")
        self.assertEqual(self.opened_urls, ["https://hyper3d.ai/workspace/oauth/device?code=ABCD-1234"])

        step2 = self.tools["rodin_oauth_login"](wait_for_approval=True, timeout_seconds=60)
        self.assertTrue(step2["authenticated"])
        self.assertEqual(self.sleeps, [5.0, 5.0, 10.0])

        status = self.tools["rodin_oauth_status"]()
        self.assertTrue(status["connected"])
        self.assertNotIn("CANARY-ACCESS-TOKEN-777", json.dumps(status))
        self.assertNotIn("CANARY-REFRESH-TOKEN-888", json.dumps(status))

        out = self.tools["rodin_oauth_logout"]()
        self.assertTrue(out["credentials_removed"])
        self.assertFalse(self.tools["rodin_oauth_status"]()["connected"])

    def test_account_info_sanitizes_pii_and_refreshes_expired_token(self):
        self.store.save({
            "client_id": "https://hyper3d.ai/oauth_cimd/cli.json",
            "token_endpoint": "https://api.hyper3d.com/api/grant/oauth/oauth2/token",
            "access_token": "OLD-EXPIRED-TOKEN",
            "refresh_token": "CANARY-REFRESH-999",
            "expires_at": self.now_val - 10,
        })

        def transport(method, url, **kwargs):
            if url.endswith("/oauth2/token"):
                self.assertEqual(kwargs["data"]["grant_type"], "refresh_token")
                return FakeHttpResponse({
                    "access_token": "FRESH-ACCESS-TOKEN",
                    "expires_in": 3600,
                })
            if url.endswith("/api/user/get_info"):
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer FRESH-ACCESS-TOKEN")
                return FakeHttpResponse({
                    "billing_workspace": {"type": "personal"},
                    "meta": {
                        "user_uuid": ASSET_UUID,
                        "username": "StudioUser",
                        "email": "private-email@example.com",
                        "avatar_url": "https://signed.example/avatar",
                        "balance": 250,
                        "frozen": 10,
                        "subscriptions": {
                            "highest_level": "Business",
                            "active_subscriptions": [{
                                "plan_level": "Business",
                                "plan_price": 120,
                                "plan_yearly": False,
                                "active": True,
                                "subscription_reference": "sub_SECRET_STRIPE_ID",
                                "start_time": "2026-10-26T00:00:00Z",
                                "end_time": "2026-11-26T00:00:00Z",
                                "wallet_balance": 2050,
                            }],
                        },
                        "features": ["full_api.access", "rodin_pack.use_high_resolution"],
                    },
                })
            raise AssertionError(f"Unexpected URL: {url}")

        self.http.side_effect = transport
        info = self.tools["rodin_account_info"]()
        self.assertEqual(info["wallet"]["balance_credits"], 25.0)
        self.assertEqual(info["wallet"]["subscription_balance_credits"], 205.0)
        self.assertEqual(info["wallet"]["total_credits"], 230.0)
        self.assertEqual(info["wallet"]["frozen_credits"], 1.0)
        self.assertEqual(info["highest_plan_level"], "Business")

        dumped = json.dumps(info)
        for forbidden in ("private-email@example.com", "sub_SECRET_STRIPE_ID", "FRESH-ACCESS-TOKEN", "CANARY-REFRESH-999"):
            self.assertNotIn(forbidden, dumped)

    def test_account_info_group_workspace_and_uuid_mismatch(self):
        self.store.save({
            "access_token": "TOKEN",
            "expires_at": self.now_val + 3600,
        })

        def transport(method, url, **kwargs):
            if url.endswith("/api/user/get_info"):
                return FakeHttpResponse({
                    "billing_workspace": {"type": "group", "group_uuid": "grp-1"},
                    "meta": {"user_uuid": ASSET_UUID, "username": "TeamUser", "subscriptions": {}},
                })
            if url.endswith("/api/group/group_info"):
                return FakeHttpResponse({
                    "balance": 5000,
                    "group_meta": {"uuid": "grp-1", "name": "Studio Team", "frozen": 20},
                })
            raise AssertionError(url)

        self.http.side_effect = transport
        info = self.tools["rodin_account_info"]()
        self.assertEqual(info["wallet"]["type"], "group")
        self.assertEqual(info["wallet"]["balance_credits"], 500.0)
        self.assertEqual(info["wallet"]["frozen_credits"], 2.0)

    def test_mcp_streamable_http_sse_uploads_and_result_page(self):
        self.store.save({
            "access_token": "CANARY-OAUTH-BEARER",
            "expires_at": self.now_val + 3600,
        })
        put_headers_seen = []

        def transport(method, url, **kwargs):
            if method == "PUT" and url == "https://storage.hyper3d.com/upload/slot1":
                put_headers_seen.append(kwargs["headers"])
                return FakeHttpResponse(status_code=200)
            body = kwargs["json"]
            rpc_method = body["method"]
            if rpc_method == "initialize":
                return FakeHttpResponse(
                    {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}},
                    headers={"Content-Type": "application/json", "Mcp-Session-Id": "sess-42"},
                )
            if rpc_method == "notifications/initialized":
                self.assertEqual(kwargs["headers"]["Mcp-Session-Id"], "sess-42")
                return FakeHttpResponse(status_code=202)
            if rpc_method == "tools/call":
                name = body["params"]["name"]
                if name == "rodin_create_uploads":
                    sse = 'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"uploads":[{"upload_id":"up-1","method":"PUT","upload_url":"https://storage.hyper3d.com/upload/slot1","headers":{"Content-Type":"image/png"}}]}}}'
                    return FakeHttpResponse(headers={"Content-Type": "text/event-stream"}, text=sse)
                if name == "rodin_list_uploads":
                    return FakeHttpResponse({
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"structuredContent": {"files": [{"upload_id": "up-1"}], "total": 1}},
                    })
                if name == "rodin_delete_uploads":
                    return FakeHttpResponse({
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"structuredContent": {"deleted": body["params"]["arguments"]["upload_ids"]}},
                    })
                if name == "rodin_get_result":
                    return FakeHttpResponse({
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"structuredContent": {"display_url": "https://hyper3d.ai/workspace/r/abc", "files": []}},
                    })
            raise AssertionError(f"Unexpected call: {method} {url}")

        self.http.side_effect = transport

        created = self.tools["rodin_uploads_create"](file_paths=[str(self.image)], kind="image")
        self.assertEqual(created["upload_ids"], ["up-1"])
        # Crucial security check: OAuth Bearer must NEVER be forwarded to presigned upload URL!
        self.assertNotIn("Authorization", put_headers_seen[0])

        listed = self.tools["rodin_uploads_list"](kind="image", page=1)
        self.assertEqual(listed["total"], 1)

        deleted = self.tools["rodin_uploads_delete"](upload_ids=["up-1"])
        self.assertEqual(deleted["deleted"], ["up-1"])

        page = self.tools["rodin_result_page"](ASSET_UUID)
        self.assertEqual(page["display_url"], "https://hyper3d.ai/workspace/r/abc")

    def test_file_fallback_token_store_uses_0600_permissions(self):
        token_file = self.root / "cfg" / "oauth.json"
        store = KeychainOrFileTokenStore(fallback_path=token_file)
        with unittest.mock.patch.object(KeychainOrFileTokenStore, "_has_keychain", return_value=False):
            self.assertIsNone(store.load())
            store.save({"access_token": "tok"})
            mode = stat.S_IMODE(token_file.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(store.load(), {"access_token": "tok"})
            self.assertTrue(store.delete())
            self.assertIsNone(store.load())


if __name__ == "__main__":
    unittest.main()
