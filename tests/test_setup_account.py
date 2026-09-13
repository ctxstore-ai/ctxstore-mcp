"""Unit tests for the ctxstore-mcp connector's setup_account flow.

Covers the auth path that lets agents recover from missing/invalid
TENANT_API_KEY without restarting the connector. Filed in response to
Opus 4.8 refusing the OTP path on 2026-05-28 — error messages now
explicitly guide the agent through the email-magic-code flow with
safety-friendly framing (no 'credential extraction' triggers), and
setup_account is exempt from the no-key gate so it can run pre-auth.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

# Make the connector's package importable.
PKG_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from ctxstore_mcp import server as mcp_server  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset_global_api_key():
    """Reset the connector's module-global API_KEY before each test."""
    prior = mcp_server.API_KEY
    mcp_server.API_KEY = None
    yield
    mcp_server.API_KEY = prior


# ── link_or_create ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_or_create_sends_otp_and_returns_instructions():
    with patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_FakeResponse(200))
        out = await mcp_server._handle_setup_account(
            {"mode": "link_or_create", "email": "user@example.org"}
        )
    text = out[0].text
    assert "6-digit code" in text
    assert "user@example.org" in text
    assert "setup_account(mode='verify'" in text


@pytest.mark.asyncio
async def test_link_or_create_requires_email():
    out = await mcp_server._handle_setup_account({"mode": "link_or_create"})
    assert "Missing email" in out[0].text


@pytest.mark.asyncio
async def test_link_or_create_handles_rate_limit():
    with patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_FakeResponse(429))
        out = await mcp_server._handle_setup_account(
            {"mode": "link_or_create", "email": "u@e.org"}
        )
    assert "Rate limited" in out[0].text


# ── verify ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_success_binds_api_key_globally():
    payload = {"api_key": "emk_freshly_minted_xyz", "is_new": True}
    with patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_FakeResponse(200, payload))
        out = await mcp_server._handle_setup_account(
            {"mode": "verify", "email": "u@e.org", "code": "123456"}
        )
    assert mcp_server.API_KEY == "emk_freshly_minted_xyz"
    text = out[0].text
    assert "Account created and ready" in text
    assert "emk_freshly_minted_xyz" in text
    assert "TENANT_API_KEY" in text  # user gets persistence instructions


@pytest.mark.asyncio
async def test_verify_returning_user_message():
    payload = {"api_key": "emk_existing_user_key", "is_new": False}
    with patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_FakeResponse(200, payload))
        out = await mcp_server._handle_setup_account(
            {"mode": "verify", "email": "u@e.org", "code": "654321"}
        )
    assert "Welcome back" in out[0].text


@pytest.mark.asyncio
async def test_verify_invalid_code_guides_retry():
    with patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = AsyncMock(
            return_value=_FakeResponse(400, {"error": "Invalid or expired code."})
        )
        out = await mcp_server._handle_setup_account(
            {"mode": "verify", "email": "u@e.org", "code": "000000"}
        )
    text = out[0].text
    assert "rejected" in text.lower() or "invalid" in text.lower()
    assert "setup_account(mode='link_or_create'" in text  # path to retry
    # API_KEY must NOT be set on failed verify.
    assert mcp_server.API_KEY is None


@pytest.mark.asyncio
async def test_verify_requires_email_and_code():
    out = await mcp_server._handle_setup_account({"mode": "verify", "email": "u@e.org"})
    assert "Both email and code" in out[0].text


# ── restore ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_with_valid_key_validates_and_binds():
    with patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_FakeResponse(200))
        out = await mcp_server._handle_setup_account(
            {"mode": "restore", "api_key": "emk_user_pasted_key"}
        )
    assert mcp_server.API_KEY == "emk_user_pasted_key"
    assert "validated" in out[0].text.lower()


@pytest.mark.asyncio
async def test_restore_rejects_non_emk_key():
    out = await mcp_server._handle_setup_account(
        {"mode": "restore", "api_key": "sk_wrong_format"}
    )
    assert mcp_server.API_KEY is None
    assert "emk_" in out[0].text


@pytest.mark.asyncio
async def test_restore_rejects_invalid_key_per_server():
    with patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_FakeResponse(401))
        out = await mcp_server._handle_setup_account(
            {"mode": "restore", "api_key": "emk_stale_revoked_key"}
        )
    assert mcp_server.API_KEY is None
    assert "rejected" in out[0].text.lower()


# ── unknown mode ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_mode_returns_helpful_error():
    out = await mcp_server._handle_setup_account({"mode": "magic"})
    assert "Unknown" in out[0].text
    assert "link_or_create" in out[0].text


# ── safety-framing guard tests ───────────────────────────────────────────────


def test_setup_account_tool_description_avoids_safety_triggers():
    """Ensures the setup_account tool description doesn't contain framing that
    safety-trained models read as credential exfiltration. emily_4.8 refused
    the flow on 2026-05-28 because the error message it saw said 'check your
    TENANT_API_KEY' (config-file framing). The fix here also requires the tool
    description itself stays disarmed."""
    # Pull the tool list — list_tools is async, so run it.
    import asyncio
    tools = asyncio.run(mcp_server.list_tools())
    setup = next((t for t in tools if t.name == "setup_account"), None)
    assert setup is not None, "setup_account must be exposed by the connector"
    desc = (setup.description or "").lower()
    # The framing must explicitly establish the user-as-owner, user-types-code pattern
    assert "user" in desc
    assert "magic" in desc  # frames it as a magic-link/code login (not extraction)
    # Banned wording — these are the exact phrases that triggered the refusal
    assert "credential extraction" not in desc
    assert "capture" not in desc  # "capture the OTP" is the trigger phrase
