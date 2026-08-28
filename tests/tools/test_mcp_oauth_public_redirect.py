"""Public (gateway-forwarded) OAuth redirect_uri for MCP flows.

A headless box has no browser, and the browser that does the authorizing runs
on someone else's laptop — it can never reach ``http://127.0.0.1:<port>/callback``
on the server. When the deployment fronts Hermes with a gateway that forwards
``<base>/oauth/cb/<flow-id>`` to the flow's local listener, these tests pin the
engine side of that contract:

  * ``HERMES_MCP_OAUTH_REDIRECT_BASE`` (or ``mcp.oauth_redirect_base``, or a
    per-server ``oauth.redirect_base``) makes the registered redirect_uri
    public while the callback keeps listening on loopback.
  * Unset means nothing changes: the loopback default is preserved.
  * Both URIs are registered when the base is set, so a provider that honours
    only one of them still has a working callback.
"""
import asyncio

import pytest


BASE = "https://os.example.com"


def _clear_base(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_OAUTH_REDIRECT_BASE", raising=False)
    monkeypatch.setattr(
        "tools.mcp_oauth._config_redirect_base", lambda: None, raising=False
    )


# ---------------------------------------------------------------------------
# Flow id
# ---------------------------------------------------------------------------


def test_flow_id_is_stable_for_a_server():
    """The id must survive a restart: it is baked into a registered redirect_uri.

    A per-process random id would make the second login present a redirect_uri
    the cached client registration never registered, and the authorization
    server rejects it.
    """
    from tools.mcp_oauth import oauth_flow_id

    assert oauth_flow_id("notion") == oauth_flow_id("notion")


def test_flow_id_differs_between_servers():
    from tools.mcp_oauth import oauth_flow_id

    assert oauth_flow_id("notion") != oauth_flow_id("linear")


def test_flow_id_is_url_safe():
    import re

    from tools.mcp_oauth import oauth_flow_id

    assert re.fullmatch(r"[A-Za-z0-9]{8,64}", oauth_flow_id("../../etc/passwd"))


# ---------------------------------------------------------------------------
# _resolve_redirect_uri
# ---------------------------------------------------------------------------


def test_public_base_from_env_builds_oauth_cb_url(monkeypatch):
    from tools.mcp_oauth import _resolve_redirect_uri

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)

    assert (
        _resolve_redirect_uri({"_flow_id": "abc123"}, 41234)
        == f"{BASE}/oauth/cb/abc123"
    )


def test_public_base_trailing_slash_does_not_double_up(monkeypatch):
    from tools.mcp_oauth import _resolve_redirect_uri

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE + "/")

    assert (
        _resolve_redirect_uri({"_flow_id": "abc123"}, 41234)
        == f"{BASE}/oauth/cb/abc123"
    )


def test_base_unset_keeps_loopback_default(monkeypatch):
    from tools.mcp_oauth import _resolve_redirect_uri

    _clear_base(monkeypatch)

    assert (
        _resolve_redirect_uri({"_flow_id": "abc123"}, 41234)
        == "http://127.0.0.1:41234/callback"
    )


def test_explicit_redirect_uri_still_wins_over_public_base(monkeypatch):
    from tools.mcp_oauth import _resolve_redirect_uri

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)
    cfg = {"_flow_id": "abc123", "redirect_uri": "https://funnel.example/callback"}

    assert _resolve_redirect_uri(cfg, 41234) == "https://funnel.example/callback"


def test_per_server_base_overrides_env(monkeypatch):
    from tools.mcp_oauth import _resolve_redirect_uri

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)
    cfg = {"_flow_id": "abc123", "redirect_base": "https://other.example/gw"}

    assert _resolve_redirect_uri(cfg, 41234) == "https://other.example/gw/oauth/cb/abc123"


def test_config_yaml_base_used_when_env_unset(monkeypatch):
    """``mcp.oauth_redirect_base`` in config.yaml is the non-env way to set it."""
    from tools.mcp_oauth import _resolve_redirect_uri

    _clear_base(monkeypatch)
    monkeypatch.setattr(
        "tools.mcp_oauth._config_redirect_base", lambda: BASE, raising=False
    )

    assert (
        _resolve_redirect_uri({"_flow_id": "abc123"}, 41234)
        == f"{BASE}/oauth/cb/abc123"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-url",
        "ftp://os.example.com",
        "https://",
        "https://os.example.com/gw?x=1",
        "https://os.example.com/gw#frag",
    ],
)
def test_invalid_base_falls_back_to_loopback(monkeypatch, bad, caplog):
    """A malformed base must not silently produce a broken redirect_uri."""
    from tools.mcp_oauth import _resolve_redirect_uri

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", bad)

    assert (
        _resolve_redirect_uri({"_flow_id": "abc123"}, 41234)
        == "http://127.0.0.1:41234/callback"
    )


