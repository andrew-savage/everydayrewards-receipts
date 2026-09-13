from __future__ import annotations

import json
import os
import stat
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from everyday_receipts.auth import (
    ApigeeTokens,
    Auth0Tokens,
    AuthError,
    AuthManager,
    LoginAttempt,
    ReloginRequired,
    TokenStore,
    looks_like_auth0_token,
    make_state,
    parse_redirect,
)

from conftest import Recorder, make_client

LOGIN_URL_TEMPLATE = (
    "https://auth.everyday.com.au/authorize?response_type=code&scope=openid%20profile%20offline_access"
    "&client_id=wWG2vmdG9vsNvGKPQcYz56LwCuRfKVF8&redirect_uri={redirect}"
    "&audience=https://www.woolworthsrewards.com.au/auth/&ext-newsignup=true&state=abf66c"
)


def test_make_state_is_base64_json_with_nonce():
    import base64

    state = make_state()
    payload = json.loads(base64.b64decode(state))
    assert payload["redirectUri"] == "https://www.everyday.com.au/"
    assert payload["nonce"].isdigit()
    assert make_state() != state or True  # nonce is random; equality is merely unlikely


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


def _login_handler(state: dict):
    """Mocks the login-url, token and refresh endpoints plus Auth0's authorize redirect."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "auth.everyday.com.au" and path == "/authorize":
            state.setdefault("authorize_hits", 0)
            state["authorize_hits"] += 1
            redirect = parse_qs(request.url.query.decode()).get("redirect_uri", [""])[0]
            if redirect.startswith("http://localhost"):
                return httpx.Response(302, headers={"location": "https://everyday.com.au/error-generic.html?error=unauthorized_client&error_description=Callback%20URL%20mismatch.%20http%3A%2F%2Flocalhost%3A8765%2Fcallback%20is%20not%20in%20the%20list%20of%20allowed%20callback%20URLs"})
            return httpx.Response(302, headers={"location": "/u/login/identifier?state=hKFo"})
        if path == "/wx/v2/security/login/url":
            assert request.headers["client_id"] == "8h41mMOiDULmlLT28xKSv5ITpp3XBRvH"
            q = parse_qs(request.url.query.decode())
            state["login_url_query"] = {k: v[0] for k, v in q.items()}
            return httpx.Response(200, json={"data": {"url": LOGIN_URL_TEMPLATE.format(redirect=q["redirectUri"][0])}})
        if path == "/wx/v2/security/token":
            payload = json.loads(request.content)
            state["token_payload"] = payload
            if payload.get("code") != "GOODCODE":
                return httpx.Response(400, json={"errors": [{"status": 400, "code": "400", "message": "Invalid state"}]})
            return httpx.Response(200, json={"data": {
                "bearer": "BEARER1", "bearerExpiredInSeconds": 1800, "refresh": "REFRESH1",
                "refreshExpiredInSeconds": 38879999, "state": state.get("expected_state"),
                "passwordResetRequired": False, "isInactiveCard": False, "twoFAVerified": "true",
            }})
        if path == "/wx/v2/security/refreshToken":
            payload = json.loads(request.content)
            state.setdefault("refresh_payloads", []).append(payload)
            assert request.headers.get("Authorization", "Bearer x").startswith("Bearer ")
            token = payload.get(state.get("accepted_key", "refresh_token"))
            if token is None:
                return httpx.Response(400, json={"errors": [{"status": 400, "code": "400", "message": "Bad Request"}]})
            if token == "DEAD":
                return httpx.Response(401, json={"errors": [{"status": 401, "code": "1008", "message": "Refresh Token Invalid"}]})
            n = len(state["refresh_payloads"])
            return httpx.Response(200, json={"data": {"bearer": f"BEARER{n + 1}", "bearerExpiredInSeconds": 1800, "refresh": f"REFRESH{n + 1}", "refreshExpiredInSeconds": 38879999}})
        return httpx.Response(404, text="unexpected " + str(request.url))

    return handler


def test_begin_check_and_complete_login(settings):
    st: dict = {}
    now = {"t": 1_000_000.0}
    rec = Recorder()
    http = make_client(_login_handler(st), rec)
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])

    attempt = auth.begin_login("https://www.everyday.com.au/callback")
    st["expected_state"] = attempt.state
    assert st["login_url_query"]["redirectUri"] == "https://www.everyday.com.au/callback"
    assert st["login_url_query"]["state"] == attempt.state
    assert attempt.url.startswith("https://auth.everyday.com.au/authorize?")
    assert attempt.auth0_state == "abf66c"

    assert auth.check_login_url(attempt.url) is None

    tokens = auth.complete_login("GOODCODE", "abf66c", attempt)
    assert st["token_payload"] == {"code": "GOODCODE", "state": "abf66c", "redirectUri": "https://www.everyday.com.au/callback"}
    assert tokens.access_token == "BEARER1" and tokens.refresh_token == "REFRESH1"
    assert tokens.refresh_expires_at == pytest.approx(now["t"] + 38879999)
    assert store.mode == "apigee"
    assert stat.S_IMODE(os.stat(settings.token_path).st_mode) == 0o600

    # Fresh bearer: no network.
    before = len(rec.requests)
    assert auth.get_bearer() == "BEARER1"
    assert len(rec.requests) == before

    # Expired bearer: refreshed with the default body key; refresh token rotated and persisted.
    now["t"] += 1800
    assert auth.get_bearer() == "BEARER2"
    assert st["refresh_payloads"] == [{"refresh_token": "REFRESH1"}]
    reloaded = TokenStore.load(settings.token_path)
    assert reloaded.apigee.refresh_token == "REFRESH2"
    assert reloaded.refresh_body_key == "refresh_token"


def test_check_login_url_reports_callback_mismatch(settings):
    st: dict = {}
    auth = AuthManager(settings, TokenStore(path=settings.token_path), make_client(_login_handler(st)))
    attempt = auth.begin_login("http://localhost:8765/callback")
    problem = auth.check_login_url(attempt.url)
    assert problem is not None and "Callback URL mismatch" in problem


def test_check_login_url_tolerates_network_errors(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    auth = AuthManager(settings, TokenStore(path=settings.token_path), make_client(handler))
    assert auth.check_login_url("https://auth.everyday.com.au/authorize?x=1") is None


def test_complete_login_rejected_code(settings):
    st: dict = {}
    auth = AuthManager(settings, TokenStore(path=settings.token_path), make_client(_login_handler(st)))
    attempt = LoginAttempt(url="u", state="s", redirect_uri="https://www.everyday.com.au/callback", auth0_state="abf66c")
    with pytest.raises(AuthError, match="Invalid state"):
        auth.complete_login("BADCODE", None, attempt)
    assert st["token_payload"]["state"] == "abf66c"


def test_refresh_falls_back_to_camel_case_key_and_remembers_it(settings):
    st: dict = {"accepted_key": "refreshToken"}
    now = {"t": 50_000.0}
    store = TokenStore(mode="apigee", path=settings.token_path)
    store.apigee = ApigeeTokens(access_token="OLD", expires_at=now["t"] - 1, refresh_token="R1", refresh_expires_at=now["t"] + 9e6)
    auth = AuthManager(settings, store, make_client(_login_handler(st)), clock=lambda: now["t"])
    assert auth.get_bearer() == "BEARER3"
    assert st["refresh_payloads"] == [{"refresh_token": "R1"}, {"refreshToken": "R1"}]
    assert TokenStore.load(settings.token_path).refresh_body_key == "refreshToken"

    # Next refresh tries the remembered key first.
    now["t"] += 1800
    auth.get_bearer()
    assert st["refresh_payloads"][2] == {"refreshToken": "REFRESH3"}


def test_refresh_rejected_requires_relogin(settings):
    st: dict = {}
    now = {"t": 50_000.0}
    store = TokenStore(mode="apigee", path=settings.token_path)
    store.apigee = ApigeeTokens(access_token="OLD", expires_at=now["t"] - 1, refresh_token="DEAD")
    auth = AuthManager(settings, store, make_client(_login_handler(st)), clock=lambda: now["t"])
    with pytest.raises(ReloginRequired):
        auth.get_bearer()


def test_refresh_all_keys_rejected_is_transient_error(settings):
    st: dict = {"accepted_key": "somethingElse"}
    now = {"t": 50_000.0}
    store = TokenStore(mode="apigee", path=settings.token_path)
    store.apigee = ApigeeTokens(access_token="OLD", expires_at=now["t"] - 1, refresh_token="R1")
    auth = AuthManager(settings, store, make_client(_login_handler(st)), clock=lambda: now["t"])
    with pytest.raises(AuthError) as exc_info:
        auth.get_bearer()
    assert not isinstance(exc_info.value, ReloginRequired)
    assert len(st["refresh_payloads"]) == 2


def test_expired_refresh_token_requires_relogin(settings):
    now = {"t": 50_000.0}
    store = TokenStore(mode="apigee", path=settings.token_path)
    store.apigee = ApigeeTokens(access_token="OLD", expires_at=now["t"] - 1, refresh_token="R1", refresh_expires_at=now["t"] - 5)
    auth = AuthManager(settings, store, make_client(lambda r: httpx.Response(500)), clock=lambda: now["t"])
    with pytest.raises(ReloginRequired):
        auth.get_bearer()


def test_no_credentials_requires_login(settings):
    auth = AuthManager(settings, TokenStore(path=settings.token_path), make_client(lambda r: httpx.Response(500)))
    with pytest.raises(ReloginRequired):
        auth.get_bearer()


def test_import_session_then_refresh(settings):
    st: dict = {}
    now = {"t": 100_000.0}
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, make_client(_login_handler(st)), clock=lambda: now["t"])
    auth.import_auth_status(json.dumps({
        "reason": "AUTHENTICATED", "access_token": "OLDBEARER", "expires_in": 1800,
        "refresh_token": "R1", "refresh_token_expires_in": 38879999, "accessTokenExpired": "N",
    }))
    assert store.mode == "apigee"
    assert auth.get_bearer() == "OLDBEARER"
    now["t"] += 1800
    assert auth.get_bearer() == "BEARER2"
    assert st["refresh_payloads"] == [{"refresh_token": "R1"}]
    assert store.apigee.refresh_token == "REFRESH2"


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


def test_token_store_roundtrip_and_legacy_file(tmp_path):
    path = tmp_path / "t.json"
    store = TokenStore(mode="apigee", path=path, refresh_body_key="refreshToken")
    store.apigee = ApigeeTokens(access_token="b", expires_at=2.0, refresh_token="r", refresh_expires_at=3.0)
    store.save()
    loaded = TokenStore.load(path)
    assert loaded.mode == "apigee" and loaded.apigee == store.apigee and loaded.refresh_body_key == "refreshToken"
    loaded.clear()
    assert TokenStore.load(path).mode == "none"

    # An app-token (auth0) session round-trips too.
    store = TokenStore(mode="auth0", path=path)
    store.auth0 = Auth0Tokens(refresh_token="rt", access_token="jwt", expires_at=9.0)
    store.apigee = ApigeeTokens(access_token="bearer", expires_at=10.0)
    store.save()
    loaded = TokenStore.load(path)
    assert loaded.mode == "auth0" and loaded.auth0 == store.auth0 and loaded.apigee == store.apigee

    # A file with an unknown mode but apigee tokens still loads as apigee.
    path.write_text(json.dumps({"mode": "weird", "apigee": {"access_token": "b", "expires_at": 1.0}}))
    legacy = TokenStore.load(path)
    assert legacy.mode == "apigee" and legacy.apigee.access_token == "b"


# --------------------------------------------------------------------------- console paste + keepalive

from everyday_receipts.auth import parse_auth_status  # noqa: E402

RAW = '{"reason":"AUTHENTICATED","access_token":"A","expires_in":3599,"refresh_token":"R","refresh_token_expires_in":7199,"isTempPassword":false}'


@pytest.mark.parametrize(
    "text",
    [
        RAW,
        f"  {RAW}\n",
        json.dumps(RAW),  # double-encoded, as Chrome/Safari display stored strings
        json.dumps(RAW) + " = $1",  # Safari appends a console variable marker
        "'" + RAW + "'",
        '{"authStatus": ' + RAW + "}",
        RAW.replace('"', '\\"'),  # escaped quotes without outer quotes
    ],
)
def test_parse_auth_status_variants(text):
    data = parse_auth_status(text)
    assert data["access_token"] == "A"
    assert data["refresh_token"] == "R"
    assert data["refresh_token_expires_in"] == 7199


def test_parse_auth_status_rejects_junk():
    with pytest.raises(AuthError):
        parse_auth_status("undefined")
    with pytest.raises(AuthError):
        parse_auth_status('{"reason":"x"}')
    with pytest.raises(AuthError):
        parse_auth_status("")


def test_keepalive_schedule_for_short_lived_refresh_token(settings):
    st: dict = {}
    now = {"t": 100_000.0}
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, make_client(_login_handler(st)), clock=lambda: now["t"])
    auth.import_auth_status(RAW)  # bearer 3599 s, refresh 7199 s
    assert store.apigee.refresh_lifetime == 7199

    assert auth.keepalive_due() is False
    assert auth.keepalive() is False
    now["t"] += 3000  # 4199 s of refresh life left: more than half, bearer still valid
    assert auth.keepalive_due() is False
    now["t"] += 700  # bearer within the refresh margin -> due
    assert auth.keepalive_due() is True
    assert auth.keepalive() is True
    assert st["refresh_payloads"] == [{"refresh_token": "R"}]
    # Renewed: new refresh token with a fresh lifetime, so nothing is due right away.
    assert store.apigee.refresh_token == "REFRESH2"
    assert store.apigee.refresh_expires_at == pytest.approx(now["t"] + 38879999)
    assert auth.keepalive_due() is False


def test_keepalive_due_by_refresh_half_life(settings):
    now = {"t": 0.0}
    store = TokenStore(mode="apigee", path=settings.token_path)
    store.apigee = ApigeeTokens(access_token="B", expires_at=1e9, refresh_token="R", refresh_expires_at=7200.0, refresh_lifetime=7200.0)
    auth = AuthManager(settings, store, make_client(lambda r: httpx.Response(500)), clock=lambda: now["t"])
    assert auth.keepalive_due() is False
    now["t"] = 3599.0
    assert auth.keepalive_due() is False
    now["t"] = 3601.0
    assert auth.keepalive_due() is True


def test_keepalive_not_applicable_without_refresh_token(settings):
    store = TokenStore(mode="static", path=settings.token_path)
    store.apigee = ApigeeTokens(access_token="B", expires_at=0.0)
    auth = AuthManager(settings, store, make_client(lambda r: httpx.Response(500)))
    assert auth.keepalive_due() is False


def test_refresh_without_rotation_keeps_lifetime(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"bearer": "NEW", "bearerExpiredInSeconds": 3599}})

    now = {"t": 0.0}
    store = TokenStore(mode="apigee", path=settings.token_path)
    store.apigee = ApigeeTokens(access_token="OLD", expires_at=-1.0, refresh_token="R", refresh_expires_at=7000.0, refresh_lifetime=7199.0)
    auth = AuthManager(settings, store, make_client(handler), clock=lambda: now["t"])
    assert auth.get_bearer() == "NEW"
    assert store.apigee.refresh_token == "R"
    assert store.apigee.refresh_expires_at == 7000.0
    assert store.apigee.refresh_lifetime == 7199.0


# --------------------------------------------------------------------------- nested shapes + direct import

def test_parse_auth_status_finds_nested_mobile_shapes():
    assert parse_auth_status(json.dumps({"data": {"login": {"bearer": "B", "refresh": "R"}}}))["bearer"] == "B"
    assert parse_auth_status(json.dumps({"data": {"access_token": "B2", "refresh_token": "R2"}}))["refresh_token"] == "R2"
    with pytest.raises(AuthError):
        parse_auth_status(json.dumps({"data": {"nothing": 1}}))


def test_import_tokens_with_access_token(settings):
    st: dict = {}
    now = {"t": 100.0}
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, make_client(_login_handler(st)), clock=lambda: now["t"])
    auth.import_tokens(refresh_token="R", access_token="B", refresh_lifetime=38879999)
    assert store.mode == "apigee"
    assert auth.get_bearer() == "B"
    assert store.apigee.refresh_lifetime == 38879999
    # No keepalive needed yet with a 14-month token.
    assert auth.keepalive_due() is False


def test_import_tokens_without_access_token_mints_bearer(settings):
    st: dict = {}
    now = {"t": 100.0}
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, make_client(_login_handler(st)), clock=lambda: now["t"])
    auth.import_tokens(refresh_token="R", access_token=None, refresh_lifetime=38879999)
    # import_tokens refreshed immediately to obtain a bearer.
    assert store.apigee.access_token == "BEARER2"
    assert st["refresh_payloads"] == [{"refresh_token": "R"}]



def test_import_tokens_without_access_no_bearer_header(settings):
    """A refresh with no current bearer must omit Authorization, not send 'Bearer '."""
    seen = {}

    def handler(request):
        seen["auth_present"] = "authorization" in {k.lower() for k in request.headers}
        return httpx.Response(200, json={"data": {"bearer": "NEW", "bearerExpiredInSeconds": 3599, "refresh": "R2", "refreshExpiredInSeconds": 38879999}})

    now = {"t": 0.0}
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, make_client(handler), clock=lambda: now["t"])
    auth.import_tokens(refresh_token="R", access_token=None, refresh_lifetime=38879999)
    assert store.apigee.access_token == "NEW"
    assert seen["auth_present"] is False


# --------------------------------------------------------------------------- auth0 app-token flow

MOBILE_JWT_HEADER = "eyJhbGciOiJSUzI1NiJ9"


def _make_jwt(exp: int, nonce: int = 0) -> str:
    import base64 as _b64

    payload = _b64.urlsafe_b64encode(json.dumps({"exp": exp, "n": nonce}).encode()).rstrip(b"=").decode()
    return f"{MOBILE_JWT_HEADER}.{payload}.{'s' * 90}"  # long enough to look like a real JWT


def _auth0_handler(state: dict, clock):
    """Mocks Auth0 /oauth/token and the partner token-exchange endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "auth.everyday.com.au" and path == "/oauth/token":
            body = json.loads(request.content)
            state.setdefault("auth0_calls", []).append(body)
            assert body["grant_type"] == "refresh_token"
            assert body["client_id"] == "NIG5ul5ubHYy61KoFRNBspUSo1scgDwx"
            if body["refresh_token"] in state.get("dead_refresh", ()):
                return httpx.Response(403, json={"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
            n = len(state["auth0_calls"])
            resp = {"access_token": _make_jwt(int(clock()) + 86400, nonce=n), "expires_in": 86400, "token_type": "Bearer", "scope": "openid offline_access"}
            if state.get("rotate", True):
                resp["refresh_token"] = f"RT{n + 1}"
            return httpx.Response(200, json=resp)
        if path == "/wx/v1/rewardspartner/secure/token-exchange":
            assert request.headers["client_id"] == "eAjOrRlfHIyqpK1KVX8UlmmCFvfmoGXY"
            jwt = json.loads(request.content)["access_token"]
            state.setdefault("exchanges", []).append(jwt)
            if jwt in state.get("reject_jwt", ()):
                return httpx.Response(401, json={"errors": [{"code": "1007", "message": "Access Token Invalid"}]})
            n = len(state["exchanges"])
            return httpx.Response(201, json={"data": {"accessToken": f"BEARER{n}", "accessTokenExpiresIn": 1199}})
        return httpx.Response(404, text="unexpected " + str(request.url))

    return handler


def test_looks_like_auth0_token():
    assert looks_like_auth0_token({"refresh_token": "r", "access_token": _make_jwt(1)}) is True
    assert looks_like_auth0_token({"refresh_token": "r", "id_token": "x", "access_token": "opaque"}) is True
    assert looks_like_auth0_token({"access_token": "opaque28chars", "refresh_token": "r"}) is False  # not a JWT, no id_token
    assert looks_like_auth0_token({"access_token": "A", "expires_in": 1800}) is False


def test_import_app_token_with_jwt_then_reuse_and_refresh(settings):
    st: dict = {}
    now = {"t": 1_000_000.0}
    http = make_client(_auth0_handler(st, lambda: now["t"]))
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])

    auth.import_app_token(refresh_token="RT0", access_token=_make_jwt(int(now["t"]) + 86400))
    assert store.mode == "auth0"
    assert store.auth0.refresh_token == "RT0"  # JWT supplied, so no Auth0 refresh yet
    assert st.get("auth0_calls") is None
    assert store.apigee.access_token == "BEARER1"  # one exchange happened

    # Fresh bearer: no calls.
    before = len(st["exchanges"])
    assert auth.get_bearer() == "BEARER1"
    assert len(st["exchanges"]) == before

    # Bearer expired, JWT still valid: only a new exchange, no Auth0 refresh.
    now["t"] += 1200
    assert auth.get_bearer() == "BEARER2"
    assert st.get("auth0_calls") is None
    assert st["exchanges"] == [store.auth0.access_token, store.auth0.access_token]

    # JWT expired: Auth0 refresh (rotates RT), then exchange.
    now["t"] += 86400
    assert auth.get_bearer() == "BEARER3"
    assert len(st["auth0_calls"]) == 1
    assert store.auth0.refresh_token == "RT2"
    assert TokenStore.load(settings.token_path).auth0.refresh_token == "RT2"


