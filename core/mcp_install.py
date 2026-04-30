"""One-click Claude Desktop MCP server installer.

Adds a ``transcribe`` entry to Claude Desktop's
``claude_desktop_config.json`` so the user can talk to the Transcribe
MCP server from any Claude chat. Mac-only — paths are hard-coded to
``~/Library/Application Support/Claude``.

Public surface:

- :func:`is_installed` — does the config already point at this checkout?
- :func:`install` — add or replace the entry. Idempotent.
- :func:`uninstall` — remove the entry. Idempotent.

All three accept an optional ``config_path`` to redirect at a temp file
for testing. Real callers (the GUI prompt, the CLI wrapper) pass nothing
and get the real path.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

ENTRY_NAME = "transcribe"

# Project root: this file lives at <project>/core/mcp_install.py.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
MAIN_MCP = PROJECT_ROOT / "main_mcp.py"

DEFAULT_CONFIG_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Claude"
    / "claude_desktop_config.json"
)


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    config_path: Path
    action: str  # 'installed' | 'updated' | 'unchanged' | 'noop' | 'error'
    message: str


def _resolve_config_path(config_path: Path | None) -> Path:
    return config_path if config_path is not None else DEFAULT_CONFIG_PATH


def _expected_entry() -> dict[str, object]:
    return {
        "command": str(VENV_PYTHON),
        "args": [str(MAIN_MCP)],
    }


def _read_config(path: Path) -> tuple[dict | None, str | None]:
    """Return (parsed, error). On missing/empty file return ({}-shape, None)."""
    if not path.exists():
        return {"mcpServers": {}}, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"Could not read {path}: {exc}"
    if not raw.strip():
        return {"mcpServers": {}}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            f"{path} exists but is not valid JSON ({exc.msg} at line "
            f"{exc.lineno}, col {exc.colno}). Refusing to overwrite. "
            f"Fix the file by hand and retry."
        )
    if not isinstance(data, dict):
        return None, f"{path} top-level is not a JSON object."
    if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
        data["mcpServers"] = {}
    return data, None


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Single rolling backup of the existing config. Atomic rename
    # protects against partial writes; this protects against a logic
    # bug in this module emitting a structurally valid but semantically
    # destructive payload (e.g. wiping someone's other mcpServers
    # entries). Best-effort: if we can't write the backup, proceed
    # anyway — refusing to install over a non-writable backup path is
    # worse than the residual risk.
    if path.exists():
        backup = path.parent / (path.name + ".bak")
        try:
            backup.write_bytes(path.read_bytes())
        except OSError:
            pass
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def is_installed(*, config_path: Path | None = None) -> bool:
    path = _resolve_config_path(config_path)
    data, err = _read_config(path)
    if err is not None or data is None:
        return False
    entry = data.get("mcpServers", {}).get(ENTRY_NAME)
    if not isinstance(entry, dict):
        return False
    return entry == _expected_entry()


def install(*, config_path: Path | None = None) -> InstallResult:
    path = _resolve_config_path(config_path)

    if not VENV_PYTHON.exists():
        return InstallResult(
            ok=False,
            config_path=path,
            action="error",
            message=(
                f"venv Python not found at {VENV_PYTHON}. Create the venv "
                f"and install dependencies first:\n"
                f"  python3.11 -m venv .venv\n"
                f"  ./.venv/bin/pip install -r requirements.txt"
            ),
        )
    if not MAIN_MCP.exists():
        return InstallResult(
            ok=False,
            config_path=path,
            action="error",
            message=f"MCP entry point not found at {MAIN_MCP}.",
        )

    data, err = _read_config(path)
    if err is not None:
        return InstallResult(
            ok=False, config_path=path, action="error", message=err
        )
    assert data is not None

    expected = _expected_entry()
    existing = data["mcpServers"].get(ENTRY_NAME)
    if existing == expected:
        return InstallResult(
            ok=True,
            config_path=path,
            action="unchanged",
            message=(
                f"Already connected. Claude Desktop config at {path} "
                f"points at this checkout."
            ),
        )
    action = "updated" if isinstance(existing, dict) else "installed"
    data["mcpServers"][ENTRY_NAME] = expected
    payload = json.dumps(data, indent=2) + "\n"
    try:
        _atomic_write(path, payload)
    except OSError as exc:
        return InstallResult(
            ok=False,
            config_path=path,
            action="error",
            message=f"Could not write {path}: {exc}",
        )
    verb = "Updated" if action == "updated" else "Installed"
    return InstallResult(
        ok=True,
        config_path=path,
        action=action,
        message=(
            f"{verb} 'transcribe' entry in {path}. Quit Claude Desktop "
            f"(Cmd-Q) and reopen it for the connection to take effect."
        ),
    )


def uninstall(*, config_path: Path | None = None) -> InstallResult:
    path = _resolve_config_path(config_path)
    if not path.exists():
        return InstallResult(
            ok=True,
            config_path=path,
            action="noop",
            message=f"No Claude Desktop config at {path}; nothing to remove.",
        )
    data, err = _read_config(path)
    if err is not None:
        return InstallResult(
            ok=False, config_path=path, action="error", message=err
        )
    assert data is not None
    if ENTRY_NAME not in data.get("mcpServers", {}):
        return InstallResult(
            ok=True,
            config_path=path,
            action="noop",
            message="No 'transcribe' entry to remove.",
        )
    del data["mcpServers"][ENTRY_NAME]
    payload = json.dumps(data, indent=2) + "\n"
    try:
        _atomic_write(path, payload)
    except OSError as exc:
        return InstallResult(
            ok=False,
            config_path=path,
            action="error",
            message=f"Could not write {path}: {exc}",
        )
    return InstallResult(
        ok=True,
        config_path=path,
        action="removed",
        message=(
            f"Removed 'transcribe' entry from {path}. Restart Claude "
            f"Desktop to drop the connection."
        ),
    )
