"""Thin CLI for the Claude Desktop MCP installer.

Useful for power users and as a debugging fallback if the GUI prompt
misbehaves. The primary install path is the first-launch dialog in the
Qt UI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as ``python scripts/install_mcp.py`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import mcp_install  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the 'transcribe' entry from the Claude Desktop config.",
    )
    group.add_argument(
        "--check",
        action="store_true",
        help="Print whether the integration is currently installed.",
    )
    args = parser.parse_args(argv)

    if args.check:
        installed = mcp_install.is_installed()
        path = mcp_install.DEFAULT_CONFIG_PATH
        if installed:
            print(f"installed: 'transcribe' entry in {path} matches this checkout.")
            return 0
        print(f"not installed: no matching entry at {path}.")
        return 1

    if args.uninstall:
        result = mcp_install.uninstall()
    else:
        result = mcp_install.install()
    print(result.message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
