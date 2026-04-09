"""
ctxstore credential management — auto-provisioning and credential caching.

Flow:
  1. Check TENANT_API_KEY env var (always wins)
  2. Check ~/.ctxstore/credentials.json
  3. POST /api/v1/provision to get a free key, save it
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("ctxstore.auth")

CREDENTIALS_FILE = Path.home() / ".ctxstore" / "credentials.json"
CTXSTORE_URL = os.getenv("CTXSTORE_URL", "https://ctxstore.ai").rstrip("/")
PROVISION_URL = f"{CTXSTORE_URL}/api/v1/provision"


def load_credentials() -> Optional[dict]:
    """Load saved credentials from disk. Returns None if not found or invalid."""
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        with open(CREDENTIALS_FILE) as f:
            data = json.load(f)
        if data.get("api_key"):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def save_credentials(api_key: str, tenant_id: str = "") -> None:
    """Save credentials to ~/.ctxstore/credentials.json with 600 permissions."""
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "api_key": api_key,
        "tenant_id": tenant_id,
        "provisioned_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    CREDENTIALS_FILE.chmod(0o600)


def provision() -> str:
    """
    Call the ctxstore.ai provision endpoint to get a free API key.
    Saves credentials to disk and prints a friendly first-run message.
    Returns the api_key string.
    """
    print(
        "\n✦ ctxstore.ai: provisioning your free memory account...",
        file=sys.stderr,
    )
    try:
        resp = httpx.post(
            PROVISION_URL,
            json={"source": "ctxstore-mcp"},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Could not provision ctxstore.ai account: {e}\n"
            f"Set TENANT_API_KEY manually or visit {CTXSTORE_URL}"
        ) from e

    api_key = data.get("api_key", "")
    tenant_id = data.get("tenant_id", "")

    if not api_key:
        raise RuntimeError(
            f"Provisioning response missing api_key. "
            f"Visit {CTXSTORE_URL} to get your key."
        )

    save_credentials(api_key, tenant_id)
    print(
        f"✓ ctxstore.ai: account provisioned! Key saved to {CREDENTIALS_FILE}",
        file=sys.stderr,
    )
    return api_key


def get_or_provision() -> str:
    """
    Return the API key, auto-provisioning if needed.

    Priority:
      1. TENANT_API_KEY environment variable
      2. ~/.ctxstore/credentials.json
      3. Auto-provision via ctxstore.ai (free, no signup)
    """
    # 1. Env var always wins
    env_key = os.getenv("TENANT_API_KEY", "").strip()
    if env_key:
        return env_key

    # 2. Saved credentials
    creds = load_credentials()
    if creds:
        return creds["api_key"]

    # 3. Auto-provision
    return provision()


# Alias for server.py import
resolve_api_key = get_or_provision
