"""Tests for OAuth against providers that do not challenge with 401.

Google's hosted MCP servers (``calendarmcp.googleapis.com``,
``gmailmcp.googleapis.com``) are the motivating case:

* ``initialize`` and ``tools/list`` answer **HTTP 200 without any
  credential**, so the MCP SDK's auth flow — which only runs its discovery
  and authorization branch on a 401 — never starts. ``hermes mcp login``
  therefore connects, lists tools, and exits without ever printing an
  authorize URL or writing a token.
* Their protected-resource metadata lives at the RFC 9728 *path-suffixed*
  location (``/.well-known/oauth-protected-resource/mcp/v1``); the root
  location 404s.
* That metadata advertises the authorization server as
  ``https://accounts.google.com/`` **with** a trailing slash, while the
  authorization-server metadata names its issuer
  ``https://accounts.google.com`` **without** one. The SDK's
  ``validate_metadata_issuer`` compares the two as exact strings and rejects
  the pair, even though its own ``issuers_match`` helper treats them as the
  same server (RFC 8414 section 3.3).

These tests lock in the two behaviours needed to authenticate such a server.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("mcp.client.auth.oauth2", reason="MCP SDK 1.26.0+ required")

import httpx2  # noqa: E402  (after importorskip)

from mcp.client.auth.utils import validate_metadata_issuer  # noqa: E402
from mcp.shared.auth import OAuthMetadata  # noqa: E402

from tools.mcp_oauth import HermesTokenStorage  # noqa: E402
from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS  # noqa: E402


SERVER_URL = "https://calendarmcp.googleapis.com/mcp/v1"

# Verbatim shapes served by Google on 2026-11-09.
GOOGLE_PRM = {
    "authorization_servers": ["https://accounts.google.com/"],
    "bearer_methods_supported": ["header"],
    "resource": "https://calendarmcp.googleapis.com/mcp/v1",
    "scopes_supported": [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
    ],
}
GOOGLE_ASM = {
    "issuer": "https://accounts.google.com",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "response_types_supported": ["code"],
    "code_challenge_methods_supported": ["plain", "S256"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_methods_supported": [
        "client_secret_post",
        "client_secret_basic",
    ],
    "authorization_response_iss_parameter_supported": True,
}


def _google_transport(token_payload: dict | None = None) -> httpx2.MockTransport:
    """Mock transport mirroring Google's real responses, including the 404s."""
    token_payload = token_payload or {
        "access_token": "GOOGLE_ACCESS",
        "token_type": "Bearer",
        "expires_in": 3599,
        "refresh_token": "GOOGLE_REFRESH",
        "scope": " ".join(GOOGLE_PRM["scopes_supported"]),
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url.endswith("/.well-known/oauth-protected-resource/mcp/v1"):
            return httpx2.Response(200, json=GOOGLE_PRM)
        if url.endswith("/.well-known/oauth-protected-resource"):
            return httpx2.Response(404, text="not found")
        if url.endswith("/.well-known/oauth-authorization-server"):
            return httpx2.Response(200, json=GOOGLE_ASM)
        if url == "https://oauth2.googleapis.com/token":
            return httpx2.Response(200, json=token_payload)
        return httpx2.Response(404, text=f"unexpected {url}")

    return httpx2.MockTransport(handler)


def _fake_sdk_httpx(transport: httpx2.MockTransport):
    """Stand-in for ``tools.mcp_tool.sdk_httpx()`` bound to *transport*."""

    class _Module:
        HTTPError = httpx2.HTTPError
        Request = httpx2.Request
        Response = httpx2.Response

        @staticmethod
        def AsyncClient(**kwargs):  # noqa: N802 — mirrors the httpx API
            kwargs.pop("transport", None)
            return httpx2.AsyncClient(transport=transport, **kwargs)

    return _Module


def _preregistered_client_file(token_dir, name: str) -> None:
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / f"{name}.client.json").write_text(
        json.dumps(
            {
                "client_id": "test-client.apps.googleusercontent.com",
                "client_secret": "test-secret",
                "redirect_uris": ["http://127.0.0.1:41234/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            }
        )
    )


def _build_provider(server_name: str, *, captured: dict):
    """Build the production provider class with capturing OAuth handlers."""
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    async def redirect_handler(authorization_url: str) -> None:
        captured["authorize_url"] = authorization_url

    async def callback_handler():
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(captured["authorize_url"]).query)
        return AuthorizationCodeResult(
            code="AUTH_CODE",
            state=params["state"][0],
            iss="https://accounts.google.com",
        )

    client_metadata = OAuthClientMetadata.model_validate(
        {
            "client_name": "Hermes Agent",
            "redirect_uris": ["http://127.0.0.1:41234/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }
    )
    return _HERMES_PROVIDER_CLS(
        server_url=SERVER_URL,
        client_metadata=client_metadata,
        storage=HermesTokenStorage(server_name),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        server_name=server_name,
        preregistered=True,
    )


class TestRootIssuerTrailingSlash:
    """PRM advertises ``https://accounts.google.com/``; ASM says no slash."""

    def test_selected_authorization_server_validates_against_asm_issuer(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        provider = _build_provider("calendar", captured={})

        provider.context.auth_server_url = provider._select_authorization_server(
            GOOGLE_PRM["authorization_servers"]
        )

        asm = OAuthMetadata.model_validate(GOOGLE_ASM)
        # Must not raise: a root issuer with and without its trailing slash
        # names the same authorization server (RFC 8414 section 3.3).
        validate_metadata_issuer(asm, provider._expected_issuer())

    def test_a_different_authorization_server_is_still_rejected(
        self, tmp_path, monkeypatch
    ):
        """Normalising the slash must not make the issuer check toothless."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        provider = _build_provider("calendar", captured={})

        provider.context.auth_server_url = provider._select_authorization_server(
            ["https://attacker.example.com/"]
        )

        asm = OAuthMetadata.model_validate(GOOGLE_ASM)
        with pytest.raises(Exception):
            validate_metadata_issuer(asm, provider._expected_issuer())

    def test_path_bearing_authorization_server_keeps_its_slash_semantics(
        self, tmp_path, monkeypatch
    ):
        """Only a *root* issuer's bare trailing slash is dropped."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        provider = _build_provider("calendar", captured={})

        selected = provider._select_authorization_server(
            ["https://auth.example.com/tenant-a/"]
        )
        assert selected == "https://auth.example.com/tenant-a/"


class TestAuthorizeWithoutChallenge:
    """``hermes mcp login`` must work when the server never answers 401."""

    @pytest.mark.asyncio
    async def test_authorize_now_writes_a_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        token_dir = tmp_path / "mcp-tokens"
        _preregistered_client_file(token_dir, "calendar")

        captured: dict = {}
        provider = _build_provider("calendar", captured=captured)
        transport = _google_transport()

        with patch(
            "tools.mcp_tool.sdk_httpx", return_value=_fake_sdk_httpx(transport)
        ):
            ok = await provider.authorize_now()

        assert ok is True
        assert "authorize_url" in captured, "no authorize URL was ever produced"
        assert captured["authorize_url"].startswith(
            "https://accounts.google.com/o/oauth2/v2/auth?"
        )

        tokens_file = token_dir / "calendar.json"
        assert tokens_file.exists(), "no token was written to disk"
        assert json.loads(tokens_file.read_text())["access_token"] == "GOOGLE_ACCESS"

    @pytest.mark.asyncio
    async def test_authorize_now_requests_the_resource_scopes(
        self, tmp_path, monkeypatch
    ):
        """Scopes come from protected-resource metadata, per the MCP spec."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _preregistered_client_file(tmp_path / "mcp-tokens", "calendar")

        captured: dict = {}
        provider = _build_provider("calendar", captured=captured)

        with patch(
            "tools.mcp_tool.sdk_httpx",
            return_value=_fake_sdk_httpx(_google_transport()),
        ):
            await provider.authorize_now()

        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(captured["authorize_url"]).query)
        assert params["scope"][0].split() == GOOGLE_PRM["scopes_supported"]
        assert params["client_id"][0] == "test-client.apps.googleusercontent.com"
        assert params["code_challenge_method"][0] == "S256"

    @pytest.mark.asyncio
    async def test_authorize_now_explains_a_missing_oauth_client(
        self, tmp_path, monkeypatch
    ):
        """Google publishes no registration_endpoint, so DCR cannot rescue us."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "mcp-tokens").mkdir(parents=True, exist_ok=True)

        provider = _build_provider("calendar", captured={})

        with patch(
            "tools.mcp_tool.sdk_httpx",
            return_value=_fake_sdk_httpx(_google_transport()),
        ):
            with pytest.raises(Exception) as excinfo:
                await provider.authorize_now()

        assert "client_id" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_authorize_now_persists_discovered_metadata(
        self, tmp_path, monkeypatch
    ):
        """So a later cold-load refreshes without rediscovery."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        token_dir = tmp_path / "mcp-tokens"
        _preregistered_client_file(token_dir, "calendar")

        provider = _build_provider("calendar", captured={})

        with patch(
            "tools.mcp_tool.sdk_httpx",
            return_value=_fake_sdk_httpx(_google_transport()),
        ):
            await provider.authorize_now()

        meta = HermesTokenStorage("calendar").load_oauth_metadata()
        assert meta is not None
        assert str(meta.token_endpoint) == "https://oauth2.googleapis.com/token"


class TestLoginFallsBackToForcedAuthorization:
    """The CLI must drive the forced flow when a probe yields no token."""

    def test_login_forces_authorization_when_probe_leaves_no_token(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        from hermes_cli import mcp_config

        # Probe succeeds (Google serves tools/list unauthenticated) but no
        # token ever lands — exactly the reported symptom.
        monkeypatch.setattr(
            mcp_config,
            "_probe_single_server",
            lambda *a, **k: [("list_events", "List events")],
        )

        forced = {"called": False}

        def fake_force(name, server_config):
            forced["called"] = True
            return True

        monkeypatch.setattr(mcp_config, "_force_oauth_authorization", fake_force)
        monkeypatch.setattr(
            mcp_config,
            "_oauth_tokens_present",
            lambda name: forced["called"],
        )

        ok = mcp_config._reauth_oauth_server(
            "calendar", {"url": SERVER_URL, "auth": "oauth"}
        )

        assert forced["called"], "login did not attempt forced authorization"
        assert ok is True
