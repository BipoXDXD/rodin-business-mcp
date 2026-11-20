# Rodin-Business MCP

[![CI](https://github.com/BipoXDXD/rodin-business-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/BipoXDXD/rodin-business-mcp/actions/workflows/ci.yml)

An **unofficial**, local [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Hyper3D Rodin](https://hyper3d.ai) **REST API v2**, authenticated with a Business API key.
It lets MCP clients such as Claude Code, Claude Desktop or Codex generate, texture, rig and
simplify 3D models and download the results to disk. No browser and no OAuth login are needed.

> Not affiliated with or endorsed by Deemos / Hyper3D. "Rodin" and "Hyper3D" are their trademarks.
> Hyper3D also offers an official hosted MCP (OAuth) at `https://api.hyper3d.com/api/mcp`.
> Use this server if you want to use your Business API key and keep downloads local.

## Tools

| Tool | What it does | Credits* |
|---|---|---|
| `rodin_capabilities` | Lists features, limits and safety rules | free |
| `rodin_balance` | Current Business API balance | free |
| `rodin_estimate` | Local cost estimate, no request sent | free |
| `rodin_generate` | Gen-2.5 text/image-to-3D (all tiers, 2K–12K textures, HighPack, Quad/Raw, seeds, multiview labels) | 0.5–3 |
| `rodin_agentic` | Rodin Agentic (beta) from 1–5 images | 0.5 / 1 / 2 |
| `rodin_bang_asset` | BANG! split/refine on a Rodin asset | 0.5 |
| `rodin_bang_model` | BANG! split/refine on an uploaded model | 0.5 |
| `rodin_texture` | Texture an existing model (up to 12K) | 0.5 (+2 extreme-high) |
| `rodin_rigging` | Skeleton + skinning (auto, human, quadruped, bird, dragon, kaiju, custom), stock or FBX animations, UE5/Unity/ActorCore FBX skeletons | 0.5 |
| `rodin_animation_catalog` | Stock rigging animations with metadata and previews | free |
| `rodin_polygen` | PolyGen Smart Low Poly (200–10,000 faces) from an asset, model, images or text | 0.5 |
| `rodin_images` | Image generation/editing (up to 3 references), saved to disk | 0.5 |
| `rodin_status` | Task status | free |
| `rodin_download_links` | Signed result URLs | free |
| `rodin_save_results` | Downloads finished results to disk | free |

\* Estimates from the published docs; the API response (`consumed`) is authoritative.

## Spending safety

Paid tools default to a **dry-run preview**: they validate the inputs and return the parameters
and estimated cost without sending anything. A request is sent only with `approved_spend=true`
and a finite `max_credits` that covers the estimate. Invalid combinations are rejected locally
before any request, so they cost nothing.

The API key is never a tool parameter and is never returned in tool output.

## Requirements

- Python 3.12+
- A Hyper3D Business API key. PolyGen needs **Full API** access; `resolution=High` needs
  high-resolution export access.

## Install

```bash
git clone https://github.com/BipoXDXD/rodin-business-mcp.git
cd rodin-business-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest test_server   # offline, no API calls
```

### API key

On macOS, store the key in the login Keychain (service `hyper3d-business`):

```bash
security add-generic-password -s hyper3d-business -a "$(id -un)" -w
```

On any OS you can instead set `RODIN_API_KEY` in the MCP server's environment. If both are set,
the environment variable wins. If calls fail with `401`, check for an old exported value.

### Register the server

Claude Code:

```bash
claude mcp add --scope user Rodin-Business -- \
  "$PWD/.venv/bin/python" "$PWD/server.py"
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "Rodin-Business": {
      "command": "/absolute/path/to/rodin-business-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/rodin-business-mcp/server.py"]
    }
  }
}
```

Codex CLI:

```bash
codex mcp add Rodin-Business -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

## Typical flow

1. Submit a job (`rodin_generate`, `rodin_polygen`, `rodin_rigging`, ...), first as a preview,
   then with approval.
2. Poll `rodin_status` with `jobs.subscription_key` until every job is `Done`.
3. Call `rodin_save_results` with the top-level `uuid`. Files land in `~/Downloads/Hyper3D`,
   or in the directory set by `RODIN_OUTPUT_DIR`.
4. Open the GLB/FBX in Blender or your engine.

`rodin_images` is synchronous and saves the image right away.

## Not included

- Library uploads (list/delete): these exist only in the hosted OAuth MCP. The REST API sends
  files directly with each request.
- Rodin Gen-2 and Gen-1/1.5 generators: removed in favour of Gen-2.5.

## Development

Tests are offline contract tests: HTTP is mocked at the transport boundary, so they never
spend credits.

```bash
.venv/bin/python -m unittest -v test_server
```

API reference: <https://docs.hyper3d.ai/en>

## License

[MIT](LICENSE)
