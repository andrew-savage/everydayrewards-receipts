"""Authentication against Everyday Rewards.

Two ways to hold a long-lived session are supported:

* ``auth0`` mode - we run the same Auth0 authorization-code + PKCE login the web app
  uses, keep the Auth0 refresh token, and exchange Auth0 access tokens for API bearer
  tokens with the ``token-exchange`` endpoint the web app calls.
* ``apigee`` mode - the browser's stored ``authStatusData`` (bearer + refresh token) is
  imported and refreshed through the API's ``/wx/v2/security/refreshToken`` endpoint.

Bearer tokens for the API are short-lived (about 30 minutes), so both modes refresh
transparently from :meth:`AuthManager.get_bearer`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
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

AUTH0_SCOPE = "openid profile offline_access"
AUTH0_CLIENT_HEADER = base64.b64encode(
    json.dumps({"name": "auth0-spa-js", "version": "2.23.0"}).encode()
).decode()
WEB_ORIGIN = "https://www.everyday.com.au"


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


@dataclass
class Auth0Tokens:
    access_token: str
    expires_at: float
    refresh_token: str | None = None
    scope: str | None = None


@dataclass
class TokenStore:
    mode: str = "none"  # none | auth0 | apigee | static
    apigee: ApigeeTokens | None = None
    auth0: Auth0Tokens | None = None
    updated_at: float = 0.0
    path: Path | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "apigee": vars(self.apigee) if self.apigee else None,
            "auth0": vars(self.auth0) if self.auth0 else None,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> "TokenStore":
        apigee = data.get("apigee")
        auth0 = data.get("auth0")
        return cls(
            mode=data.get("mode", "none"),
            apigee=ApigeeTokens(**apigee) if apigee else None,
            auth0=Auth0Tokens(**auth0) if auth0 else None,
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


# --------------------------------------------------------------------------- PKCE


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 (S256)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(settings: Settings, *, state: str, code_challenge: str, redirect_uri: str) -> str:
    params = {
        "client_id": settings.auth0_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": AUTH0_SCOPE,
        "audience": settings.auth0_audience,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": AUTH0_CLIENT_HEADER,
    }
    return f"{settings.auth0_domain}/authorize?{urlencode(params)}"


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
    """Serve one HTTP request on host:port and return its path (including the query string)."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if "code=" in self.path or "error=" in self.path:
                result["path"] = self.path
                body = b"<html><body><h2>Everyday Receipts: login received. You can close this tab.</h2></body></html>"
            else:
                body = b"<html><body>Waiting for the Auth0 redirect...</body></html>"
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


