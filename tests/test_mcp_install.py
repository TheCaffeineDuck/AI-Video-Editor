"""Tests for ``core.mcp_install``.

All tests redirect the Claude Desktop config path into a temp dir via
the ``config_path`` parameter — the real config is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core import mcp_install


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "claude_desktop_config.json"


@pytest.fixture(autouse=True)
def _stub_paths(tmp_path: Path):
    """Pretend the venv + main_mcp.py exist so install() doesn't bail."""
    fake_venv = tmp_path / ".venv" / "bin" / "python"
    fake_main = tmp_path / "main_mcp.py"
    fake_venv.parent.mkdir(parents=True, exist_ok=True)
    fake_venv.write_text("#!/bin/sh\n")
    fake_main.write_text("# entry\n")
    with patch.object(mcp_install, "VENV_PYTHON", fake_venv), patch.object(
        mcp_install, "MAIN_MCP", fake_main
    ):
        yield (fake_venv, fake_main)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_into_empty_config(config_path, _stub_paths):
    venv, main_mcp = _stub_paths
    result = mcp_install.install(config_path=config_path)
    assert result.ok
    assert result.action == "installed"
    data = _read(config_path)
    assert data["mcpServers"]["transcribe"] == {
        "command": str(venv),
        "args": [str(main_mcp)],
    }
    assert mcp_install.is_installed(config_path=config_path)


def test_install_unchanged_when_entry_matches(config_path, _stub_paths):
    mcp_install.install(config_path=config_path)
    result = mcp_install.install(config_path=config_path)
    assert result.ok
    assert result.action == "unchanged"


def test_install_updates_when_paths_differ(config_path, _stub_paths):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "transcribe": {
                        "command": "/old/python",
                        "args": ["/old/main_mcp.py"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    venv, main_mcp = _stub_paths
    result = mcp_install.install(config_path=config_path)
    assert result.ok
    assert result.action == "updated"
    entry = _read(config_path)["mcpServers"]["transcribe"]
    assert entry["command"] == str(venv)
    assert entry["args"] == [str(main_mcp)]


def test_install_preserves_other_entries(config_path, _stub_paths):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "filesystem": {"command": "node", "args": ["fs.js"]}
                },
                "globalShortcut": "Cmd+Shift+T",
            }
        ),
        encoding="utf-8",
    )
    result = mcp_install.install(config_path=config_path)
    assert result.ok
    data = _read(config_path)
    assert data["mcpServers"]["filesystem"] == {
        "command": "node",
        "args": ["fs.js"],
    }
    assert "transcribe" in data["mcpServers"]
    assert data["globalShortcut"] == "Cmd+Shift+T"


def test_uninstall_removes_only_transcribe(config_path, _stub_paths):
    mcp_install.install(config_path=config_path)
    # Add another entry to confirm it survives.
    data = _read(config_path)
    data["mcpServers"]["other"] = {"command": "x", "args": []}
    config_path.write_text(json.dumps(data), encoding="utf-8")

    result = mcp_install.uninstall(config_path=config_path)
    assert result.ok
    after = _read(config_path)
    assert "transcribe" not in after["mcpServers"]
    assert after["mcpServers"]["other"] == {"command": "x", "args": []}


def test_uninstall_idempotent(config_path, _stub_paths):
    # No file at all.
    result = mcp_install.uninstall(config_path=config_path)
    assert result.ok
    assert result.action == "noop"
    # File with no entry.
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    result = mcp_install.uninstall(config_path=config_path)
    assert result.ok
    assert result.action == "noop"


def test_install_refuses_invalid_json(config_path, _stub_paths):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{ this is not json", encoding="utf-8")
    original = config_path.read_text(encoding="utf-8")
    result = mcp_install.install(config_path=config_path)
    assert not result.ok
    assert result.action == "error"
    assert config_path.read_text(encoding="utf-8") == original


def test_install_missing_venv(config_path, tmp_path):
    missing = tmp_path / "nope" / "bin" / "python"
    fake_main = tmp_path / "main_mcp.py"
    fake_main.write_text("")
    with patch.object(mcp_install, "VENV_PYTHON", missing), patch.object(
        mcp_install, "MAIN_MCP", fake_main
    ):
        result = mcp_install.install(config_path=config_path)
    assert not result.ok
    assert result.action == "error"
    assert "venv" in result.message.lower()
    assert not config_path.exists()


def test_is_installed_false_when_missing(config_path, _stub_paths):
    assert mcp_install.is_installed(config_path=config_path) is False


def test_is_installed_false_when_paths_differ(config_path, _stub_paths):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "transcribe": {"command": "/wrong", "args": ["/x"]}
                }
            }
        ),
        encoding="utf-8",
    )
    assert mcp_install.is_installed(config_path=config_path) is False
