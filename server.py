"""Backwards-compatible entrypoint for the Rodin-Business MCP server.

Keeps existing registrations (`python /path/to/server.py`) working unchanged while
delegating all logic to the modular `rodin_business_mcp` package under `src/`.
"""

from __future__ import annotations

from pathlib import Path
import sys

_SRC_DIR = str(Path(__file__).resolve().parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from rodin_business_mcp import SERVER_NAME, VERSION  # noqa: E402
from rodin_business_mcp.app import create_server  # noqa: E402

_bundle = create_server()
mcp = _bundle.mcp

# Re-export registered tool functions for direct inspection and compatibility.
globals().update(_bundle.tools)

__all__ = ["SERVER_NAME", "VERSION", "create_server", "mcp", *_bundle.tools.keys()]

if __name__ == "__main__":
    mcp.run()
