from __future__ import annotations

import base64
import hashlib
import json
import os
import stat

import httpx
import pytest

from everyday_receipts.auth import (
    ApigeeTokens,
    Auth0Tokens,
    AuthError,
    AuthManager,
    ReloginRequired,
    TokenStore,
    build_authorize_url,
    generate_pkce,
    parse_redirect,
)

from conftest import Recorder, make_client


def test_pkce_pair_is_valid():
    verifier, challenge = generate_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_build_authorize_url(settings):
    url = build_authorize_url(settings, state="st", code_challenge="ch", redirect_uri="https://www.everyday.com.au/callback")
    assert url.startswith("https://auth.everyday.com.au/authorize?")
    assert "client_id=sOyZPtybxGPItZdk4kCOqro8DU1VeuTw" in url
    assert "scope=openid+profile+offline_access" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=https%3A%2F%2Fwww.everyday.com.au%2Fcallback" in url


def test_parse_redirect_variants():
    assert parse_redirect("https://www.everyday.com.au/callback?code=abc&state=xyz") == ("abc", "xyz")
    assert parse_redirect("https://www.everyday.com.au/callback#code=abc&state=xyz") == ("abc", "xyz")
    assert parse_redirect("?code=abc&state=xyz") == ("abc", "xyz")
    assert parse_redirect("/callback?code=abc") == ("abc", None)
    assert parse_redirect("rawcode") == ("rawcode", None)
    with pytest.raises(ValueError, match="access_denied"):
        parse_redirect("https://www.everyday.com.au/callback?error=access_denied&error_description=nope")
    with pytest.raises(ValueError):
        parse_redirect("https://www.everyday.com.au/callback?foo=bar")
    with pytest.raises(ValueError):
        parse_redirect("   ")


def _auth0_and_exchange_handler(state: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.everyday.com.au" and request.url.path == "/oauth/token":
            form = {k: v for k, v in (p.split("=", 1) for p in request.content.decode().split("&"))}
            state["grants"].append(form["grant_type"])
            if form["grant_type"] == "refresh_token" and state.get("refresh_rejected"):
                return httpx.Response(403, json={"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
            body = {"access_token": f"A0-{len(state['grants'])}", "expires_in": 86400, "scope": "openid profile offline_access", "token_type": "Bearer"}
            if form["grant_type"] == "authorization_code" or state.get("rotate"):
                body["refresh_token"] = f"RT-{len(state['grants'])}"
            return httpx.Response(200, json=body)
        if request.url.path == "/wx/v1/rewardspartner/secure/token-exchange":
            assert request.headers["client_id"] == "eAjOrRlfHIyqpK1KVX8UlmmCFvfmoGXY"
            assert request.headers["api-version"] == "2"
            payload = json.loads(request.content)
            state["exchanged"].append(payload["access_token"])
            return httpx.Response(200, json={"data": {"accessToken": f"AP-{payload['access_token']}", "accessTokenExpiresIn": "1800"}})
        return httpx.Response(404, text="unexpected " + str(request.url))

    return handler


def test_pkce_login_then_bearer_refresh(settings):
    st = {"grants": [], "exchanged": [], "rotate": True}
    now = {"t": 1_000_000.0}
    rec = Recorder()
    http = make_client(_auth0_and_exchange_handler(st), rec)
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])

    auth.complete_pkce_login("CODE", "VERIFIER", "https://www.everyday.com.au/callback")
    assert rec.form_body(0) == {
        "grant_type": "authorization_code",
        "client_id": settings.auth0_client_id,
        "code": "CODE",
        "code_verifier": "VERIFIER",
        "redirect_uri": "https://www.everyday.com.au/callback",
    }
    assert rec.requests[0].headers["Auth0-Client"]
    assert store.mode == "auth0"
    assert store.auth0.refresh_token == "RT-1"
    assert store.apigee.access_token == "AP-A0-1"
    assert stat.S_IMODE(os.stat(settings.token_path).st_mode) == 0o600

    # Bearer still fresh: no network.
    before = len(rec.requests)
    assert auth.get_bearer() == "AP-A0-1"
    assert len(rec.requests) == before

    # Bearer expired but Auth0 access token still valid: only a token exchange happens.
    now["t"] += 1800
    assert auth.get_bearer() == "AP-A0-1"  # exchange of the same Auth0 token
    assert st["grants"] == ["authorization_code"]
    assert st["exchanged"] == ["A0-1", "A0-1"]

    # Auth0 token expired too: refresh grant, rotated refresh token persisted, then exchange.
    now["t"] += 86400
    assert auth.get_bearer() == "AP-A0-2"
    assert st["grants"] == ["authorization_code", "refresh_token"]
    assert rec.form_body(len(rec.requests) - 2)["refresh_token"] == "RT-1"
    reloaded = TokenStore.load(settings.token_path)
    assert reloaded.auth0.refresh_token == "RT-2"
    assert reloaded.apigee.access_token == "AP-A0-2"

    # A forced refresh (after a 401) always goes back through Auth0.
    assert auth.get_bearer(force_refresh=True) == "AP-A0-3"


def test_refresh_without_rotation_keeps_old_refresh_token(settings):
    st = {"grants": [], "exchanged": [], "rotate": False}
    now = {"t": 5000.0}
    http = make_client(_auth0_and_exchange_handler(st))
    store = TokenStore(mode="auth0", path=settings.token_path)
    store.auth0 = Auth0Tokens(access_token="old", expires_at=now["t"] - 1, refresh_token="RT-keep")
    store.apigee = ApigeeTokens(access_token="dead", expires_at=now["t"] - 1)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])
    assert auth.get_bearer() == "AP-A0-1"
    assert store.auth0.refresh_token == "RT-keep"


