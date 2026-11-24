# Rodin-Business MCP (v3.0.0)

[![CI](https://github.com/BipoXDXD/rodin-business-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/BipoXDXD/rodin-business-mcp/actions/workflows/ci.yml)

An **unofficial**, modular local [Model Context Protocol](https://modelcontextprotocol.io) server for
**Hyper3D Rodin Business**, built strictly on official documented contracts:

1. **Hyper3D REST API v2** (`https://api.hyper3d.com/api/v2/*`) — authenticated with your Business API key (macOS Keychain or `RODIN_API_KEY`). Works standalone with zero browser or OAuth login.
2. **Optional Official OAuth 2.1 Bridge** (`https://api.hyper3d.com/api/mcp` + `/api/user/get_info` & `/api/group/group_info`) — RFC 8628 Device Flow with tokens stored in macOS Keychain (or `0600` config file on Linux), unlocking account/wallet breakdown, team workspaces, persistent upload library management, and permanent `display_url` links.

> Not affiliated with or endorsed by Deemos / Hyper3D. "Rodin" and "Hyper3D" are their trademarks.
> On Business subscriptions, both REST API v2 and the official OAuth MCP draw from the same unified credit pool.

## Architecture

```text
server.py                       # Backwards-compatible MCP entrypoint
src/rodin_business_mcp/
├── app.py                      # Composition root (explicit dependency injection)
├── config.py                   # Constants, OpenAPI limits, Keychain/env API key lookup
├── security.py                 # HTTPS + DNS-resolved SSRF guard, atomic writes, safe filenames
├── pricing.py                  # Local credit estimator & finite budget guard
├── validators.py               # Parse-don't-validate OpenAPI v3 input parsers
├── rest_client.py              # Fixed-endpoint REST v2 client (429/5xx retry on reads; 0 retries on paid calls)
├── job_store.py                # Bounded in-memory session ledger of submitted jobs
├── downloads.py                # Atomic streaming downloads & base64 PNG/JPEG/WebP decoder
├── oauth_bridge.py             # Official OAuth 2.1 Device Flow + Streamable HTTP MCP client
├── tools_rest.py               # 17 REST API v2 tools
└── tools_oauth.py              # 8 optional OAuth 2.1 tools
```

## Tools (25 total)

### REST API v2 — 17 tools (Business API Key only)

| Tool | What it does | Credits* |
|---|---|---|
| `rodin_capabilities` | Lists capabilities, auth state, all tools, and safety rules | free |
| `rodin_balance` | Business API balance + optional paginated usage history (`usage_page >= 1`) | free |
| `rodin_estimate` | Local credit estimate, no HTTP request | free |
| `rodin_generate` | Gen-2.5 text/image-to-3D (all 5 tiers, 2K–12K textures, `HighPack`, `uhd_texture`, `hd_texture`, Quad/Raw, `bbox_condition`, `image_labels`, `TAPose`, `is_micro`, `preview_render`) | 0.5–3 |
| `rodin_agentic` | Rodin Agentic (beta) from 1–5 images with optional `route` (`high-poly` / `n-gons`) | 0.5 / 1 / 2 |
| `rodin_bang_asset` | BANG! split/segment a Rodin asset (`instruction`, `preview_render`, `Hybrid`) | 0.5 |
| `rodin_bang_model` | BANG! split/segment an uploaded 3D model (`obj`/`glb`/`stl`/`fbx`/`usd*`) | 0.5 |
| `rodin_texture` | Generate 2K–12K PBR/Shaded/Hybrid/All textures for an uploaded model from 1–5 reference images (`uhd_texture`, `hd_texture`, `image_labels`, `preview_render`) | 0.5 (2.0 `extreme-high`) |
| `rodin_rigging` | Skeleton + skinning (`auto`/`human`/`quadruped`/`bird`/`dragon`/`kaiju`/`custom`; 1–256 bone `hierarchy` with optional `transform`/`length`; stock or FBX clips; `ue5`/`unity`/`actorcore` FBX; `in_place`) | 0.5 |
| `rodin_animation_catalog` | Stock rigging animation catalog with metadata and MP4 preview URLs | free |
| `rodin_polygen` | PolyGen Smart Low-Poly (200–10,000 faces; `Quad`/`Raw`/`NGon`) from asset, model, images, or text | 0.5 |
| `rodin_images` | Synchronous **Text-to-Image** (JSON) and **Image Editing** (1–3 references, up to 2048×2048), saved atomically to disk | 0.5 |
| `rodin_status` | Single task status check (`jobs.subscription_key` or Agentic `job.subscription_key`) | free |
| `rodin_wait` | Async polling with backoff and HTTP 429 `Retry-After` handling until `Done`, `Failed`, or deadline | free |
| `rodin_download_links` | Temporary signed result URLs for a completed `task_uuid` | free |
| `rodin_save_results` | Atomic local download with DNS-resolved SSRF & redirect protection | free |
| `rodin_recent_jobs` | Lists recent paid jobs submitted in the current server session | free |

### Optional Official OAuth 2.1 Bridge — 8 tools (free)

| Tool | What it does |
|---|---|
| `rodin_oauth_status` | Connection state, expiry, scopes, and pending device login (never exposes tokens) |
| `rodin_oauth_login` | RFC 8628 Device Flow sign-in (stores tokens in macOS Keychain / `0600` file) |
| `rodin_oauth_logout` | Removes stored local OAuth tokens |
| `rodin_account_info` | Business plan, wallet split (permanent + subscription credits, or team workspace), and unlocked features |
| `rodin_uploads_list` | Lists saved files in your Hyper3D library (`image`, `model`, `animation`) |
| `rodin_uploads_create` | Uploads local files via presigned HTTPS PUT URLs (never forwards OAuth tokens) |
| `rodin_uploads_delete` | Deletes saved/transient uploads by `upload_id` to release quota |
| `rodin_result_page` | Fetches permanent Hyper3D `display_url` and output metadata |

\* Estimates come from published Hyper3D docs; the API response field `consumed` is authoritative.

## Spending & Security Guarantees

- **Dry-run by default:** Every paid tool defaults to `approved_spend=False` and returns validated parameters plus estimated cost without touching the network. Execution requires `approved_spend=True` and a finite `max_credits >= estimated_credits`.
- **Zero auto-retries on paid calls:** Transient HTTP `429`/`5xx` retries with `Retry-After` backoff apply **only** to idempotent read endpoints (`check_balance`, `rigging_animations`, `status`, `download`). Billable POST endpoints never retry automatically.
- **No credential leakage:** API keys and OAuth tokens are never tool parameters and never appear in outputs or errors.
- **SSRF protection:** Download and presigned-upload URLs must be HTTPS on port 443, disallow redirects, and resolve to public global IPs only.

## Scope & Intentional Exclusions

- **Web-only CAPTCHA-gated tools are intentionally excluded:** OmniCraft `Image Enhancer` (2K/4K upscaler), `HDRI Generator`, `Image Remix`, Web Gateway `Video` (`Veo 3.1`/`Seedance`), `WorldGen`, and `ChatAvatar` are not exposed on the public REST API v2 or official OAuth MCP and are protected by web CAPTCHA (`Turnstile`/`Aliyun`/`GeeTest`). This server uses official documented APIs only.
- **Legacy Gen-2 and Gen-1/1.5 tiers** remain omitted in favor of Gen-2.5.

## Setup

```bash
git clone https://github.com/BipoXDXD/rodin-business-mcp.git
cd rodin-business-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -W error -m unittest discover -s tests -v
```

### Business API Key

Store in macOS Keychain (`hyper3d-business`):

```bash
security add-generic-password -s hyper3d-business -a "$(id -un)" -w
```

Or export `RODIN_API_KEY` in the MCP server's environment (env takes precedence over Keychain).

### Optional OAuth 2.1 Login

Inside your MCP client, run `rodin_oauth_login` to authorize via browser Device Flow (`https://api.hyper3d.com/api/grant/oauth`). Credentials are saved to macOS Keychain (`hyper3d-business-oauth`) or `~/.config/rodin-business-mcp/oauth.json` (`0600`) on Linux.

## License

[MIT](LICENSE)