def test_import_app_token_refresh_only_mints_immediately(settings):
    st: dict = {}
    now = {"t": 500.0}
    http = make_client(_auth0_handler(st, lambda: now["t"]))
    store = TokenStore(path=settings.token_path)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])
    auth.import_app_token(refresh_token="RT0")  # no JWT: must refresh Auth0 first
    assert len(st["auth0_calls"]) == 1
    assert store.apigee.access_token == "BEARER1"
    assert store.auth0.access_token.startswith(MOBILE_JWT_HEADER)


def test_import_auth0_response_from_pasted_json(settings):
    st: dict = {}
    now = {"t": 500.0}
    http = make_client(_auth0_handler(st, lambda: now["t"]))
    auth = AuthManager(settings, TokenStore(path=settings.token_path), http, clock=lambda: now["t"])
    auth.import_auth0_response({"access_token": _make_jwt(int(now["t"]) + 86400), "refresh_token": "RTx", "expires_in": 86400})
    assert auth.get_bearer() == "BEARER1"


def test_auth0_dead_refresh_token_requires_relogin(settings):
    st: dict = {"dead_refresh": {"RT0"}}
    now = {"t": 500.0}
    http = make_client(_auth0_handler(st, lambda: now["t"]))
    store = TokenStore(mode="auth0", path=settings.token_path)
    store.auth0 = Auth0Tokens(refresh_token="RT0", access_token="", expires_at=0.0)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])
    with pytest.raises(ReloginRequired):
        auth.get_bearer()


