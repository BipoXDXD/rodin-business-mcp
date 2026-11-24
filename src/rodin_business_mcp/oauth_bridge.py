"""Official Hyper3D OAuth 2.1 Device Flow + Streamable HTTP MCP client.

Connects to the documented official endpoints:
  - RFC 9728 / RFC 8414 discovery on https://api.hyper3d.com/.well-known/oauth-protected-resource/api/mcp
  - RFC 8628 Device Authorization Flow on https://api.hyper3d.com/api/grant/oauth
  - Official hosted MCP (Streamable HTTP) on https://api.hyper3d.com/api/mcp
  - Account/wallet endpoints: POST /api/user/get_info and POST /api/group/group_info

Tokens are stored securely in macOS Keychain (or 0600 config file on non-macOS) and never returned in tool output.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
import webbrowser

import requests

from . import VERSION
from .config import (
    KEYCHAIN_OAUTH_ACCOUNT,
    KEYCHAIN_OAUTH_SERVICE,
    OAUTH_CLIENT_ID,
    OAUTH_SCOPES,
    ServerConfig,
    WALLET_UNITS_PER_CREDIT,
)
from .security import atomic_write_bytes, validate_safe_https_url

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
USER_CODE_PATTERN = re.compile(r"^[A-Za-z0-9 -]{1,64}$")
MCP_PROTOCOL_VERSION = "2025-03-26"

RequestFn = Callable[..., requests.Response]
SleepFn = Callable[[float], None]
NowFn = Callable[[], float]
OpenBrowserFn = Callable[[str], bool]


class TokenStore(Protocol):
    def load(self) -> dict[str, Any] | None: ...
    def save(self, data: dict[str, Any]) -> None: ...
    def delete(self) -> bool: ...


class KeychainOrFileTokenStore:
    """Stores OAuth token payload in macOS Keychain, falling back to a 0600 file on other OSes."""

    def __init__(
        self,
        service: str = KEYCHAIN_OAUTH_SERVICE,
        account: str = KEYCHAIN_OAUTH_ACCOUNT,
        *,
        fallback_path: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._service = service
        self._account = account
        self._fallback_path = fallback_path or (
            Path(os.getenv("XDG_CONFIG_HOME", "~/.config")).expanduser()
            / "rodin-business-mcp"
            / "oauth.json"
        )
        self._runner = runner

    @staticmethod
    def _has_keychain() -> bool:
        return os.name == "posix" and Path("/usr/bin/security").exists()

    def load(self) -> dict[str, Any] | None:
        if self._has_keychain():
            proc = self._runner(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    self._service,
                    "-a",
                    self._account,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if proc.returncode != 0:
                return None
            raw = proc.stdout.strip()
            if not raw:
                return None
            if len(raw) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in raw):
                try:
                    raw = bytes.fromhex(raw).decode("utf-8")
                except Exception:  # noqa: BLE001
                    pass
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None

        if not self._fallback_path.is_file():
            return None
        try:
            data = json.loads(self._fallback_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
        if self._has_keychain():
            proc = self._runner(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    self._service,
                    "-a",
                    self._account,
                    "-w",
                    payload,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if proc.returncode != 0:
                raise RuntimeError("Failed to persist OAuth credentials in macOS Keychain")
            return

        self._fallback_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_bytes(self._fallback_path, (payload + "\n").encode("utf-8"), mode=0o600)

    def delete(self) -> bool:
        if self._has_keychain():
            proc = self._runner(
                [
                    "/usr/bin/security",
                    "delete-generic-password",
                    "-s",
                    self._service,
                    "-a",
                    self._account,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return proc.returncode == 0
        if self._fallback_path.exists():
            self._fallback_path.unlink(missing_ok=True)
            return True
        return False


@dataclass
class PendingDeviceAuth:
    device_code: str
    user_code: str
    verification_url: str
    token_endpoint: str
    expires_at: float
    interval: int


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urlsplit(url)
    return (p.scheme.lower(), (p.hostname or "").lower(), p.port)


def _parse_mcp_response_body(response: requests.Response) -> dict[str, Any]:
    """Parse either application/json or text/event-stream JSON-RPC 2.0 responses."""
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "text/event-stream" in content_type:
        last_obj: dict[str, Any] | None = None
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                parsed = json.loads(chunk)
                if isinstance(parsed, dict):
                    last_obj = parsed
            except json.JSONDecodeError:
                continue
        if last_obj is None:
            raise RuntimeError("Official MCP SSE stream contained no JSON-RPC payload")
        return last_obj
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("Official MCP returned a non-object JSON-RPC response")
    return body


def _extract_mcp_tool_data(rpc_payload: dict[str, Any]) -> Any:
    if "error" in rpc_payload and rpc_payload["error"]:
        err = rpc_payload["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"Official MCP error: {msg}")
    result = rpc_payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Official MCP returned an unexpected result envelope")
    if result.get("isError"):
        texts = [
            item.get("text", "")
            for item in (result.get("content") or [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        detail = "\n".join(t for t in texts if t).strip() or "remote tool returned an error"
        raise RuntimeError(f"Official MCP tool failed: {detail}")
    if "structuredContent" in result and result["structuredContent"] is not None:
        return result["structuredContent"]
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    raise RuntimeError("Official MCP returned no structured result payload")


class Hyper3DOAuthBridge:
    """Manages official OAuth 2.1 Device Flow, account queries, and Streamable HTTP MCP calls."""

    def __init__(
        self,
        config: ServerConfig,
        store: TokenStore,
        *,
        request_fn: RequestFn = requests.request,
        sleep_fn: SleepFn = time.sleep,
        now_fn: NowFn = time.time,
        open_browser_fn: OpenBrowserFn = webbrowser.open,
        resolve_dns: bool = True,
    ) -> None:
        self.config = config
        self.store = store
        self._request = request_fn
        self._sleep = sleep_fn
        self._now = now_fn
        self._open_browser = open_browser_fn
        self._resolve_dns = resolve_dns
        self._pending: PendingDeviceAuth | None = None
        self._session_id: str | None = None
        self._rpc_id = 0

    def _safe_url(self, url: str) -> str:
        return validate_safe_https_url(url, resolve_dns=self._resolve_dns)

    def _next_rpc_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def status(self) -> dict[str, Any]:
        creds = self.store.load()
        if not creds or not creds.get("access_token"):
            pending_info = None
            if self._pending and self._now() < self._pending.expires_at:
                pending_info = {
                    "user_code": self._pending.user_code,
                    "verification_url": self._pending.verification_url,
                    "expires_in_seconds": max(0, int(self._pending.expires_at - self._now())),
                }
            return {
                "connected": False,
                "client_id": OAUTH_CLIENT_ID,
                "mcp_url": self.config.official_mcp_url,
                "pending_device_login": pending_info,
            }

        expires_at = creds.get("expires_at")
        expired = isinstance(expires_at, (int, float)) and expires_at <= self._now()
        return {
            "connected": True,
            "client_id": creds.get("client_id", OAUTH_CLIENT_ID),
            "mcp_url": self.config.official_mcp_url,
            "scope": creds.get("scope", OAUTH_SCOPES),
            "has_refresh_token": bool(creds.get("refresh_token")),
            "access_token_expired": expired,
            "expires_in_seconds": (
                max(0, int(expires_at - self._now())) if isinstance(expires_at, (int, float)) else None
            ),
        }

    def is_connected(self) -> bool:
        creds = self.store.load()
        return bool(creds and creds.get("access_token"))

    def logout(self) -> dict[str, Any]:
        self._pending = None
        self._session_id = None
        removed = self.store.delete()
        return {"logged_out": True, "credentials_removed": removed}

    def discover_oauth(self) -> dict[str, str]:
        parsed = urlsplit(self.config.official_mcp_url)
        pr_url = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{parsed.path}"
        pr_resp = self._request("GET", self._safe_url(pr_url), timeout=(10, 15), allow_redirects=False)
        if pr_resp.status_code >= 400:
            raise RuntimeError(f"OAuth resource discovery failed (HTTP {pr_resp.status_code})")
        pr_meta = pr_resp.json()
        servers = pr_meta.get("authorization_servers") or []
        if not isinstance(servers, list) or not servers:
            raise RuntimeError("OAuth discovery did not advertise an authorization server")

        issuer = str(servers[0]).rstrip("/")
        as_url = f"{issuer}/.well-known/oauth-authorization-server"
        as_resp = self._request("GET", self._safe_url(as_url), timeout=(10, 15), allow_redirects=False)
        if as_resp.status_code >= 400:
            raise RuntimeError(f"OAuth authorization server metadata failed (HTTP {as_resp.status_code})")
        as_meta = as_resp.json()

        if str(as_meta.get("issuer", "")).rstrip("/") != issuer:
            raise RuntimeError("OAuth discovery issuer mismatch")
        device_ep = as_meta.get("device_authorization_endpoint")
        token_ep = as_meta.get("token_endpoint")
        if not isinstance(device_ep, str) or not isinstance(token_ep, str):
            raise RuntimeError("OAuth server does not advertise Device Flow and token endpoints")
        return {
            "resource": str(pr_meta.get("resource") or self.config.official_mcp_url),
            "issuer": issuer,
            "device_authorization_endpoint": self._safe_url(device_ep),
            "token_endpoint": self._safe_url(token_ep),
        }

    def start_device_login(self, *, open_browser: bool = True) -> dict[str, Any]:
        """Start RFC 8628 Device Flow and return verification URL + user code."""
        meta = self.discover_oauth()
        resp = self._request(
            "POST",
            meta["device_authorization_endpoint"],
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "client_id": OAUTH_CLIENT_ID,
                "resource": meta["resource"],
                "scope": OAUTH_SCOPES,
            },
            timeout=(10, 15),
            allow_redirects=False,
        )
        body = resp.json()
        if resp.status_code >= 400:
            raise RuntimeError(f"Device authorization failed: {body.get('error', resp.status_code)}")

        device_code = body.get("device_code")
        user_code = body.get("user_code")
        expires_in = body.get("expires_in")
        interval = body.get("interval", 5)
        verification_uri = body.get("verification_uri")
        verification_complete = body.get("verification_uri_complete") or verification_uri

        if (
            not isinstance(device_code, str)
            or not device_code
            or not isinstance(user_code, str)
            or not USER_CODE_PATTERN.fullmatch(user_code)
            or type(expires_in) is not int
            or not (1 <= expires_in <= 86400)
            or type(interval) is not int
            or not (1 <= interval <= 60)
            or not isinstance(verification_uri, str)
            or not isinstance(verification_complete, str)
        ):
            raise RuntimeError("Invalid device authorization response from OAuth server")

        safe_v = self._safe_url(verification_uri)
        safe_c = self._safe_url(verification_complete)
        if _origin(safe_v) != _origin(safe_c):
            raise RuntimeError("Device verification URL origin mismatch")

        self._pending = PendingDeviceAuth(
            device_code=device_code,
            user_code=user_code,
            verification_url=safe_c,
            token_endpoint=meta["token_endpoint"],
            expires_at=self._now() + expires_in,
            interval=interval,
        )

        browser_opened = False
        if open_browser:
            try:
                browser_opened = bool(self._open_browser(safe_c))
            except Exception:  # noqa: BLE001
                browser_opened = False

        return {
            "authenticated": False,
            "pending": True,
            "user_code": user_code,
            "verification_url": safe_c,
            "expires_in_seconds": expires_in,
            "browser_opened": browser_opened,
            "instructions": (
                f"Confirm code {user_code} in your browser at {safe_c}, select your Business workspace, "
                "and approve access. Then call rodin_oauth_login(wait_for_approval=True) to finalize."
            ),
        }

    def poll_device_login(self, *, timeout_seconds: int = 120) -> dict[str, Any]:
        """Poll token endpoint for the active pending device flow and store tokens on approval."""
        if self._pending is None or self._now() >= self._pending.expires_at:
            raise RuntimeError("No active device authorization pending; start login first.")
        if type(timeout_seconds) is not int or not (5 <= timeout_seconds <= 600):
            raise ValueError("timeout_seconds must be an integer in 5..600")

        pending = self._pending
        deadline = min(pending.expires_at, self._now() + timeout_seconds)
        interval = float(pending.interval)

        while self._now() < deadline:
            remaining = deadline - self._now()
            self._sleep(min(interval, max(0.0, remaining)))
            if self._now() >= deadline:
                break

            resp = self._request(
                "POST",
                pending.token_endpoint,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "grant_type": DEVICE_GRANT_TYPE,
                    "client_id": OAUTH_CLIENT_ID,
                    "device_code": pending.device_code,
                },
                timeout=(10, 15),
                allow_redirects=False,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_hdr = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                try:
                    retry_s = float(int(retry_hdr)) if retry_hdr else 0.0
                except ValueError:
                    retry_s = 0.0
                interval = max(interval + 5.0, retry_s)
                continue

            body = resp.json()
            if resp.status_code < 400 and isinstance(body, dict) and body.get("access_token"):
                self._persist_tokens(body, token_endpoint=pending.token_endpoint)
                self._pending = None
                self._session_id = None
                return {
                    "authenticated": True,
                    "pending": False,
                    "scope": body.get("scope", OAUTH_SCOPES),
                    "has_refresh_token": bool(body.get("refresh_token")),
                }

            err = body.get("error") if isinstance(body, dict) else None
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5.0
                continue
            self._pending = None
            raise RuntimeError(f"OAuth device authorization rejected: {err or resp.status_code}")

        return {
            "authenticated": False,
            "pending": self._now() < pending.expires_at,
            "user_code": pending.user_code,
            "verification_url": pending.verification_url,
            "note": "Timed out waiting for browser approval; call rodin_oauth_login again once approved.",
        }

    def _persist_tokens(
        self,
        tokens: dict[str, Any],
        *,
        token_endpoint: str,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        expires_in = tokens.get("expires_in")
        expires_at = (
            self._now() + float(expires_in)
            if isinstance(expires_in, (int, float)) and expires_in > 0
            else None
        )
        record = {
            "client_id": OAUTH_CLIENT_ID,
            "token_endpoint": token_endpoint,
            "access_token": str(tokens["access_token"]),
            "refresh_token": tokens.get("refresh_token") or (existing or {}).get("refresh_token"),
            "token_type": tokens.get("token_type", "Bearer"),
            "scope": tokens.get("scope") or (existing or {}).get("scope") or OAUTH_SCOPES,
            "expires_at": expires_at,
        }
        self.store.save(record)
        return record

    def _refresh_tokens(self, creds: dict[str, Any]) -> dict[str, Any]:
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("OAuth session expired and no refresh_token is available. Run rodin_oauth_login.")
        token_ep = creds.get("token_endpoint") or self.discover_oauth()["token_endpoint"]
        resp = self._request(
            "POST",
            self._safe_url(str(token_ep)),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "client_id": OAUTH_CLIENT_ID,
                "refresh_token": str(refresh_token),
            },
            timeout=(10, 15),
            allow_redirects=False,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"OAuth token refresh failed (HTTP {resp.status_code}). Run rodin_oauth_login.")
        body = resp.json()
        if not isinstance(body, dict) or not body.get("access_token"):
            raise RuntimeError("OAuth token refresh returned no access_token.")
        return self._persist_tokens(body, token_endpoint=str(token_ep), existing=creds)

    def _access_token(self, *, force_refresh: bool = False) -> str:
        creds = self.store.load()
        if not creds or not creds.get("access_token"):
            raise RuntimeError(
                "Not signed in to Hyper3D OAuth. Call rodin_oauth_login first to use account/upload library tools."
            )
        expires_at = creds.get("expires_at")
        needs_refresh = force_refresh or (
            isinstance(expires_at, (int, float)) and expires_at <= self._now() + 60.0
        )
        if needs_refresh and creds.get("refresh_token"):
            creds = self._refresh_tokens(creds)
        return str(creds["access_token"])

    def _account_post(self, relative_path: str, body: dict[str, Any]) -> dict[str, Any]:
        if relative_path not in {"user/get_info", "group/group_info"}:
            raise ValueError(f"Disallowed account endpoint: {relative_path}")
        url = f"{self.config.account_api_base_url.rstrip('/')}/{relative_path}"
        token = self._access_token()
        for attempt in range(2):
            resp = self._request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=body,
                timeout=(10, 20),
                allow_redirects=False,
            )
            if resp.status_code == 401 and attempt == 0:
                token = self._access_token(force_refresh=True)
                continue
            if resp.status_code in (401, 403):
                raise RuntimeError("Account access denied. Re-run rodin_oauth_login to authorize account:read.")
            if resp.status_code >= 400:
                raise RuntimeError(f"Account endpoint HTTP {resp.status_code}")
            payload = resp.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Unexpected account response format")
            if payload.get("error"):
                raise RuntimeError(f"Account lookup failed: {payload['error']}")
            return payload
        raise RuntimeError("Account authentication failed after token refresh")

    def get_account_info(self) -> dict[str, Any]:
        """Fetch sanitized Business account, wallet breakdown (personal or group), and features."""
        info = self._account_post("user/get_info", {})
        meta = info.get("meta") or {}
        workspace = info.get("billing_workspace") or {"type": "personal"}
        ws_type = workspace.get("type", "personal")

        subs_raw = (meta.get("subscriptions") or {}).get("active_subscriptions") or []
        active_subs: list[dict[str, Any]] = []
        for s in subs_raw:
            if not isinstance(s, dict):
                continue
            entry: dict[str, Any] = {
                "wallet_balance_credits": float(s.get("wallet_balance", 0)) / WALLET_UNITS_PER_CREDIT,
            }
            for key in ("plan_level", "plan_price", "start_time", "end_time"):
                if s.get(key) is not None:
                    entry[key] = s[key]
            if "plan_yearly" in s:
                entry["plan_yearly"] = bool(s["plan_yearly"])
            if "active" in s:
                entry["active"] = bool(s["active"])
            active_subs.append(entry)

        if ws_type == "group":
            group_uuid = str(workspace.get("group_uuid") or "")
            group_data = self._account_post("group/group_info", {"group_uuid": group_uuid})
            group_meta = group_data.get("group_meta") or {}
            if group_meta.get("uuid") != group_uuid:
                raise RuntimeError("Returned group wallet does not match authorized billing workspace")
            balance_credits = float(group_data.get("balance", 0)) / WALLET_UNITS_PER_CREDIT
            frozen_credits = float(group_meta.get("frozen", 0)) / WALLET_UNITS_PER_CREDIT
            wallet = {
                "type": "group",
                "group_uuid": group_uuid,
                "name": group_meta.get("name"),
                "balance_credits": balance_credits,
                "total_credits": balance_credits,
                "frozen_credits": frozen_credits,
            }
        else:
            perm_credits = float(meta.get("balance", 0)) / WALLET_UNITS_PER_CREDIT
            sub_credits = sum(s["wallet_balance_credits"] for s in active_subs)
            frozen_credits = float(meta.get("frozen", 0)) / WALLET_UNITS_PER_CREDIT
            wallet = {
                "type": "personal",
                "balance_credits": perm_credits,
                "subscription_balance_credits": sub_credits,
                "total_credits": round(perm_credits + sub_credits, 4),
                "frozen_credits": frozen_credits,
            }

        summary: dict[str, Any] = {
            "authenticated": True,
            "user": {
                "uuid": meta.get("user_uuid"),
                "username": meta.get("username"),
            },
            "wallet": wallet,
        }
        highest_plan = (meta.get("subscriptions") or {}).get("highest_level")
        if highest_plan:
            summary["highest_plan_level"] = highest_plan
        if active_subs:
            summary["active_subscriptions"] = active_subs
        features = list(meta.get("features") or [])
        if features:
            summary["features"] = features
        return summary

    def _mcp_post(self, token: str, payload: dict[str, Any], *, session_id: str | None = None) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return self._request(
            "POST",
            self.config.official_mcp_url,
            headers=headers,
            json=payload,
            timeout=(10, 60),
            allow_redirects=False,
        )

    def _ensure_mcp_session(self, token: str) -> str | None:
        if self._session_id:
            return self._session_id
        init_payload = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "rodin-business-mcp", "version": VERSION},
            },
        }
        resp = self._mcp_post(token, init_payload)
        if resp.status_code == 401:
            raise PermissionError("OAuth token rejected during MCP initialize")
        if resp.status_code >= 400:
            raise RuntimeError(f"Official MCP initialize failed (HTTP {resp.status_code})")
        _parse_mcp_response_body(resp)
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        self._session_id = sid
        self._mcp_post(
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            session_id=sid,
        )
        return sid

    def call_official_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Invoke an official read/upload-management tool on https://api.hyper3d.com/api/mcp."""
        allowed_tools = frozenset({
            "rodin_list_uploads",
            "rodin_create_uploads",
            "rodin_delete_uploads",
            "rodin_get_result",
        })
        if tool_name not in allowed_tools:
            raise ValueError(f"Official MCP bridge tool not allowed: {tool_name}")

        token = self._access_token()
        for attempt in range(2):
            try:
                sid = self._ensure_mcp_session(token)
            except PermissionError:
                if attempt == 0:
                    self._session_id = None
                    token = self._access_token(force_refresh=True)
                    continue
                raise RuntimeError("Official MCP rejected OAuth credentials. Run rodin_oauth_login.")

            resp = self._mcp_post(
                token,
                {
                    "jsonrpc": "2.0",
                    "id": self._next_rpc_id(),
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
                session_id=sid,
            )
            if resp.status_code == 401 and attempt == 0:
                self._session_id = None
                token = self._access_token(force_refresh=True)
                continue
            if resp.status_code == 404 and attempt == 0 and self._session_id:
                self._session_id = None
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Official MCP HTTP {resp.status_code} on '{tool_name}'")
            rpc = _parse_mcp_response_body(resp)
            return _extract_mcp_tool_data(rpc)

        raise RuntimeError(f"Official MCP call '{tool_name}' failed after session retry")

    def upload_files(self, paths: list[Path], *, kind: str) -> dict[str, Any]:
        """Request presigned PUT URLs from official MCP and upload files without forwarding OAuth credentials."""
        manifest = [{"filename": p.name, "size_bytes": p.stat().st_size} for p in paths]
        payload = {"files": manifest}
        if kind != "image":
            payload["kind"] = kind

        created = self.call_official_tool("rodin_create_uploads", payload)
        uploads = created.get("uploads") if isinstance(created, dict) else None
        if not isinstance(uploads, list) or len(uploads) != len(paths):
            raise RuntimeError("Official MCP returned an unexpected number of upload slots")

        # Validate every destination BEFORE streaming a single byte.
        validated_targets: list[tuple[str, str, dict[str, str]]] = []
        for slot in uploads:
            if not isinstance(slot, dict):
                raise RuntimeError("Malformed upload slot from official MCP")
            method = slot.get("method")
            upload_id = slot.get("upload_id")
            upload_url = slot.get("upload_url")
            headers = slot.get("headers")
            if (
                method != "PUT"
                or not isinstance(upload_id, str)
                or not upload_id
                or not isinstance(upload_url, str)
                or not isinstance(headers, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items())
            ):
                raise RuntimeError("Invalid presigned upload descriptor returned by official MCP")
            safe_url = self._safe_url(upload_url)
            validated_targets.append((upload_id, safe_url, dict(headers)))

        uploaded_ids: list[str] = []
        for path, (upload_id, safe_url, headers) in zip(paths, validated_targets):
            with path.open("rb") as handle:
                resp = self._request(
                    "PUT",
                    safe_url,
                    headers=headers,
                    data=handle,
                    timeout=(10, 120),
                    allow_redirects=False,
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"Presigned upload failed for '{path.name}' (HTTP {resp.status_code})")
            uploaded_ids.append(upload_id)

        return {"kind": kind, "upload_ids": uploaded_ids, "count": len(uploaded_ids)}