def test_missing_flow_id_falls_back_to_loopback(monkeypatch):
    from tools.mcp_oauth import _resolve_redirect_uri

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)

    assert (
        _resolve_redirect_uri({}, 41234) == "http://127.0.0.1:41234/callback"
    )


# ---------------------------------------------------------------------------
# Registration: both URIs, public first
# ---------------------------------------------------------------------------


def test_client_metadata_registers_public_and_loopback(monkeypatch):
    pytest.importorskip("mcp")
    from tools.mcp_oauth import _build_client_metadata

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)
    cfg = {"cimd": False, "_resolved_port": 41234, "_flow_id": "abc123"}

    uris = [str(u) for u in _build_client_metadata(cfg).redirect_uris]

    # Public first: the SDK sends redirect_uris[0] on the authorization request.
    assert uris[0] == f"{BASE}/oauth/cb/abc123"
    assert "http://127.0.0.1:41234/callback" in uris


def test_client_metadata_loopback_only_when_base_unset(monkeypatch):
    pytest.importorskip("mcp")
    from tools.mcp_oauth import _build_client_metadata

    _clear_base(monkeypatch)
    cfg = {"cimd": False, "_resolved_port": 41234, "_flow_id": "abc123"}

    uris = [str(u) for u in _build_client_metadata(cfg).redirect_uris]

    assert uris == ["http://127.0.0.1:41234/callback"]


def test_loopback_registration_can_be_disabled(monkeypatch):
    """Some authorization servers reject a registration with mixed URIs."""
    pytest.importorskip("mcp")
    from tools.mcp_oauth import _build_client_metadata

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)
    cfg = {
        "cimd": False,
        "_resolved_port": 41234,
        "_flow_id": "abc123",
        "register_loopback_redirect": False,
    }

    uris = [str(u) for u in _build_client_metadata(cfg).redirect_uris]

    assert uris == [f"{BASE}/oauth/cb/abc123"]


def test_preregistered_client_records_both_uris(monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    import json

    from tools.mcp_oauth import (
        HermesTokenStorage,
        _build_client_metadata,
        _maybe_preregister_client,
    )

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)
    cfg = {
        "cimd": False,
        "client_id": "pinned-id",
        "_resolved_port": 41234,
        "_flow_id": "abc123",
    }
    storage = HermesTokenStorage("srv")
    _maybe_preregister_client(storage, cfg, _build_client_metadata(cfg))

    data = json.loads(storage._client_info_path().read_text())
    assert data["redirect_uris"][0] == f"{BASE}/oauth/cb/abc123"
    assert "http://127.0.0.1:41234/callback" in data["redirect_uris"]


# ---------------------------------------------------------------------------
# Port wiring: the listener stays local
# ---------------------------------------------------------------------------


def test_public_base_still_allocates_a_real_local_port(monkeypatch, tmp_path):
    """The flow listens on loopback; only the *registered* URI is public."""
    from tools.mcp_oauth import HermesTokenStorage, _configure_callback_port

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)
    cfg = {"cimd": False}

    port = _configure_callback_port(cfg, HermesTokenStorage("srv"))

    assert port > 0
    assert cfg["_resolved_port"] == port


def test_configure_callback_port_sets_flow_id_from_server_name(monkeypatch, tmp_path):
    from tools.mcp_oauth import (
        HermesTokenStorage,
        _configure_callback_port,
        oauth_flow_id,
    )

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)
    cfg = {"cimd": False}

    _configure_callback_port(cfg, HermesTokenStorage("notion"))

    assert cfg["_flow_id"] == oauth_flow_id("notion")


def test_cached_public_uri_does_not_zero_the_port(monkeypatch, tmp_path):
    """Regression guard for the cached-https shortcut.

    Once a public redirect_uri has been registered it lands in client.json.
    Reading it back must not collapse the callback port to 0 — the gateway
    forwards to a fixed local port, and port 0 binds a random one.
    """
    import json

    from tools.mcp_oauth import HermesTokenStorage, _configure_callback_port

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)
    storage = HermesTokenStorage("srv")
    storage._client_info_path().parent.mkdir(parents=True, exist_ok=True)
    storage._client_info_path().write_text(
        json.dumps(
            {
                "client_id": "x",
                "redirect_uris": [
                    f"{BASE}/oauth/cb/abc123",
                    "http://127.0.0.1:41234/callback",
                ],
            }
        )
    )
    cfg = {"cimd": False}

    port = _configure_callback_port(cfg, storage)

    # The loopback URI in the cached registration pins the port, so the
    # listener comes back on the port the gateway already forwards to.
    assert port == 41234