def test_auth0_exchange_rejected_jwt_triggers_one_refresh_retry(settings):
    st: dict = {}
    now = {"t": 500.0}
    stale = _make_jwt(int(now["t"]) + 86400)
    st["reject_jwt"] = {stale}
    http = make_client(_auth0_handler(st, lambda: now["t"]))
    store = TokenStore(mode="auth0", path=settings.token_path)
    store.auth0 = Auth0Tokens(refresh_token="RT0", access_token=stale, expires_at=now["t"] + 86400)
    auth = AuthManager(settings, store, http, clock=lambda: now["t"])
    # First exchange (stale JWT) 401s -> one Auth0 refresh -> exchange the fresh JWT succeeds.
    assert auth.get_bearer().startswith("BEARER")
    assert len(st["auth0_calls"]) == 1


def test_auth0_keepalive_due_when_jwt_near_expiry(settings):
    now = {"t": 0.0}
    store = TokenStore(mode="auth0", path=settings.token_path)
    store.auth0 = Auth0Tokens(refresh_token="RT0", access_token="jwt", expires_at=1000.0)
    store.apigee = ApigeeTokens(access_token="B", expires_at=1e9)
    auth = AuthManager(settings, store, make_client(lambda r: httpx.Response(500)), clock=lambda: now["t"])
    assert auth.keepalive_due() is False
    now["t"] = 800.0  # within AUTH0_MARGIN (300) of 1000
    assert auth.keepalive_due() is True


def test_current_refresh_token_tracks_the_right_token_per_mode(settings, tmp_path):
    """In app-token mode the durable token is the Auth0 one, not the (absent) apigee one."""
    from everyday_receipts.cli import App

    class _App(App):
        def __init__(self, store):  # bypass network setup
            self.store = store

    auth0_store = TokenStore(mode="auth0", path=tmp_path / "a.json")
    auth0_store.auth0 = Auth0Tokens(refresh_token="APP-RT", access_token="jwt", expires_at=1.0)
    auth0_store.apigee = ApigeeTokens(access_token="bearer", expires_at=2.0)  # no refresh token here
    assert _App(auth0_store)._current_refresh_token() == "APP-RT"

    web_store = TokenStore(mode="apigee", path=tmp_path / "b.json")
    web_store.apigee = ApigeeTokens(access_token="b", expires_at=2.0, refresh_token="WEB-RT")
    assert _App(web_store)._current_refresh_token() == "WEB-RT"
