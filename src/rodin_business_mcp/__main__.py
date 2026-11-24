"""CLI module entrypoint: `python -m rodin_business_mcp`."""

from __future__ import annotations

from .app import create_server


def main() -> None:
    bundle = create_server()
    bundle.mcp.run()


if __name__ == "__main__":
    main()