def test_public_base_disables_cimd(monkeypatch, tmp_path):
    """The published CIMD document only declares loopback URIs.

    Presenting it while requesting a public redirect_uri is an exact-match
    failure at the authorization endpoint, so a configured base must send the
    flow down DCR instead.
    """
    from tools.mcp_oauth import HermesTokenStorage, _maybe_use_cimd

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", BASE)

    assert _maybe_use_cimd({}, HermesTokenStorage("srv")) is None


# ---------------------------------------------------------------------------
# Operator-facing output
# ---------------------------------------------------------------------------


def test_redirect_handler_prints_public_uri_and_local_port(monkeypatch, capsys):
    """The pairing line is how the fronting gateway learns fid -> port."""
    import tools.mcp_oauth as mco

    _clear_base(monkeypatch)
    monkeypatch.setattr(mco, "_is_interactive", lambda: True)
    monkeypatch.setattr(mco, "_can_open_browser", lambda: False)

    handler = mco._make_redirect_handler(
        41234, redirect_uri=f"{BASE}/oauth/cb/abc123"
    )
    asyncio.get_event_loop().run_until_complete(handler("https://example.com/auth"))

    err = capsys.readouterr().err
    assert f"{BASE}/oauth/cb/abc123" in err
    assert "41234" in err


# ---------------------------------------------------------------------------
# End to end: a real gateway forward closes the loop
# ---------------------------------------------------------------------------


class _GatewayForwarder:
    """A stand-in for the fronting gateway's ``/oauth/cb/<fid>`` route.

    Mirrors the real deployment: registration is loopback-only, and the route
    relays the provider's query string to ``http://127.0.0.1:<port>/callback``.
    Used to prove the whole path, not just the string that gets registered.
    """

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import urlparse

        self.routes: dict[str, int] = {}
        routes = self.routes

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                import urllib.request

                parsed = urlparse(self.path)
                prefix = "/oauth/cb/"
                if not parsed.path.startswith(prefix):
                    self.send_response(404)
                    self.end_headers()
                    return
                port = routes.get(parsed.path[len(prefix):])
                if port is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                target = f"http://127.0.0.1:{port}/callback"
                if parsed.query:
                    target += f"?{parsed.query}"
                urllib.request.urlopen(target, timeout=5).read()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Connected.")

            def log_message(self, format, *args):  # noqa: A002
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.base = "http://127.0.0.1:%d" % self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def test_public_callback_reaches_the_local_listener(monkeypatch, tmp_path):
    """The authorization code survives the gateway hop.

    This is the claim the option exists to make: a browser that can only
    reach the public base still delivers its code to the loopback listener
    the flow is waiting on.
    """
    import threading
    import time
    import urllib.request
    from urllib.parse import urlparse

    import tools.mcp_oauth as mco

    _clear_base(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mco, "_is_interactive", lambda: True)

    gateway = _GatewayForwarder()
    try:
        monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_BASE", gateway.base)

        cfg = {"cimd": False}
        storage = mco.HermesTokenStorage("srv")
        port = mco._configure_callback_port(cfg, storage)
        registered = [
            str(u) for u in mco._build_client_metadata(cfg).redirect_uris
        ]

        public = registered[0]
        assert public.startswith(gateway.base + "/oauth/cb/")
        assert f"http://127.0.0.1:{port}/callback" in registered

        # The deployment registers fid -> local port with its gateway; here
        # that is the dict the forwarder consults.
        gateway.routes[urlparse(public).path.rsplit("/", 1)[-1]] = port

        def _drive():
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    urllib.request.urlopen(
                        f"{public}?code=THE-CODE&state=st-1", timeout=5
                    ).read()
                    return
                except OSError:
                    time.sleep(0.01)

        threading.Thread(target=_drive, daemon=True).start()

        waiter = mco._make_callback_waiter(port, timeout=20.0)
        result = asyncio.get_event_loop().run_until_complete(waiter())
    finally:
        gateway.close()

    code = result[0] if isinstance(result, tuple) else result.code
    state = result[1] if isinstance(result, tuple) else result.state
    assert code == "THE-CODE"
    assert state == "st-1"
