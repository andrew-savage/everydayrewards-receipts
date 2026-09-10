"""Authentication against Everyday Rewards.

The website logs in through Auth0 but never talks to Auth0's token endpoint itself:
its backend hands out the Auth0 login URL (``/wx/v2/security/login/url``) and later
swaps the returned code for API tokens (``/wx/v2/security/token``). We drive exactly
that flow, keep the resulting refresh token, and renew the short-lived bearer with
``/wx/v2/security/refreshToken``.

Alternatively the browser's stored ``authStatusData`` (bearer + refresh token) can be
imported; it is renewed the same way.
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
    """Stored credentials cannot be refreshed any more; a person must log in again."""


@dataclass
class ApigeeTokens:
    access_token: str
    expires_at: float
    refresh_token: str | None = None
    refresh_expires_at: float | None = None
    refresh_lifetime: float | None = None  # seconds the refresh token was valid for when issued


@dataclass
class TokenStore:
    mode: str = "none"  # none | apigee | static
    apigee: ApigeeTokens | None = None
    refresh_body_key: str | None = None  # JSON key the refresh endpoint accepted last time
    updated_at: float = 0.0
    path: Path | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "apigee": vars(self.apigee) if self.apigee else None,
            "refresh_body_key": self.refresh_body_key,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> "TokenStore":
        apigee = data.get("apigee")
        mode = data.get("mode", "none")
        if mode not in ("none", "apigee", "static"):
            mode = "apigee" if apigee else "none"
        return cls(
            mode=mode,
            apigee=ApigeeTokens(**apigee) if apigee else None,
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
        self.save()


@dataclass
class LoginAttempt:
    url: str  # Auth0 authorize URL to open in a browser
    state: str  # state we generated and sent to the backend
    redirect_uri: str
    auth0_state: str | None  # state the backend put into the Auth0 URL (echoed back on the callback)


# --------------------------------------------------------------------------- helpers


def make_state() -> str:
    """State in the same shape the website generates (base64 JSON with a nonce)."""
    payload = {"redirectUri": WEB_ORIGIN + "/", "nonce": str(secrets.randbelow(1000))}
    return base64.b64encode(json.dumps(payload).encode()).decode()


_ACCESS_KEYS = ("bearer", "access_token", "accessToken")
_REFRESH_KEYS = ("refresh", "refresh_token", "refreshToken")


def _find_token_dict(obj: Any) -> dict[str, Any] | None:
    """Depth-first search for the dict that carries a bearer/access token."""
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


def parse_auth_status(text: str) -> dict[str, Any]:
    """Parse a captured session however it was rendered.

    Accepts the browser's ``authStatusData`` (raw, a JSON string literal with ``\"``
    escapes, a trailing `` = $1`` marker, or surrounding quotes) and also the nested
    shapes a proxy capture of the mobile app produces, e.g. ``{"data":{"login":{...}}}``.
    """
    cleaned = text.strip()
    cleaned = re.sub(r"\s*=\s*\$\d+\s*$", "", cleaned)  # Safari's " = $1" suffix
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "'\"" and not cleaned.startswith('"{'):
        cleaned = cleaned[1:-1]
    data: Any = None
    for attempt in range(3):
        try:
            data = json.loads(cleaned)
        except ValueError:
            if attempt == 0 and "\\\"" in cleaned:
                cleaned = cleaned.replace("\\\"", '"')
                cleaned = cleaned.strip("'\"") if not cleaned.startswith("{") else cleaned
                continue
            raise AuthError("that is not valid JSON; paste exactly what the console printed") from None
        if isinstance(data, str):
            cleaned = data  # double-encoded: unwrap and parse again
            continue
        break
    token_dict = _find_token_dict(data)
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
    """Serve HTTP on host:port until a request carrying code= or error= arrives; return its path."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
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

        def log_message(self, *args: Any) -> None:  # silence default logging
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
        return "; ".join(f"{e.get('code', '')}: {e.get('message', '')}".strip(": ") for e in errors if isinstance(e, dict))
    return json.dumps(body)[:300]


# --------------------------------------------------------------------------- manager


