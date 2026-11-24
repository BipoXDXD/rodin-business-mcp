"""Composition root for the Rodin-Business MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server import MCPServer

from . import SERVER_NAME, VERSION
from .config import ApiKeyProvider, ServerConfig
from .job_store import JobStore
from .oauth_bridge import Hyper3DOAuthBridge, KeychainOrFileTokenStore, TokenStore
from .rest_client import RodinRestClient
from .tools_oauth import OAUTH_TOOL_NAMES, register_oauth_tools
from .tools_rest import register_rest_tools


@dataclass
class ServerBundle:
    """Explicit wiring container returned by create_server for clean testing and startup."""

    mcp: MCPServer
    config: ServerConfig
    key_provider: ApiKeyProvider
    job_store: JobStore
    rest_client: RodinRestClient
    oauth_bridge: Hyper3DOAuthBridge
    tools: dict[str, Callable[..., Any]]


def create_server(
    *,
    config: ServerConfig | None = None,
    key_provider: ApiKeyProvider | None = None,
    job_store: JobStore | None = None,
    token_store: TokenStore | None = None,
    rest_request_fn: Any = None,
    download_get_fn: Any = None,
    oauth_request_fn: Any = None,
    sleep_fn: Any = None,
    monotonic_fn: Any = None,
    now_fn: Any = None,
    open_browser_fn: Any = None,
    resolve_dns: bool = True,
) -> ServerBundle:
    """Wire all dependencies explicitly without hidden global singletons."""
    cfg = config or ServerConfig()
    keys = key_provider or ApiKeyProvider(service=cfg.keychain_api_service)
    jobs = job_store or JobStore()
    store = token_store or KeychainOrFileTokenStore(
        service=cfg.keychain_oauth_service,
        account=cfg.keychain_oauth_account,
    )

    rest_kwargs: dict[str, Any] = {}
    if rest_request_fn is not None:
        rest_kwargs["request_fn"] = rest_request_fn
    if sleep_fn is not None:
        rest_kwargs["sleep_fn"] = sleep_fn
    if monotonic_fn is not None:
        rest_kwargs["monotonic_fn"] = monotonic_fn

    rest_client = RodinRestClient(cfg, keys, jobs, **rest_kwargs)

    oauth_kwargs: dict[str, Any] = {"resolve_dns": resolve_dns}
    if oauth_request_fn is not None:
        oauth_kwargs["request_fn"] = oauth_request_fn
    if sleep_fn is not None:
        oauth_kwargs["sleep_fn"] = sleep_fn
    if now_fn is not None:
        oauth_kwargs["now_fn"] = now_fn
    if open_browser_fn is not None:
        oauth_kwargs["open_browser_fn"] = open_browser_fn

    oauth_bridge = Hyper3DOAuthBridge(cfg, store, **oauth_kwargs)

    mcp = MCPServer(SERVER_NAME, version=VERSION)
    tools: dict[str, Callable[..., Any]] = {}
    tools.update(
        register_rest_tools(
            mcp,
            rest_client,
            oauth_tool_names=OAUTH_TOOL_NAMES,
            oauth_connected_fn=oauth_bridge.is_connected,
            get_fn=download_get_fn,
            resolve_dns=resolve_dns,
        )
    )
    tools.update(register_oauth_tools(mcp, oauth_bridge))

    return ServerBundle(
        mcp=mcp,
        config=cfg,
        key_provider=keys,
        job_store=jobs,
        rest_client=rest_client,
        oauth_bridge=oauth_bridge,
        tools=tools,
    )
