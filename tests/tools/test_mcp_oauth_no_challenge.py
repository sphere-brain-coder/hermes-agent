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

# Google publishes neither of these, so the branches below are exercised with
# authorization servers that do.
REGISTERING_ASM = {**GOOGLE_ASM, "registration_endpoint": "https://accounts.google.com/register"}
CIMD_ASM = {**GOOGLE_ASM, "client_id_metadata_document_supported": True}

CIMD_URL = "https://nousresearch.github.io/hermes-agent/docs/oauth/client-metadata.json"


def _google_transport(
    token_payload: dict | None = None, *, asm: dict | None = None
) -> httpx2.MockTransport:
    """Mock transport mirroring Google's real responses, including the 404s."""
    asm = asm if asm is not None else GOOGLE_ASM
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
            return httpx2.Response(200, json=asm)
        if url == "https://accounts.google.com/register":
            return httpx2.Response(
                201,
                json={
                    "client_id": "dynamically-registered",
                    "client_secret": "dcr-secret",
                    "redirect_uris": ["http://127.0.0.1:41234/callback"],
                },
            )
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


def _build_provider(
    server_name: str,
    *,
    captured: dict,
    scope: str | None = None,
    client_metadata_url: str | None = None,
):
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

    metadata: dict = {
        "client_name": "Hermes Agent",
        "redirect_uris": ["http://127.0.0.1:41234/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }
    if scope is not None:
        # What `oauth.scope` in config.yaml produces (tools.mcp_oauth.
        # _build_client_metadata puts it here).
        metadata["scope"] = scope
    extra = {}
    if client_metadata_url is not None:
        extra["client_metadata_url"] = client_metadata_url
    return _HERMES_PROVIDER_CLS(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata.model_validate(metadata),
        storage=HermesTokenStorage(server_name),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        server_name=server_name,
        preregistered=True,
        **extra,
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


class TestWorksOnThePinnedSdk:
    """The forced path must not need SDK members the pinned version lacks.

    ``_select_authorization_server`` and ``_expected_issuer`` only exist on
    ``OAuthClientProvider`` from mcp 2.2.0. This repo pins ``mcp==2.0.0``
    (pyproject, and uv.lock is what CI installs), where calling ``super()``
    for either raises ``AttributeError`` and the whole forced-authorization
    path dies before printing an authorize URL.

    These tests delete both members from the base class, so they exercise the
    pinned-SDK behaviour whatever version is installed. On 2.0.0 the deletes
    are no-ops and the tests still assert the real code path.
    """

    @staticmethod
    def _strip_base_members(monkeypatch):
        from mcp.client.auth import OAuthClientProvider

        for name in ("_select_authorization_server", "_expected_issuer"):
            monkeypatch.delattr(OAuthClientProvider, name, raising=False)

    def test_the_overrides_do_not_require_the_newer_sdk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._strip_base_members(monkeypatch)
        provider = _build_provider("calendar", captured={})

        selected = provider._select_authorization_server(
            GOOGLE_PRM["authorization_servers"]
        )
        assert selected == "https://accounts.google.com"

        provider.context.auth_server_url = selected
        assert provider._expected_issuer() == "https://accounts.google.com"

    def test_expected_issuer_falls_back_to_the_resource_origin(
        self, tmp_path, monkeypatch
    ):
        """The legacy no-PRM path: RFC 8414 section 3.3 builds from the origin."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._strip_base_members(monkeypatch)
        provider = _build_provider("calendar", captured={})

        provider.context.auth_server_url = None
        # SERVER_URL carries a path; only the origin identifies the issuer,
        # and it must render without an empty trailing path so the
        # exact-string comparison in validate_metadata_issuer can succeed.
        assert provider._expected_issuer() == "https://calendarmcp.googleapis.com"

    @pytest.mark.asyncio
    async def test_authorize_now_runs_on_the_pinned_sdk(self, tmp_path, monkeypatch):
        """End to end: an authorize URL and a token, with no 2.2.0 members."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._strip_base_members(monkeypatch)
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
        assert captured["authorize_url"].startswith(
            "https://accounts.google.com/o/oauth2/v2/auth"
        )


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
    async def test_a_configured_scope_narrows_the_request(self, tmp_path, monkeypatch):
        """`oauth.scope` must win over the resource's advertised set.

        config.yaml documents `oauth.scope` as "default: server-provided", so
        a configured value replaces that default. It is the only way to
        request *fewer* scopes than the resource advertises — the gmail entry
        narrowing away from `gmail.send` is exactly that case. Without this,
        the configured value is silently discarded and consent still asks for
        send.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _preregistered_client_file(tmp_path / "mcp-tokens", "calendar")

        narrowed = "https://www.googleapis.com/auth/calendar.readonly"
        captured: dict = {}
        provider = _build_provider("calendar", captured=captured, scope=narrowed)

        with patch(
            "tools.mcp_tool.sdk_httpx",
            return_value=_fake_sdk_httpx(_google_transport()),
        ):
            await provider.authorize_now()

        from urllib.parse import parse_qs, urlparse

        requested = parse_qs(urlparse(captured["authorize_url"]).query)["scope"][0]
        assert requested.split() == [narrowed]
        # The broader scope the resource advertises must not sneak back in.
        assert "https://www.googleapis.com/auth/calendar" not in requested.split()

    @pytest.mark.asyncio
    async def test_a_client_id_metadata_document_is_used_when_supported(
        self, tmp_path, monkeypatch
    ):
        """CIMD before DCR, as in the SDK's 401 branch.

        CIMD is Hermes' default for any server with no pinned client, and an
        authorization server can advertise it while publishing no
        `registration_endpoint`. Going straight to registration would both
        fail that flow and tell the user to create an OAuth client they do
        not need.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "mcp-tokens").mkdir(parents=True, exist_ok=True)

        provider = _build_provider(
            "calendar", captured={}, client_metadata_url=CIMD_URL
        )

        with patch(
            "tools.mcp_tool.sdk_httpx",
            return_value=_fake_sdk_httpx(_google_transport(asm=CIMD_ASM)),
        ):
            ok = await provider.authorize_now()

        assert ok is True
        assert provider.context.client_info.client_id == CIMD_URL

    @pytest.mark.asyncio
    async def test_dynamic_registration_binds_credentials_to_the_issuer(
        self, tmp_path, monkeypatch
    ):
        """SEP-2352: registered credentials record the issuer they belong to.

        `credentials_match_issuer` treats a null issuer as "no binding to
        enforce", so leaving it unset would let credentials registered here be
        presented to a different authorization server later.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "mcp-tokens").mkdir(parents=True, exist_ok=True)

        provider = _build_provider("calendar", captured={})

        with patch(
            "tools.mcp_tool.sdk_httpx",
            return_value=_fake_sdk_httpx(_google_transport(asm=REGISTERING_ASM)),
        ):
            ok = await provider.authorize_now()

        assert ok is True
        assert provider.context.client_info.client_id == "dynamically-registered"
        assert provider.context.client_info.issuer == "https://accounts.google.com"

        on_disk = json.loads(
            (tmp_path / "mcp-tokens" / "calendar.client.json").read_text()
        )
        assert on_disk["issuer"] == "https://accounts.google.com"

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
