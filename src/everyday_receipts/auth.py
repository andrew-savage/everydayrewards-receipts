"""Authentication against Everyday Rewards.

The reliable, unattended path uses the mobile app's Auth0 session (mode ``auth0``):

* The Everyday Rewards app is a standard Auth0 native client. Its refresh token is
  long-lived and renews an Auth0 access token (a JWT) at ``auth.everyday.com.au/oauth/token``.
* That JWT is swapped for a short-lived API bearer at the ``token-exchange`` endpoint the
  website uses, and the bearer drives the receipt endpoints on ``api.everyday.com.au``.

Two legacy bootstraps remain, useful for a one-off backfill but NOT for unattended use
because the web session cannot be refreshed:

* ``login`` - the website's backend-mediated Auth0 code flow.
* ``import-session`` - the browser's stored ``authStatusData`` bearer + refresh token.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Settings

log = logging.getLogger(__name__)

WEB_ORIGIN = "https://www.everyday.com.au"
REFRESH_BODY_KEYS = ("refresh_token", "refreshToken")
REFRESH_TIMEOUT = 25.0


class AuthError(Exception):
    """Authentication failed; may be transient."""


class ReloginRequired(AuthError):
    """Stored credentials cannot be refreshed any more; a person must supply a new session."""


@dataclass
class ApigeeTokens:
    """A short-lived API bearer (and, for the legacy web session, its refresh token)."""

    access_token: str
    expires_at: float
    refresh_token: str | None = None
    refresh_expires_at: float | None = None
    refresh_lifetime: float | None = None


@dataclass
class Auth0Tokens:
    """The mobile app's Auth0 session: a long-lived refresh token and the current JWT."""

    refresh_token: str
    access_token: str = ""  # the Auth0 JWT (audience = woolworthsrewards auth)
    expires_at: float = 0.0


@dataclass
class TokenStore:
    mode: str = "none"  # none | auth0 | apigee | static
    apigee: ApigeeTokens | None = None
    auth0: Auth0Tokens | None = None
    refresh_body_key: str | None = None  # JSON key the (legacy) refresh endpoint accepted
    updated_at: float = 0.0
    path: Path | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "apigee": vars(self.apigee) if self.apigee else None,
            "auth0": vars(self.auth0) if self.auth0 else None,
            "refresh_body_key": self.refresh_body_key,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> "TokenStore":
        apigee = data.get("apigee")
        auth0 = data.get("auth0")
        mode = data.get("mode", "none")
        if mode not in ("none", "auth0", "apigee", "static"):
            mode = "auth0" if auth0 else ("apigee" if apigee else "none")
        return cls(
            mode=mode,
            apigee=ApigeeTokens(**apigee) if apigee else None,
            auth0=Auth0Tokens(**auth0) if auth0 else None,
            refresh_body_key=data.get("refresh_body_key"),
            updated_at=float(data.get("updated_at") or 0.0),
            path=path,
        )

    @classmethod
    def load(cls, path: Path) -> "TokenStore":
        if not path.exists():
            return cls(path=path)
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8") or "{}"), path)
        except (ValueError, TypeError) as exc:
            raise AuthError(f"token file {path} is corrupt: {exc}") from exc

    def save(self) -> None:
        if self.path is None:
            return
        self.updated_at = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(self.to_dict(), fh, indent=2)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.mode = "none"
        self.apigee = None
        self.auth0 = None
        self.save()


@dataclass
class LoginAttempt:
    url: str
    state: str
    redirect_uri: str
    auth0_state: str | None


# --------------------------------------------------------------------------- helpers


def make_state() -> str:
    payload = {"redirectUri": WEB_ORIGIN + "/", "nonce": str(secrets.randbelow(1000))}
    return base64.b64encode(json.dumps(payload).encode()).decode()


_ACCESS_KEYS = ("bearer", "access_token", "accessToken")
_REFRESH_KEYS = ("refresh", "refresh_token", "refreshToken")


def _looks_like_jwt(value: Any) -> bool:
    return isinstance(value, str) and value.count(".") == 2 and len(value) > 60