# --------------------------------------------------------------------------- helpers


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

    # -- public -------------------------------------------------------------

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
            if mode == "auth0":
                self._refresh_via_auth0(force=force_refresh)
            elif mode == "apigee":
                self._refresh_via_apigee()
            else:
                raise ReloginRequired("no credentials stored; run `everyday-receipts login` first")
            assert self.store.apigee is not None
            return self.store.apigee.access_token

    def complete_pkce_login(self, code: str, code_verifier: str, redirect_uri: str) -> None:
        with self._lock:
            auth0 = self._auth0_token_request(
                {
                    "grant_type": "authorization_code",
                    "client_id": self.settings.auth0_client_id,
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                }
            )
            apigee = self._exchange_auth0_token(auth0.access_token)
            self.store.mode = "auth0"
            self.store.auth0 = auth0
            self.store.apigee = apigee
            self.store.save()
            if not auth0.refresh_token:
                log.warning(
                    "Auth0 did not return a refresh token; this session will stop working when the "
                    "Auth0 access token expires (about %d min)",
                    int((auth0.expires_at - self.clock()) / 60),
                )

    def import_auth_status(self, text: str) -> None:
        """Import the web app's localStorage ``authStatusData`` JSON blob."""
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise AuthError(f"authStatusData is not valid JSON: {exc}") from exc
        if isinstance(data, dict) and isinstance(data.get("authStatus"), dict):
            data = data["authStatus"]
        if not isinstance(data, dict) or not data.get("access_token"):
            raise AuthError("authStatusData has no access_token field")
        now = self.clock()
        expires_in = int(data.get("expires_in") or 1800)
        refresh_token = data.get("refresh_token") or None
        refresh_expires_in = data.get("refresh_token_expires_in")
        with self._lock:
            self.store.mode = "apigee"
            self.store.auth0 = None
            self.store.apigee = ApigeeTokens(
                access_token=str(data["access_token"]),
                expires_at=now + expires_in,
                refresh_token=refresh_token,
                refresh_expires_at=(now + int(refresh_expires_in)) if refresh_expires_in else None,
            )
            self.store.save()
        if not refresh_token:
            log.warning(
                "authStatusData has no refresh_token; the imported bearer will stop working in "
                "about %d minutes. Prefer `everyday-receipts login`.",
                expires_in // 60,
            )

    def use_static_token(self, token: str) -> None:
        with self._lock:
            self.store.mode = "static"
            self.store.auth0 = None
            self.store.apigee = ApigeeTokens(access_token=token, expires_at=self.clock() + 1800)

    def describe(self) -> dict[str, Any]:
        now = self.clock()
        info: dict[str, Any] = {"mode": self.store.mode}
        if self.store.apigee:
            info["api_bearer_expires_in_s"] = int(self.store.apigee.expires_at - now)
            info["api_refresh_token"] = bool(self.store.apigee.refresh_token)
            if self.store.apigee.refresh_expires_at:
                info["api_refresh_expires_in_s"] = int(self.store.apigee.refresh_expires_at - now)
        if self.store.auth0:
            info["auth0_access_expires_in_s"] = int(self.store.auth0.expires_at - now)
            info["auth0_refresh_token"] = bool(self.store.auth0.refresh_token)
        return info

    # -- internals ----------------------------------------------------------

    def _auth0_headers(self) -> dict[str, str]:
        return {
            "Auth0-Client": AUTH0_CLIENT_HEADER,
            "Accept": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/",
            "User-Agent": self.settings.user_agent,
        }

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

    def _auth0_token_request(self, form: dict[str, str], fallback_refresh: str | None = None) -> Auth0Tokens:
        url = f"{self.settings.auth0_domain}/oauth/token"
        try:
            resp = self.http.post(url, data=form, headers=self._auth0_headers())
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach Auth0: {exc}") from exc
        body = _json_or_error(resp)
        if resp.status_code != 200 or not body.get("access_token"):
            error = body.get("error", f"http_{resp.status_code}")
            description = body.get("error_description", "")
            if form.get("grant_type") == "refresh_token" and error in ("invalid_grant", "unauthorized_client", "access_denied"):
                raise ReloginRequired(
                    f"Auth0 refused the refresh token ({error}: {description}); run `everyday-receipts login` again"
                )
            raise AuthError(f"Auth0 token request failed ({resp.status_code}): {error}: {description}")
        now = self.clock()
        return Auth0Tokens(
            access_token=body["access_token"],
            expires_at=now + int(body.get("expires_in") or 3600),
            refresh_token=body.get("refresh_token") or fallback_refresh,
            scope=body.get("scope"),
        )

    def _exchange_auth0_token(self, auth0_access_token: str) -> ApigeeTokens:
        url = f"{self.settings.api_base}/wx/v1/rewardspartner/secure/token-exchange"
        try:
            resp = self.http.post(
                url,
                json={"access_token": auth0_access_token},
                headers=self.api_headers(self.settings.partner_client_id),
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach the Everyday Rewards API: {exc}") from exc
        body = _json_or_error(resp)
        if resp.status_code != 200:
            raise AuthError(f"token exchange failed ({resp.status_code}): {json.dumps(body)[:300]}")
        data = _unwrap(body)
        access = _pick(data, "accessToken", "access_token", "bearer")
        ttl = _pick(data, "accessTokenExpiresIn", "expires_in", "bearerExpiredInSeconds")
        if not access:
            raise AuthError(f"token exchange returned no access token: {json.dumps(body)[:300]}")
        return ApigeeTokens(access_token=str(access), expires_at=self.clock() + int(ttl or 1800))

    def _refresh_via_auth0(self, force: bool) -> None:
        auth0 = self.store.auth0
        if auth0 is None:
            raise ReloginRequired("no Auth0 session stored; run `everyday-receipts login`")
        if force or auth0.expires_at - self.clock() <= self.REFRESH_MARGIN:
            if not auth0.refresh_token:
                raise ReloginRequired("the Auth0 session has expired and no refresh token is available; run `everyday-receipts login`")
            log.info("refreshing Auth0 session")
            auth0 = self._auth0_token_request(
                {
                    "grant_type": "refresh_token",
                    "client_id": self.settings.auth0_client_id,
                    "refresh_token": auth0.refresh_token,
                },
                fallback_refresh=auth0.refresh_token,
            )
            self.store.auth0 = auth0
            self.store.save()  # persist immediately: rotated refresh tokens are single-use
        log.info("exchanging Auth0 token for an API bearer")
        self.store.apigee = self._exchange_auth0_token(auth0.access_token)
        self.store.save()

    def _refresh_via_apigee(self) -> None:
        tokens = self.store.apigee
        if tokens is None or not tokens.refresh_token:
            raise ReloginRequired("the imported session has no refresh token; run `everyday-receipts login` or import a fresh authStatusData")
        now = self.clock()
        if tokens.refresh_expires_at and tokens.refresh_expires_at <= now:
            raise ReloginRequired("the imported refresh token has expired; log in again")
        url = f"{self.settings.api_base}/wx/v2/security/refreshToken"
        headers = self.api_headers(self.settings.rewards_client_id)
        headers["Authorization"] = f"Bearer {tokens.access_token}"
        log.info("refreshing API bearer via %s", url)
        try:
            resp = self.http.post(url, json={self.settings.apigee_refresh_body_key: tokens.refresh_token}, headers=headers)
        except httpx.HTTPError as exc:
            raise AuthError(f"could not reach the Everyday Rewards API: {exc}") from exc
        body = _json_or_error(resp)
        if resp.status_code in (400, 401, 403):
            raise ReloginRequired(f"Everyday Rewards refused the refresh token ({resp.status_code}): {json.dumps(body)[:300]}")
        if resp.status_code != 200:
            raise AuthError(f"refresh failed ({resp.status_code}): {json.dumps(body)[:300]}")
        data = _unwrap(body)
        access = _pick(data, "bearer", "access_token", "accessToken")
        ttl = _pick(data, "bearerExpiredInSeconds", "expires_in", "accessTokenExpiresIn")
        new_refresh = _pick(data, "refresh", "refresh_token", "refreshToken")
        refresh_ttl = _pick(data, "refreshExpiredInSeconds", "refresh_token_expires_in", "refreshTokenExpiresIn")
        if not access:
            raise AuthError(f"refresh response had no bearer: {json.dumps(body)[:300]}")
        self.store.apigee = ApigeeTokens(
            access_token=str(access),
            expires_at=now + int(ttl or 1800),
            refresh_token=str(new_refresh) if new_refresh else tokens.refresh_token,
            refresh_expires_at=(now + int(refresh_ttl)) if refresh_ttl else tokens.refresh_expires_at,
        )
        self.store.save()