class AuthManager:
    REFRESH_MARGIN = 120  # seconds before expiry at which we refresh

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
        """Return an API bearer token, refreshing it first if it is (nearly) expired."""
        with self._lock:
            tokens = self.store.apigee
            if (
                not force_refresh
                and tokens
                and tokens.expires_at - self.clock() > self.REFRESH_MARGIN
            ):
                return tokens.access_token
            mode = self.store.mode
            if mode == "static":
                if force_refresh or not tokens:
                    raise ReloginRequired("the static EDR_ACCESS_TOKEN was rejected or has expired; supply a new one")
                return tokens.access_token
            if mode == "apigee":
                self._refresh()
            else:
                raise ReloginRequired("no credentials stored; run `everyday-receipts login` or `import-session` first")
            assert self.store.apigee is not None
            return self.store.apigee.access_token

    # -- login ------------------------------------------------------------------

    def begin_login(self, redirect_uri: str) -> LoginAttempt:
        """Ask the backend for the Auth0 login URL, as the website does."""
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
        """Return Auth0's up-front rejection (e.g. callback URL mismatch), or None if the URL looks usable."""
        try:
            resp = self.http.get(
                url,
                headers={"Accept": "text/html", "User-Agent": self.settings.user_agent},
                follow_redirects=False,
            )
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
        """Swap the code from the callback URL for API tokens via the backend."""
        payload = {
            "code": code,
            "state": callback_state or attempt.auth0_state or attempt.state,
            "redirectUri": attempt.redirect_uri,
        }
        url = f"{self.settings.security_base}/wx/v2/security/token"
        try:
            resp = self.http.post(url, json=payload, headers=self.api_headers(self.settings.rewards_client_id))
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach the Everyday Rewards API: {exc}") from exc
        body = _json_or_error(resp)
        if resp.status_code != 200:
            raise AuthError(f"Everyday Rewards rejected the login code ({resp.status_code}): {_error_text(body)}")
        data = _unwrap(body)
        returned_state = data.get("state")
        if returned_state and returned_state != attempt.state:
            log.warning("token endpoint returned a different state than we generated; continuing")
        if data.get("passwordResetRequired"):
            log.warning("Everyday Rewards says this account must reset its password; receipts may not load until that is done")
        with self._lock:
            return self._store_tokens(data, source="login")

    def import_auth_status(self, text: str) -> None:
        """Import the web app's localStorage ``authStatusData`` JSON blob."""
        data = parse_auth_status(text)
        with self._lock:
            tokens = self._store_tokens(data, source="import")
        self._warn_about_lifetime(tokens)

    def import_tokens(self, *, refresh_token: str, access_token: str | None, refresh_lifetime: float | None = None) -> None:
        """Store a session from a directly-supplied refresh token (e.g. captured from the app)."""
        now = self.clock()
        with self._lock:
            self.store.mode = "apigee"
            self.store.apigee = ApigeeTokens(
                access_token=access_token or "",
                expires_at=now if not access_token else now + 3300,
                refresh_token=refresh_token,
                refresh_expires_at=(now + refresh_lifetime) if refresh_lifetime else None,
                refresh_lifetime=refresh_lifetime,
            )
            self.store.save()
            if not access_token:
                self._refresh()  # mint a bearer straight away
            self._warn_about_lifetime(self.store.apigee)

    def _warn_about_lifetime(self, tokens: ApigeeTokens) -> None:
        if not tokens.refresh_token:
            log.warning(
                "no refresh token in this session; the bearer will stop working in about %d minutes",
                int((tokens.expires_at - self.clock()) // 60),
            )
        elif tokens.refresh_lifetime and tokens.refresh_lifetime < 6 * 3600:
            log.warning(
                "this refresh token lasts only ~%d minutes (a web session). For unattended use, "
                "import a mobile-app token instead; see the README.",
                int(tokens.refresh_lifetime // 60),
            )

    def keepalive_due(self, now: float | None = None) -> bool:
        """True when the refresh token should be renewed to keep the session alive.

        Refresh tokens from the web login are short-lived (about two hours), so a service
        that only syncs every few hours must renew them in between. We renew once less than
        half the lifetime (or five minutes) remains, or when the bearer itself has expired.
        """
        tokens = self.store.apigee
        if self.store.mode != "apigee" or tokens is None or not tokens.refresh_token:
            return False
        now = self.clock() if now is None else now
        if tokens.expires_at - now <= self.REFRESH_MARGIN:
            return True
        if tokens.refresh_expires_at is None:
            return False
        threshold = max(300.0, (tokens.refresh_lifetime or 0.0) / 2)
        return tokens.refresh_expires_at - now <= threshold

    def keepalive(self) -> bool:
        """Renew the session if :meth:`keepalive_due`; returns True when a refresh happened."""
        with self._lock:
            if not self.keepalive_due():
                return False
            self._refresh()
            return True

    def use_static_token(self, token: str) -> None:
        with self._lock:
            self.store.mode = "static"
            self.store.apigee = ApigeeTokens(access_token=token, expires_at=self.clock() + 1800)

    def describe(self) -> dict[str, Any]:
        now = self.clock()
        info: dict[str, Any] = {"mode": self.store.mode}
        if self.store.apigee:
            info["api_bearer_expires_in_s"] = int(self.store.apigee.expires_at - now)
            info["api_refresh_token"] = bool(self.store.apigee.refresh_token)
            if self.store.apigee.refresh_expires_at:
                info["api_refresh_expires_in_s"] = int(self.store.apigee.refresh_expires_at - now)
            if self.store.apigee.refresh_lifetime:
                info["api_refresh_lifetime_s"] = int(self.store.apigee.refresh_lifetime)
        if self.store.refresh_body_key:
            info["refresh_body_key"] = self.store.refresh_body_key
        return info

    # -- internals --------------------------------------------------------------

    def _store_tokens(
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

    def _refresh(self) -> None:
        tokens = self.store.apigee
        if tokens is None or not tokens.refresh_token:
            raise ReloginRequired("the stored session has no refresh token; run `everyday-receipts login` or `import-session`")
        now = self.clock()
        if tokens.refresh_expires_at and tokens.refresh_expires_at <= now:
            raise ReloginRequired("the refresh token has expired; run `everyday-receipts login` again")

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
            self._store_tokens(
                data,
                source="refresh",
                fallback_refresh=tokens.refresh_token,
                fallback_refresh_expires_at=tokens.refresh_expires_at,
                fallback_refresh_lifetime=tokens.refresh_lifetime,
            )
            return
        raise last_error or AuthError("refresh failed")
