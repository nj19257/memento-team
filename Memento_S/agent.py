"""Memento-S agent entry point."""
from __future__ import annotations

from core.config import PROJECT_ROOT, MODEL, DEBUG  # noqa: F401
from core.mcp_client import MCPToolManager  # noqa: F401
from core.model_factory import build_chat_model  # noqa: F401


def main() -> None:
    from cli.main import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
