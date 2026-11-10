"""ctxstore-setup writes nothing unless asked, and Claude Code gets project scope."""
from __future__ import annotations

import json
import os
import sys

import pytest

PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from ctxstore_mcp import setup_cli  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)  # Claude Code is installed
    (h / ".cursor").mkdir()
    (h / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"url": "https://x"}}}))
    monkeypatch.setattr(setup_cli.Path, "home", staticmethod(lambda: h))
    monkeypatch.setattr(setup_cli, "load_credentials", lambda: None)
    monkeypatch.setattr(setup_cli, "_health", lambda: True)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    return h, project


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}


def test_default_run_writes_nothing(home, capsys):
    h, project = home
    before = _snapshot(h)
    assert setup_cli.main([]) == 0
    assert _snapshot(h) == before
    assert not (project / ".mcp.json").exists()
    out = capsys.readouterr().out
    assert "showing only" in out
    assert "--scope project" in out
    assert "Claude Code (project scope)" in out and "Cursor" in out


def test_write_puts_claude_code_at_project_scope_and_keeps_other_servers(home):
    h, project = home
    assert setup_cli.main(["--write"]) == 0
    mcp = json.loads((project / ".mcp.json").read_text())
    assert mcp["mcpServers"]["ctxstore"] == {"type": "http", "url": setup_cli.MCP_URL}
    assert not (h / ".claude.json").exists()  # user scope untouched
    cursor = json.loads((h / ".cursor" / "mcp.json").read_text())
    assert cursor["mcpServers"]["other"] == {"url": "https://x"}
    assert cursor["mcpServers"]["ctxstore"] == {"url": setup_cli.MCP_URL}


def test_global_flag_writes_user_scope_for_claude_code(home):
    h, project = home
    (h / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
    assert setup_cli.main(["--write", "--global", "--client", "claude-code"]) == 0
    assert json.loads((h / ".claude.json").read_text())["mcpServers"]["ctxstore"]["url"] == setup_cli.MCP_URL
    assert not (project / ".mcp.json").exists()


def test_no_key_is_written_unless_one_is_saved(home, monkeypatch):
    h, project = home
    (h / "Library" / "Application Support" / "Claude").mkdir(parents=True)
    assert setup_cli.main(["--write", "--client", "claude-desktop"]) == 0
    desk = json.loads((h / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json").read_text())
    assert desk["mcpServers"]["ctxstore"] == {"command": "uvx", "args": ["ctxstore-mcp"]}
    monkeypatch.setattr(setup_cli, "load_credentials", lambda: {"api_key": "emk_saved"})
    assert setup_cli.main(["--write", "--client", "claude-desktop"]) == 0
    desk = json.loads((h / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json").read_text())
    assert desk["mcpServers"]["ctxstore"]["env"] == {"TENANT_API_KEY": "emk_saved"}


def test_invalid_json_config_is_left_alone(home, capsys):
    h, project = home
    (h / ".cursor" / "mcp.json").write_text("{not json")
    assert setup_cli.main(["--write", "--client", "cursor"]) == 0
    assert (h / ".cursor" / "mcp.json").read_text() == "{not json"
    assert "leaving it alone" in capsys.readouterr().err
