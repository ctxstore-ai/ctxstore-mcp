"""
ctxstore-setup — one-command MCP client configuration.

Detects installed AI clients, injects ctxstore MCP config, provisions tenant.
Usage: ctxstore-setup
"""

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from .auth import get_or_provision

CTXSTORE_URL = os.getenv("CTXSTORE_URL", "https://ctxstore.ai").rstrip("/")

# ── MCP client config locations ────────────────────────────────────────────────

MCP_CLIENTS = [
    {
        "name": "Claude Desktop",
        "paths": [
            Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
            Path.home() / ".config" / "claude" / "claude_desktop_config.json",
        ],
        "wrapper": "mcpServers",
    },
    {
        "name": "Claude Code",
        "paths": [Path.home() / ".claude.json"],
        "wrapper": "mcpServers",
    },
    {
        "name": "Cursor",
        "paths": [Path.home() / ".cursor" / "mcp.json"],
        "wrapper": "mcpServers",
    },
    {
        "name": "VS Code",
        "paths": [Path.home() / ".vscode" / "mcp.json"],
        "wrapper": "servers",
    },
    {
        "name": "Windsurf",
        "paths": [Path.home() / ".windsurf" / "mcp.json"],
        "wrapper": "mcpServers",
    },
]


def _inject_config(config_path: Path, wrapper: str, api_key: str) -> bool:
    """
    Non-destructively inject ctxstore entry into an MCP client config.
    Backs up the original file before modifying.
    Returns True on success.
    """
    try:
        with open(config_path) as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError:
                config = {}

        if wrapper not in config:
            config[wrapper] = {}

        config[wrapper]["ctxstore"] = {
            "command": "ctxstore-mcp",
            "env": {"TENANT_API_KEY": api_key},
        }

        # Backup
        shutil.copy2(config_path, str(config_path) + ".bak")

        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")

        return True
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ✗ Error modifying {config_path}: {e}", file=sys.stderr)
        return False


def _health_check() -> bool:
    """Check if ctxstore.ai is reachable."""
    try:
        import httpx
        resp = httpx.get(f"{CTXSTORE_URL}/api/health", timeout=10.0)
        return resp.status_code == 200 and "ok" in resp.text.lower()
    except Exception:
        return False


def main() -> None:
    print("\n✦ ctxstore-setup\n")

    # Step 1: Get/provision API key
    print("→ Checking credentials...")
    try:
        api_key = get_or_provision()
        print(f"  ✓ API key ready")
    except RuntimeError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)

    # Step 2: Detect and configure MCP clients
    print("\n→ Detecting MCP clients...")
    configured = []
    found_any = False

    for client in MCP_CLIENTS:
        for config_path in client["paths"]:
            if config_path.exists():
                found_any = True
                print(f"  Found: {client['name']} ({config_path})")
                success = _inject_config(config_path, client["wrapper"], api_key)
                if success:
                    configured.append(client["name"])
                    print(f"  ✓ Configured {client['name']}")
                break  # Only configure first found path per client

    if not found_any:
        print("  No MCP clients detected.")
        print("\n  Add this to your client's MCP config manually:")
        print("""
  {
    "mcpServers": {
      "ctxstore": {
        "command": "ctxstore-mcp",
        "env": { "TENANT_API_KEY": \"""" + api_key + """\" }
      }
    }
  }""")

    # Step 3: Health check
    print("\n→ Health check...")
    if _health_check():
        print("  ✓ ctxstore.ai is reachable")
    else:
        print("  ⚠ Could not reach ctxstore.ai — check your connection")

    # Step 4: Summary
    print("\n" + "━" * 50)
    print("✓ ctxstore-setup complete!\n")
    if configured:
        print(f"  Configured: {', '.join(configured)}")
        print("  → Restart your AI client to activate memory")
    print(f"\n  Docs: {CTXSTORE_URL}/docs")
    print("━" * 50 + "\n")


if __name__ == "__main__":
    main()