def _decode_jwt_exp(jwt: str) -> float | None:
    """Return the 'exp' claim of a JWT, or None if it can't be read."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def _find_token_dict(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        if any(obj.get(k) for k in _ACCESS_KEYS):
            return obj
        for value in obj.values():
            found = _find_token_dict(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_token_dict(value)
            if found is not None:
                return found
    return None


def parse_pasted_json(text: str) -> Any:
    """Parse JSON however a browser console or proxy rendered it (escapes, quotes, ' = $1')."""
    cleaned = text.strip()
    cleaned = re.sub(r"\s*=\s*\$\d+\s*$", "", cleaned)
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "'\"" and not cleaned.startswith('"{'):
        cleaned = cleaned[1:-1]
    for attempt in range(3):
        try:
            data = json.loads(cleaned)
        except ValueError:
            if attempt == 0 and '\\"' in cleaned:
                cleaned = cleaned.replace('\\"', '"')
                cleaned = cleaned.strip("'\"") if not cleaned.startswith("{") else cleaned
                continue
            raise AuthError("that is not valid JSON; paste exactly what was printed") from None
        if isinstance(data, str):
            cleaned = data
            continue
        return data
    return data


def looks_like_auth0_token(data: Any) -> bool:
    """True for a captured Auth0 /oauth/token response (refresh token + JWT access token)."""
    if not isinstance(data, dict):
        return False
    if not any(data.get(k) for k in _REFRESH_KEYS):
        return False
    return bool(data.get("id_token")) or _looks_like_jwt(data.get("access_token"))


def parse_auth_status(text: str) -> dict[str, Any]:
    """Parse a captured web session (``authStatusData`` and nested proxy shapes)."""
    token_dict = _find_token_dict(parse_pasted_json(text))
    if token_dict is None:
        raise AuthError("no access/bearer token found in what you pasted; are you logged in?")
    return token_dict


def parse_redirect(text: str) -> tuple[str, str | None]:
    """Extract (code, state) from a pasted redirect URL, query string, or bare code."""
    text = text.strip()
    if not text:
        raise ValueError("nothing was pasted")
    if "://" in text or text.startswith("/"):
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        if "code" not in query and "error" not in query and parsed.fragment:
            query = parse_qs(parsed.fragment)
    elif "=" in text:
        query = parse_qs(text.lstrip("?#"))
    else:
        return text, None
    if "error" in query:
        raise ValueError(
            f"Auth0 returned an error: {query['error'][0]}: {query.get('error_description', [''])[0]}"
        )
    code = query.get("code", [None])[0]
    if not code:
        raise ValueError("no code= parameter found in what was pasted")
    return code, query.get("state", [None])[0]


def wait_for_callback(host: str, port: int, timeout: float) -> str:
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if "code=" in self.path or "error=" in self.path:
                result["path"] = self.path
                body = b"<html><body><h2>Everyday Receipts: login received. You can close this tab.</h2></body></html>"
            else:
                body = b"<html><body>Waiting for the login redirect...</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = HTTPServer((host, port), Handler)
    server.timeout = 1.0
    deadline = time.time() + timeout
    try:
        while "path" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "path" not in result:
        raise TimeoutError(f"no login redirect arrived on port {port} within {int(timeout)}s")
    return result["path"]


def _json_or_error(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as exc:
        snippet = resp.text.strip().replace("\n", " ")[:200]
        raise AuthError(f"non-JSON response ({resp.status_code}) from {resp.request.url}: {snippet}") from exc
    return body if isinstance(body, dict) else {"data": body}


def _unwrap(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data", body)
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    return data if isinstance(data, dict) else {}


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _error_text(body: dict[str, Any]) -> str:
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(
            f"{e.get('code', '')}: {e.get('message', '')}".strip(": ") for e in errors if isinstance(e, dict)
        )
    if body.get("error"):
        return f"{body.get('error')}: {body.get('error_description', '')}".strip(": ")
    return json.dumps(body)[:300]


# --------------------------------------------------------------------------- manager


class AuthManager:
    REFRESH_MARGIN = 120  # renew a bearer this many seconds before it expires
    AUTH0_MARGIN = 300  # renew the Auth0 JWT this many seconds before it expires

    def __init__(
        self,
        settings: Settings,
        store: TokenStore,
        http: httpx.Client,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.store = store
        self.http = http
        self.clock = clock
        self._lock = threading.RLock()

    # -- headers ----------------------------------------------------------------

    def api_headers(self, client_id: str) -> dict[str, str]:
        return {
            "client_id": client_id,
            "api-version": "2",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/",
            "User-Agent": self.settings.user_agent,
        }

    # -- bearer -----------------------------------------------------------------

    def get_bearer(self, force_refresh: bool = False) -> str:
        with self._lock:
            tokens = self.store.apigee
            if not force_refresh and tokens and tokens.expires_at - self.clock() > self.REFRESH_MARGIN:
                return tokens.access_token
            mode = self.store.mode
            if mode == "static":
                if force_refresh or not tokens:
                    raise ReloginRequired("the static EDR_ACCESS_TOKEN was rejected or expired; supply a new one")
                return tokens.access_token
            if mode == "auth0":
                self._mint_bearer_from_auth0(force_refresh=force_refresh)
            elif mode == "apigee":
                self._refresh_apigee()
            else:
                raise ReloginRequired("no credentials stored; run `everyday-receipts import-app-token` first")
            assert self.store.apigee is not None
            return self.store.apigee.access_token

    # -- imports ----------------------------------------------------------------

    def import_app_token(self, *, refresh_token: str, access_token: str | None = None) -> None:
        """Store the mobile app's Auth0 session and mint the first API bearer."""
        now = self.clock()
        expires_at = 0.0
        if access_token:
            exp = _decode_jwt_exp(access_token)
            expires_at = exp if exp else now + 3600
        with self._lock:
            self.store.mode = "auth0"
            self.store.apigee = None
            self.store.auth0 = Auth0Tokens(
                refresh_token=refresh_token,
                access_token=access_token or "",
                expires_at=expires_at,
            )
            self.store.save()
            self._mint_bearer_from_auth0(force_refresh=not access_token)

    def import_auth0_response(self, data: dict[str, Any]) -> None:
        """Store a captured Auth0 /oauth/token response."""
        refresh = _pick(data, *_REFRESH_KEYS)
        if not refresh:
            raise AuthError("no refresh_token in the captured Auth0 response")
        self.import_app_token(refresh_token=str(refresh), access_token=data.get("access_token"))

    def import_auth_status(self, text: str) -> None:
        """Import the web app's ``authStatusData`` (backfill only; cannot refresh)."""
        data = parse_auth_status(text)
        with self._lock:
            tokens = self._store_apigee_from_response(data, source="import")
        self._warn_about_lifetime(tokens)

    def import_tokens(self, *, refresh_token: str, access_token: str | None, refresh_lifetime: float | None = None) -> None:
        """Legacy: store a directly-supplied apigee refresh token."""
        now = self.clock()
        with self._lock:
            self.store.mode = "apigee"
            self.store.auth0 = None
            self.store.apigee = ApigeeTokens(
                access_token=access_token or "",
                expires_at=now if not access_token else now + 3300,
                refresh_token=refresh_token,
                refresh_expires_at=(now + refresh_lifetime) if refresh_lifetime else None,
                refresh_lifetime=refresh_lifetime,
            )
            self.store.save()
            if not access_token:
                self._refresh_apigee()
            self._warn_about_lifetime(self.store.apigee)

    def use_static_token(self, token: str) -> None:
        with self._lock:
            self.store.mode = "static"
            self.store.auth0 = None
            self.store.apigee = ApigeeTokens(access_token=token, expires_at=self.clock() + 1800)

    def _warn_about_lifetime(self, tokens: ApigeeTokens) -> None:
        if not tokens.refresh_token:
            log.warning(
                "no refresh token in this session; the bearer will stop working in about %d minutes. "
                "This is a web session (backfill only) - for unattended use import an app token.",
                int((tokens.expires_at - self.clock()) // 60),
            )
        elif tokens.refresh_lifetime and tokens.refresh_lifetime < 6 * 3600:
            log.warning(
                "this refresh token lasts only ~%d minutes (a web session, backfill only). For unattended "
                "use run `import-app-token` with a mobile-app token; see the README.",
                int(tokens.refresh_lifetime // 60),
            )

    # -- keepalive --------------------------------------------------------------

    def keepalive_due(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        tokens = self.store.apigee
        if self.store.mode == "auth0":
            a0 = self.store.auth0
            if a0 is None:
                return False
            if a0.expires_at and a0.expires_at - now <= self.AUTH0_MARGIN:
                return True
            return bool(tokens and tokens.expires_at - now <= self.REFRESH_MARGIN)
        if self.store.mode != "apigee" or tokens is None or not tokens.refresh_token:
            return False
        if tokens.expires_at - now <= self.REFRESH_MARGIN:
            return True
        if tokens.refresh_expires_at is None:
            return False
        threshold = max(300.0, (tokens.refresh_lifetime or 0.0) / 2)
        return tokens.refresh_expires_at - now <= threshold

    def keepalive(self) -> bool:
        with self._lock:
            if not self.keepalive_due():
                return False
            self.get_bearer(force_refresh=True)
            return True

    # -- describe ---------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        now = self.clock()
        info: dict[str, Any] = {"mode": self.store.mode}
        if self.store.apigee:
            info["api_bearer_expires_in_s"] = int(self.store.apigee.expires_at - now)
        if self.store.mode == "auth0" and self.store.auth0:
            info["app_refresh_token"] = True
            info["auth0_jwt_expires_in_s"] = int(self.store.auth0.expires_at - now) if self.store.auth0.expires_at else None
        elif self.store.apigee:
            info["api_refresh_token"] = bool(self.store.apigee.refresh_token)
            if self.store.apigee.refresh_expires_at:
                info["api_refresh_expires_in_s"] = int(self.store.apigee.refresh_expires_at - now)
            if self.store.apigee.refresh_lifetime:
                info["api_refresh_lifetime_s"] = int(self.store.apigee.refresh_lifetime)
        if self.store.refresh_body_key:
            info["refresh_body_key"] = self.store.refresh_body_key
        return info

    # -- auth0 flow -------------------------------------------------------------

    def _mint_bearer_from_auth0(self, force_refresh: bool) -> None:
        a0 = self.store.auth0
        if a0 is None:
            raise ReloginRequired("no app session stored; run `everyday-receipts import-app-token`")
        now = self.clock()
        need_jwt = force_refresh or not a0.access_token or (a0.expires_at and a0.expires_at - now <= self.AUTH0_MARGIN)
        if need_jwt:
            self._auth0_refresh()
            a0 = self.store.auth0
            assert a0 is not None
        try:
            self._exchange_auth0_jwt(a0.access_token)
        except _ExchangeUnauthorized:
            # The JWT was rejected; force a fresh one and try once more.
            log.info("token exchange rejected the JWT; refreshing the Auth0 session and retrying")
            self._auth0_refresh()
            a0 = self.store.auth0
            assert a0 is not None
            self._exchange_auth0_jwt(a0.access_token)

    def _auth0_refresh(self) -> None:
        a0 = self.store.auth0
        assert a0 is not None
        url = f"{self.settings.auth0_domain}/oauth/token"
        body = {
            "grant_type": "refresh_token",
            "client_id": self.settings.auth0_app_client_id,
            "refresh_token": a0.refresh_token,
        }
        log.info("refreshing the Auth0 session via %s", url)
        try:
            resp = self.http.post(
                url,
                json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": self.settings.user_agent},
                timeout=REFRESH_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach Auth0: {exc}") from exc
        data = _json_or_error(resp)
        if resp.status_code in (401, 403) or data.get("error") in ("invalid_grant", "unauthorized_client", "access_denied"):
            raise ReloginRequired(f"Auth0 refused the app refresh token ({_error_text(data)}); capture a new app token")
        if resp.status_code != 200 or not data.get("access_token"):
            raise AuthError(f"Auth0 refresh failed ({resp.status_code}): {_error_text(data)}")
        now = self.clock()
        exp = _decode_jwt_exp(data["access_token"]) or (now + int(data.get("expires_in") or 3600))
        # Auth0 rotates the refresh token when rotation is enabled; keep the new one, else the old.
        new_refresh = _pick(data, *_REFRESH_KEYS) or a0.refresh_token
        self.store.auth0 = Auth0Tokens(
            refresh_token=str(new_refresh),
            access_token=str(data["access_token"]),
            expires_at=exp,
        )
        self.store.save()  # persist a rotated refresh token immediately (single-use)

    def _exchange_auth0_jwt(self, jwt: str) -> None:
        if not jwt:
            raise AuthError("no Auth0 JWT to exchange")
        url = f"{self.settings.api_base}/wx/v1/rewardspartner/secure/token-exchange"
        log.info("exchanging the Auth0 JWT for an API bearer")
        try:
            resp = self.http.post(
                url,
                json={"access_token": jwt},
                headers=self.api_headers(self.settings.partner_client_id),
                timeout=REFRESH_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach the token-exchange endpoint: {exc}") from exc
        data = _json_or_error(resp)
        if resp.status_code in (401, 403):
            raise _ExchangeUnauthorized(_error_text(data))
        if resp.status_code not in (200, 201):
            raise AuthError(f"token exchange failed ({resp.status_code}): {_error_text(data)}")
        payload = _unwrap(data)
        access = _pick(payload, "accessToken", "access_token", "bearer")
        ttl = _pick(payload, "accessTokenExpiresIn", "expires_in", "bearerExpiredInSeconds")
        if not access:
            raise AuthError(f"token exchange returned no bearer: {json.dumps(data)[:300]}")
        self.store.apigee = ApigeeTokens(access_token=str(access), expires_at=self.clock() + int(ttl or 1200))
        self.store.save()

    # -- web login flow (backfill only) -----------------------------------------

    def begin_login(self, redirect_uri: str) -> LoginAttempt:
        state = make_state()
        params = {"state": state, "redirectUri": redirect_uri, "newSignup": "true"}
        url = f"{self.settings.security_base}/wx/v2/security/login/url?{urlencode(params)}"
        try:
            resp = self.http.get(url, headers=self.api_headers(self.settings.rewards_client_id))
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach the Everyday Rewards API: {exc}") from exc
        body = _json_or_error(resp)
        if resp.status_code != 200:
            raise AuthError(f"login-url request failed ({resp.status_code}): {_error_text(body)}")
        login_url = _unwrap(body).get("url")
        if not login_url:
            raise AuthError(f"login-url response contained no url: {json.dumps(body)[:300]}")
        auth0_state = parse_qs(urlparse(login_url).query).get("state", [None])[0]
        return LoginAttempt(url=login_url, state=state, redirect_uri=redirect_uri, auth0_state=auth0_state)

    def check_login_url(self, url: str) -> str | None:
        try:
            resp = self.http.get(url, headers={"Accept": "text/html", "User-Agent": self.settings.user_agent}, follow_redirects=False)
        except httpx.HTTPError as exc:
            log.warning("could not pre-check the login URL (%s); continuing anyway", exc)
            return None
        location = resp.headers.get("location", "")
        if "error=" in location:
            query = parse_qs(urlparse(location).query)
            return f"{query.get('error', ['error'])[0]}: {query.get('error_description', [''])[0]}"
        if resp.status_code >= 400:
            return f"Auth0 responded with HTTP {resp.status_code}"
        return None

    def complete_login(self, code: str, callback_state: str | None, attempt: LoginAttempt) -> ApigeeTokens:
        payload = {"code": code, "state": callback_state or attempt.auth0_state or attempt.state, "redirectUri": attempt.redirect_uri}
        url = f"{self.settings.security_base}/wx/v2/security/token"
        try:
            resp = self.http.post(url, json=payload, headers=self.api_headers(self.settings.rewards_client_id))
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach the Everyday Rewards API: {exc}") from exc
        body = _json_or_error(resp)
        if resp.status_code != 200:
            raise AuthError(f"Everyday Rewards rejected the login code ({resp.status_code}): {_error_text(body)}")
        data = _unwrap(body)
        if data.get("passwordResetRequired"):
            log.warning("Everyday Rewards says this account must reset its password; receipts may not load until that is done")
        with self._lock:
            return self._store_apigee_from_response(data, source="login")

    # -- legacy apigee refresh (rarely works) -----------------------------------

    def _store_apigee_from_response(
        self,
        data: dict[str, Any],
        *,
        source: str,
        fallback_refresh: str | None = None,
        fallback_refresh_expires_at: float | None = None,
        fallback_refresh_lifetime: float | None = None,
    ) -> ApigeeTokens:
        access = _pick(data, "bearer", "access_token", "accessToken")
        ttl = _pick(data, "bearerExpiredInSeconds", "expires_in", "accessTokenExpiresIn")
        refresh = _pick(data, "refresh", "refresh_token", "refreshToken")
        refresh_ttl = _pick(data, "refreshExpiredInSeconds", "refresh_token_expires_in", "refreshTokenExpiresIn")
        if not access:
            raise AuthError(f"{source} response had no bearer token: {json.dumps(data)[:300]}")
        now = self.clock()
        tokens = ApigeeTokens(
            access_token=str(access),
            expires_at=now + int(ttl or 1800),
            refresh_token=str(refresh) if refresh else fallback_refresh,
            refresh_expires_at=(now + int(refresh_ttl)) if refresh_ttl else fallback_refresh_expires_at,
            refresh_lifetime=float(refresh_ttl) if refresh_ttl else fallback_refresh_lifetime,
        )
        self.store.mode = "apigee"
        self.store.apigee = tokens
        self.store.save()
        return tokens

    def _refresh_apigee(self) -> None:
        tokens = self.store.apigee
        if tokens is None or not tokens.refresh_token:
            raise ReloginRequired("the stored session has no refresh token; import an app token")
        now = self.clock()
        if tokens.refresh_expires_at and tokens.refresh_expires_at <= now:
            raise ReloginRequired("the refresh token has expired; import a new app token")
        keys: list[str] = []
        for key in (self.store.refresh_body_key, self.settings.apigee_refresh_body_key, *REFRESH_BODY_KEYS):
            if key and key not in keys:
                keys.append(key)
        url = f"{self.settings.security_base}/wx/v2/security/refreshToken"
        headers = self.api_headers(self.settings.rewards_client_id)
        if tokens.access_token:
            headers["Authorization"] = f"Bearer {tokens.access_token}"
        last_error: AuthError | None = None
        for key in keys:
            log.info("refreshing API bearer via %s (body key %r)", url, key)
            try:
                resp = self.http.post(url, json={key: tokens.refresh_token}, headers=headers, timeout=REFRESH_TIMEOUT)
            except httpx.HTTPError as exc:
                raise AuthError(f"could not reach the Everyday Rewards API: {exc}") from exc
            try:
                body = _json_or_error(resp)
            except AuthError as exc:
                last_error = exc
                continue
            if resp.status_code in (401, 403):
                raise ReloginRequired(f"Everyday Rewards refused the refresh token ({resp.status_code}): {_error_text(body)}")
            if resp.status_code != 200:
                last_error = AuthError(f"refresh failed ({resp.status_code}) with body key {key!r}: {_error_text(body)}")
                continue
            data = _unwrap(body)
            if not _pick(data, "bearer", "access_token", "accessToken"):
                last_error = AuthError(f"refresh response had no bearer (body key {key!r}): {json.dumps(body)[:300]}")
                continue
            if key != self.store.refresh_body_key:
                log.info("refresh endpoint accepted body key %r; remembering it", key)
                self.store.refresh_body_key = key
            self._store_apigee_from_response(
                data,
                source="refresh",
                fallback_refresh=tokens.refresh_token,
                fallback_refresh_expires_at=tokens.refresh_expires_at,
                fallback_refresh_lifetime=tokens.refresh_lifetime,
            )
            return
        raise last_error or AuthError("refresh failed")


class _ExchangeUnauthorized(AuthError):
    """The token-exchange endpoint rejected the Auth0 JWT (retry with a fresh JWT)."""
