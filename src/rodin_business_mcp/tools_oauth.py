"""Optional official OAuth 2.1 MCP tools (account/wallet, persistent uploads, result URL)."""

from __future__ import annotations

from typing import Any, Callable, Literal

from .config import (
    ANIMATION_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MAX_ANIMATION_SIZE,
    MAX_IMAGE_SIZE,
    MAX_MODEL_SIZE,
    STRICT_MODEL_EXTENSIONS,
    UPLOAD_KINDS,
)
from .oauth_bridge import Hyper3DOAuthBridge
from .validators import parse_local_file, parse_uuid, require

OAUTH_TOOL_NAMES = (
    "rodin_oauth_status",
    "rodin_oauth_login",
    "rodin_oauth_logout",
    "rodin_account_info",
    "rodin_uploads_list",
    "rodin_uploads_create",
    "rodin_uploads_delete",
    "rodin_result_page",
)


def register_oauth_tools(mcp: Any, bridge: Hyper3DOAuthBridge) -> dict[str, Callable[..., Any]]:
    """Register the 8 official OAuth 2.1 bridge tools and return a name->callable map."""
    registered: dict[str, Callable[..., Any]] = {}

    def _register(fn: Callable[..., Any]) -> Callable[..., Any]:
        mcp.tool()(fn)
        registered[fn.__name__] = fn
        return fn

    @_register
    def rodin_oauth_status() -> dict[str, Any]:
        """FREE: Check whether the optional official Hyper3D OAuth 2.1 bridge is connected (never exposes tokens)."""
        return bridge.status()

    @_register
    def rodin_oauth_login(
        open_browser: bool = True,
        wait_for_approval: bool = False,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        """FREE: Sign in to Hyper3D official OAuth 2.1 (Device Flow) to unlock account_info, upload library, and display_url.
        Default wait_for_approval=False returns the verification URL and user code immediately so the user can approve
        in their browser, then call rodin_oauth_login(wait_for_approval=True) to complete and save tokens in Keychain.
        """
        if wait_for_approval and bridge.status().get("pending_device_login"):
            return bridge.poll_device_login(timeout_seconds=timeout_seconds)
        started = bridge.start_device_login(open_browser=open_browser)
        if not wait_for_approval:
            return started
        return bridge.poll_device_login(timeout_seconds=timeout_seconds)

    @_register
    def rodin_oauth_logout() -> dict[str, Any]:
        """FREE: Remove stored local OAuth 2.1 tokens from Keychain/config (does not affect your Business API key)."""
        return bridge.logout()

    @_register
    def rodin_account_info() -> dict[str, Any]:
        """FREE (requires OAuth): Show Business account plan, wallet split (permanent + subscription credits, or team workspace), and unlocked features."""
        return bridge.get_account_info()

    @_register
    def rodin_uploads_list(
        kind: Literal["image", "model", "animation"] | None = None,
        tag: str | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """FREE (requires OAuth): List saved files in your Hyper3D upload library (newest first)."""
        if kind is not None:
            require(kind in UPLOAD_KINDS, f"Invalid upload kind: {kind}")
        require(type(page) is int and page >= 1, "page must be an integer >= 1")
        args: dict[str, Any] = {"page": page}
        if kind is not None:
            args["kind"] = kind
        if tag is not None:
            require(isinstance(tag, str) and bool(tag.strip()), "tag must be a non-blank string")
            args["tag"] = tag.strip()
        return bridge.call_official_tool("rodin_list_uploads", args)

    @_register
    def rodin_uploads_create(
        file_paths: list[str],
        kind: Literal["image", "model", "animation"] = "image",
    ) -> dict[str, Any]:
        """FREE (requires OAuth): Upload local images (1–5, <=25 MiB), a model (1, <=512 MiB), or FBX animations (1–10, <=25 MiB) to your Hyper3D library."""
        require(kind in UPLOAD_KINDS, f"Invalid upload kind: {kind}")
        require(isinstance(file_paths, list) and bool(file_paths), "file_paths must contain at least one path")

        if kind == "image":
            require(len(file_paths) <= 5, "At most 5 image files per upload batch")
            exts, limit = IMAGE_EXTENSIONS, MAX_IMAGE_SIZE
        elif kind == "model":
            require(len(file_paths) == 1, "Upload one model file at a time")
            exts, limit = STRICT_MODEL_EXTENSIONS, MAX_MODEL_SIZE
        else:
            require(len(file_paths) <= 10, "At most 10 FBX animation files per upload batch")
            exts, limit = ANIMATION_EXTENSIONS, MAX_ANIMATION_SIZE

        parsed = [
            parse_local_file(p, allowed_extensions=exts, size_limit=limit, label=kind)
            for p in file_paths
        ]
        return bridge.upload_files(parsed, kind=kind)

    @_register
    def rodin_uploads_delete(upload_ids: list[str]) -> dict[str, Any]:
        """FREE (requires OAuth): Delete saved library or transient uploads by upload_id to free quota."""
        require(isinstance(upload_ids, list) and 1 <= len(upload_ids) <= 50, "Provide 1..50 upload_ids")
        cleaned: list[str] = []
        for uid in upload_ids:
            require(isinstance(uid, str) and bool(uid.strip()), "Each upload_id must be a non-blank string")
            cleaned.append(uid.strip())
        return bridge.call_official_tool("rodin_delete_uploads", {"upload_ids": cleaned})

    @_register
    def rodin_result_page(generation_id: str) -> dict[str, Any]:
        """FREE (requires OAuth): Retrieve the permanent Hyper3D display_url and output metadata for a generation."""
        parsed_id = parse_uuid(generation_id, "generation_id")
        return bridge.call_official_tool("rodin_get_result", {"generation_id": parsed_id})

    return registered
