"""
Credential resolution and caching for ctxstore-mcp.

Precedence: TENANT_API_KEY env var, then ~/.ctxstore/credentials.json. With
neither, the server starts in setup mode and the setup_account tool walks the
user through the email-code flow (anonymous provisioning is retired).
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".ctxstore" / "credentials.json"


def load_credentials() -> dict | None:
    """Load credentials from ~/.ctxstore/credentials.json. Returns None if not found."""
    if CREDENTIALS_PATH.exists():
        try:
            return json.loads(CREDENTIALS_PATH.read_text())
        except Exception:
            return None
    return None


def save_credentials(api_key: str, tenant_id: str) -> None:
    """Save credentials to ~/.ctxstore/credentials.json."""
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(
        json.dumps(
            {
                "api_key": api_key,
                "tenant_id": tenant_id,
                "provisioned_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
    )
    # Restrict to owner read/write only
    CREDENTIALS_PATH.chmod(0o600)


def provision_api_key() -> str:
    """Anonymous provisioning was retired (the endpoint answers 410 Gone).

    A key now comes from the email code flow: inside any MCP client call the
    ``setup_account`` tool with mode='link_or_create' and your email, then
    mode='verify' with the six-digit code; the key is saved for you. Or sign
    in at https://ctxstore.ai. This function only explains that and returns
    an empty key so the server can start in setup mode.
    """
    print(
        "\n[ctxstore] No API key yet. Start the server anyway and, in your client, call\n"
        "[ctxstore]   setup_account(mode='link_or_create', email='you@example.com')\n"
        "[ctxstore] then setup_account(mode='verify', email=..., code='123456') with the emailed code.\n"
        "[ctxstore] Or sign in at https://ctxstore.ai and set TENANT_API_KEY. Never paste a key into chat.\n",
        file=sys.stderr,
    )
    return ""


def resolve_api_key() -> str:
    """
    Resolve the API key using this precedence:
      1. TENANT_API_KEY environment variable
      2. ~/.ctxstore/credentials.json
      3. No key: explain the email-code flow and return "" (setup mode)

    Returns the key string, or empty string when no key exists yet.
    """
    # 1. Env var wins
    env_key = os.getenv("TENANT_API_KEY", "").strip()
    if env_key:
        return env_key

    # 2. Cached credentials
    creds = load_credentials()
    if creds and creds.get("api_key"):
        return creds["api_key"]

    # 3. No key yet — setup mode
    return provision_api_key()