def test_rejected_refresh_token_requires_relogin(settings):
    st = {"grants": [], "exchanged": [], "refresh_rejected": True}
    now = {"t": 5000.0}
    http = make_client(_auth0_and_exchange_handler(st))
    store = TokenStore(mode="auth0", path=settings.token_path)
    store.auth0 = Auth0Tokens(access_token="old", expires_at=now["t"] - 1, refresh_token="RT-x")
    store.apigee = ApigeeTokens(access_token="dead", expires_at=now["t"] - 1)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])
    with pytest.raises(ReloginRequired):
        auth.get_bearer()


def test_no_credentials_requires_login(settings):
    http = make_client(lambda r: httpx.Response(500))
    auth = AuthManager(settings, TokenStore(path=settings.token_path), http)
    with pytest.raises(ReloginRequired):
        auth.get_bearer()


def test_import_session_and_apigee_refresh(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/wx/v2/security/refreshToken":
            assert request.headers["client_id"] == settings.rewards_client_id
            payload = json.loads(request.content)
            expected_bearer = "OLDBEARER" if payload.get("refresh_token") == "R1" else "NEWBEARER"
            assert request.headers["Authorization"] == f"Bearer {expected_bearer}"
            if payload.get("refresh_token") == "BAD":
                return httpx.Response(401, json={"errors": [{"status": 401, "code": "1008", "message": "Refresh Token Invalid"}]})
            return httpx.Response(200, json={"data": {"bearer": "NEWBEARER", "bearerExpiredInSeconds": 1800, "refresh": "R2", "refreshExpiredInSeconds": 38879999}})
        return httpx.Response(404)

    now = {"t": 100_000.0}
    http = make_client(handler)
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])
    auth.import_auth_status(json.dumps({
        "reason": "AUTHENTICATED", "access_token": "OLDBEARER", "expires_in": 1800,
        "refresh_token": "R1", "refresh_token_expires_in": 38879999, "accessTokenExpired": "N",
    }))
    assert store.mode == "apigee"
    assert auth.get_bearer() == "OLDBEARER"
    now["t"] += 1800
    assert auth.get_bearer() == "NEWBEARER"
    assert json.loads(calls[-1].content) == {"refresh_token": "R1"}
    assert store.apigee.refresh_token == "R2"
    assert store.apigee.refresh_expires_at == pytest.approx(now["t"] + 38879999)

    store.apigee.refresh_token = "BAD"
    now["t"] += 1800
    with pytest.raises(ReloginRequired):
        auth.get_bearer()


def test_import_session_rejects_garbage(settings):
    auth = AuthManager(settings, TokenStore(path=settings.token_path), make_client(lambda r: httpx.Response(500)))
    with pytest.raises(AuthError):
        auth.import_auth_status("not json")
    with pytest.raises(AuthError):
        auth.import_auth_status(json.dumps({"reason": "x"}))


def test_import_session_without_refresh_token_then_expiry(settings):
    now = {"t": 0.0}
    auth = AuthManager(settings, TokenStore(path=settings.token_path), make_client(lambda r: httpx.Response(500)), clock=lambda: now["t"])
    auth.import_auth_status(json.dumps({"access_token": "T", "expires_in": 600}))
    assert auth.get_bearer() == "T"
    now["t"] += 600
    with pytest.raises(ReloginRequired):
        auth.get_bearer()


def test_static_token_mode(settings):
    auth = AuthManager(settings, TokenStore(path=settings.token_path), make_client(lambda r: httpx.Response(500)))
    auth.use_static_token("STATIC")
    assert auth.get_bearer() == "STATIC"
    with pytest.raises(ReloginRequired):
        auth.get_bearer(force_refresh=True)


def test_token_store_roundtrip(tmp_path):
    path = tmp_path / "t.json"
    store = TokenStore(mode="auth0", path=path)
    store.auth0 = Auth0Tokens(access_token="a", expires_at=1.0, refresh_token="r", scope="openid")
    store.apigee = ApigeeTokens(access_token="b", expires_at=2.0)
    store.save()
    loaded = TokenStore.load(path)
    assert loaded.mode == "auth0"
    assert loaded.auth0 == store.auth0
    assert loaded.apigee == store.apigee
    loaded.clear()
    assert TokenStore.load(path).mode == "none"
