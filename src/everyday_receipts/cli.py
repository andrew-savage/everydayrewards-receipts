"""Command line entry point: login, import-session, refresh, once, run, status, healthcheck."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

import httpx

from typing import Callable

from . import __version__
from .api import ApiError, EverydayRewardsClient
from .auth import (
    AuthError,
    AuthManager,
    ReloginRequired,
    TokenStore,
    looks_like_auth0_token,
    parse_pasted_json,
    parse_redirect,
    wait_for_callback,
)
from .config import Settings
from .state import SyncState
from .sync import ReceiptWriter, SyncService

log = logging.getLogger("everyday_receipts")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _read_json_block(read_line: "Callable[[], str]" = input) -> str:
    """Read a possibly multi-line pasted JSON object from stdin.

    Pretty-printed JSON spans many lines, so a single input() call is not enough. This reads
    lines until the top-level braces balance (ignoring braces inside strings), which is the
    end of the object; a single-line paste (or a bare token) returns after the first line.
    """
    lines: list[str] = []
    depth = 0
    started = False
    while True:
        try:
            line = read_line()
        except EOFError:
            break
        lines.append(line)
        in_str = False
        esc = False
        for ch in line:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str and ch == "{":
                depth += 1
                started = True
            elif not in_str and ch == "}":
                depth -= 1
        if started and depth <= 0:
            break
        if not started and line.strip():
            break  # a single-line paste with no unquoted braces (authStatusData, bare token)
    return "\n".join(lines)


def _fmt_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return "expired"
    if seconds >= 2 * 86400:
        return f"{seconds // 86400} days"
    if seconds >= 2 * 3600:
        return f"{seconds // 3600} hours"
    return f"{seconds // 60} min"


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.http = httpx.Client(timeout=settings.request_timeout, headers={"User-Agent": settings.user_agent})
        self.store = TokenStore.load(settings.token_path)
        self.auth = AuthManager(settings, self.store, self.http)
        if settings.static_access_token:
            log.warning("EDR_ACCESS_TOKEN is set; using it as a static bearer (testing only, expires in ~30 min)")
            self.auth.use_static_token(settings.static_access_token)
        elif self.store.mode == "none" and settings.app_token_json:
            log.info("importing app token from EDR_APP_TOKEN_JSON")
            self.auth.import_auth0_response(parse_pasted_json(settings.app_token_json))
        elif self.store.mode == "none" and settings.auth_status_json:
            log.info("importing session from EDR_AUTH_STATUS_JSON")
            self.auth.import_auth_status(settings.auth_status_json)
        self.client = EverydayRewardsClient(settings, self.auth, self.http)
        self.state = SyncState.load(settings.state_path)
        self.writer = ReceiptWriter(settings.output_dir, settings.json_dir, settings.subdir_by_partner)
        self.sync = SyncService(settings, self.client, self.state, self.writer)

    # -- helpers ---------------------------------------------------------------

    def _touch_heartbeat(self) -> None:
        self.settings.heartbeat_path.write_text(str(int(time.time())), encoding="utf-8")

    def _set_needs_login(self, reason: str | None) -> None:
        path = self.settings.needs_login_path
        if reason:
            path.write_text(reason + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()

    def verify_session(self) -> int:
        """Fetch the first feed page and return the number of receipts on it."""
        first_page = next(self.client.iter_receipt_pages(), [])
        return sum(1 for item in first_page if item.has_receipt)

    def _print_session_summary(self, receipts: int | None = None) -> None:
        info = self.auth.describe()
        print(f"  API bearer valid for : {_fmt_seconds(info.get('api_bearer_expires_in_s'))} (renewed automatically)")
        if info.get("mode") == "auth0" and info.get("app_refresh_token"):
            print("  App token            : yes (long-lived Auth0 session) - suitable for unattended use")
            print(f"  Auth0 token valid for: {_fmt_seconds(info.get('auth0_jwt_expires_in_s'))}")
        elif info.get("api_refresh_token"):
            print(f"  Refresh token        : yes, valid for {_fmt_seconds(info.get('api_refresh_expires_in_s'))}")
            lifetime = info.get("api_refresh_lifetime_s")
            if lifetime:
                print(f"  Session keepalive    : renewed every ~{_fmt_seconds(int(lifetime // 2))} while the service runs")
                if lifetime < 86400:
                    print(f"  NOTE: web session (backfill only); import an app token for unattended use")
        elif info.get("mode") not in ("static", "none"):
            print("  Refresh token        : NO - web session (backfill only), import an app token for unattended use")
        if info.get("refresh_body_key"):
            print(f"  Refresh body key     : {info['refresh_body_key']}")
        if receipts is not None:
            print(f"  Receipts on 1st page : {receipts}")

    # -- commands --------------------------------------------------------------

    def cmd_login(self, args: argparse.Namespace) -> int:
        redirect_uri = args.redirect_uri or self.settings.login_redirect_uri
        try:
            attempt = self.auth.begin_login(redirect_uri)
        except AuthError as exc:
            print(f"error: could not start the login: {exc}", file=sys.stderr)
            return 1
        problem = self.auth.check_login_url(attempt.url)
        if problem:
            print(f"error: Auth0 will not accept this login request: {problem}", file=sys.stderr)
            if args.redirect_uri:
                print("hint: that redirect URI is not on Woolworths' allow-list; run `login` without --redirect-uri", file=sys.stderr)
            return 2

        print()
        print("=" * 78)
        print("Everyday Rewards login")
        print("=" * 78)
        print("1. Open this URL in a browser and sign in (email, password, one-time code):")
        print()
        print(f"   {attempt.url}")
        print()
        print(f"2. After signing in you will be redirected to {redirect_uri}")
        if redirect_uri.startswith(("https://www.everyday.com.au", "https://everyday.com.au")):
            print("   That page will immediately use the code itself and move on. To stop it:")
            print("   block JavaScript for www.everyday.com.au in your browser's site settings")
            print("   BEFORE signing in, then copy the FULL address (it contains code=...).")
            print("   (Easier alternative: `everyday-receipts import-session` after a normal login.)")
        elif not args.listen:
            print("   The browser will show an error page (nothing listens there); that is fine.")
            print("   Copy the FULL address from the address bar (it contains code=...).")
        print()

        if args.listen:
            host, _, port = args.listen.rpartition(":")
            host = host or "0.0.0.0"
            print(f"Waiting up to {args.timeout}s for the browser to be redirected to {host}:{port} ...")
            try:
                pasted = wait_for_callback(host, int(port), args.timeout)
            except (TimeoutError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        else:
            try:
                pasted = input("3. Paste the redirect URL here and press Enter:\n> ")
            except EOFError:
                print("error: no input received (run with a TTY, e.g. `docker compose run --rm everyday-receipts login`)", file=sys.stderr)
                return 2

        try:
            code, callback_state = parse_redirect(pasted)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if (
            callback_state is not None
            and attempt.auth0_state is not None
            and callback_state != attempt.auth0_state
            and not args.ignore_state

        ):
            print("error: the state in the redirect does not belong to this login attempt; start again", file=sys.stderr)
            return 2

        try:
            self.auth.complete_login(code, callback_state, attempt)
            receipts = self.verify_session()
        except (AuthError, ApiError) as exc:
            print(f"\nlogin failed: {exc}", file=sys.stderr)
            return 1
        self._set_needs_login(None)
        print()
        print("Login successful. Session stored in", self.settings.token_path)
        self._print_session_summary(receipts)
        print("Next: run `everyday-receipts refresh` once to confirm unattended renewal works.")
        return 0

    def cmd_import_app_token(self, args: argparse.Namespace) -> int:
        try:
            if args.refresh_token:
                self.auth.import_app_token(refresh_token=args.refresh_token, access_token=args.access_token)
            else:
                if args.file:
                    text = Path(args.file).read_text(encoding="utf-8")
                else:
                    print("Capture the Everyday Rewards APP login with a proxy (e.g. HTTP Toolkit) and find the")
                    print("request to auth.everyday.com.au/oauth/token. Paste its JSON RESPONSE body here")
                    print("(the whole object with access_token and refresh_token; multi-line is fine), then")
                    print("press Enter. Tip: `import-app-token --file token.json` avoids paste issues.")
                    print("> ", end="", flush=True)
                    text = _read_json_block()
                    if not text.strip():
                        print("error: no input received", file=sys.stderr)
                        return 2
                data = parse_pasted_json(text)
                if not looks_like_auth0_token(data):
                    print("error: that does not look like an Auth0 token response (needs refresh_token + access_token)", file=sys.stderr)
                    return 2
                self.auth.import_auth0_response(data)
            receipts = self.verify_session()
        except (AuthError, ApiError) as exc:
            print(f"import failed: {exc}", file=sys.stderr)
            return 1
        self._set_needs_login(None)
        print("App token imported.")
        self._print_session_summary(receipts)
        return 0

    def cmd_import_session(self, args: argparse.Namespace) -> int:
        if args.refresh_token:
            try:
                self.auth.import_tokens(
                    refresh_token=args.refresh_token,
                    access_token=args.access_token,
                    refresh_lifetime=float(args.refresh_lifetime) if args.refresh_lifetime else None,
                )
                receipts = self.verify_session()
            except (AuthError, ApiError) as exc:
                print(f"import failed: {exc}", file=sys.stderr)
                return 1
            self._set_needs_login(None)
            print("Token imported.")
            self._print_session_summary(receipts)
            return 0
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8")
        else:
            print("Sign in at https://www.everyday.com.au in a browser, open the developer console")
            print("(F12 / Cmd-Opt-J) and run:")
            print("    localStorage.getItem('authStatusData')")
            print("Copy the value it prints (quotes, backslashes and any trailing ' = $1' are fine),")
            print("paste it here and press Enter:")
            print("> ", end="", flush=True)
            text = _read_json_block()
            if not text.strip():
                print("error: no input received", file=sys.stderr)
                return 2
        try:
            data = parse_pasted_json(text)
            if looks_like_auth0_token(data):
                log.info("detected an Auth0 app token; importing it as a long-lived session")
                self.auth.import_auth0_response(data)
            else:
                self.auth.import_auth_status(text)
            receipts = self.verify_session()
        except (AuthError, ApiError) as exc:
            print(f"import failed: {exc}", file=sys.stderr)
            return 1
        self._set_needs_login(None)
        print("Session imported.")
        self._print_session_summary(receipts)
        return 0

    def cmd_refresh(self, args: argparse.Namespace) -> int:
        before = self.store.apigee.refresh_token if self.store.apigee else None
        try:
            self.auth.get_bearer(force_refresh=True)
            receipts = self.verify_session()
        except ReloginRequired as exc:
            self._set_needs_login(str(exc))
            print(f"refresh failed, login required: {exc}", file=sys.stderr)
            return 3
        except (AuthError, ApiError) as exc:
            print(f"refresh failed: {exc}", file=sys.stderr)
            return 1
        self._set_needs_login(None)
        after = self.store.apigee.refresh_token if self.store.apigee else None
        rotated = "a new refresh token was issued" if after and after != before else "the refresh token was NOT rotated"
        print(f"Refresh OK: a new bearer was issued, {rotated}, and the activity feed loads.")
        self._print_session_summary(receipts)
        if after and after == before and self.store.apigee and self.store.apigee.refresh_lifetime and self.store.apigee.refresh_lifetime < 86400:
            print("WARNING: without rotation the session will end when the refresh token expires; report this output.")
        return 0

    def cmd_once(self, args: argparse.Namespace) -> int:
        try:
            stats = self.sync.run_once()
        except ReloginRequired as exc:
            self._set_needs_login(str(exc))
            log.error("LOGIN REQUIRED: %s", exc)
            return 3
        except (AuthError, ApiError) as exc:
            log.error("sync failed: %s", exc)
            return 1
        self._set_needs_login(None)
        self._touch_heartbeat()
        return 0 if stats.failed == 0 else 1

    def cmd_run(self, args: argparse.Namespace) -> int:
        interval = self.settings.poll_interval
        stop = {"flag": False}

        def _handle(signum: int, _frame: object) -> None:
            log.info("received signal %d; stopping after the current cycle", signum)
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
        log.info("everyday-receipts %s starting; polling every %ds; writing to %s", __version__, interval, self.settings.output_dir)

        while not stop["flag"]:
            try:
                self.sync.run_once()
                self._set_needs_login(None)
                self._touch_heartbeat()
            except ReloginRequired as exc:
                self._set_needs_login(str(exc))
                log.error("LOGIN REQUIRED - run `everyday-receipts login` or `import-session`: %s", exc)
            except (AuthError, ApiError) as exc:
                log.error("sync failed; will retry next cycle: %s", exc)
            except Exception:  # keep the service alive on unexpected errors
                log.exception("unexpected error during sync")
            deadline = time.time() + interval
            next_check = 0.0
            while not stop["flag"] and time.time() < deadline:
                if time.time() >= next_check:
                    next_check = time.time() + 60
                    self._keepalive_tick()
                time.sleep(min(5, max(0.0, deadline - time.time())))
        return 0

    def _keepalive_tick(self) -> None:
        """Renew the refresh token between syncs so short-lived sessions survive the poll interval."""
        try:
            if self.auth.keepalive():
                log.info("session renewed (keepalive)")
                self._set_needs_login(None)
        except ReloginRequired as exc:
            if not self.settings.needs_login_path.exists():
                log.error("LOGIN REQUIRED - run `everyday-receipts import-session` or `login`: %s", exc)
            self._set_needs_login(str(exc))
        except AuthError as exc:
            log.warning("session keepalive failed; will retry: %s", exc)

    def cmd_status(self, args: argparse.Namespace) -> int:
        info = self.auth.describe()
        print(f"auth mode            : {info['mode']}")
        if info["mode"] != "none":
            self._print_session_summary()
        print(f"receipts saved       : {len(self.state.seen)}")
        print(f"last sync            : {self.state.last_sync or 'never'}")
        if self.settings.needs_login_path.exists():
            print(f"NEEDS LOGIN          : {self.settings.needs_login_path.read_text().strip()}")
        return 0

    def cmd_healthcheck(self, args: argparse.Namespace) -> int:
        if self.settings.needs_login_path.exists():
            print("unhealthy: login required")
            return 1
        hb = self.settings.heartbeat_path
        if not hb.exists():
            print("starting: no sync completed yet")
            return 1
        age = time.time() - int(hb.read_text().strip() or 0)
        limit = self.settings.poll_interval * 2 + 600
        if age > limit:
            print(f"unhealthy: last successful sync {int(age)}s ago")
            return 1
        print("ok")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="everyday-receipts", description="Fetch Everyday Rewards e-receipts into a folder")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="interactive one-time login through the Everyday Rewards login page")
    login.add_argument("--redirect-uri", help="override the redirect URI (default: the web app's callback URL)")
    login.add_argument("--listen", metavar="[HOST:]PORT", help="listen locally for the redirect instead of pasting it")
    login.add_argument("--timeout", type=int, default=600, help="seconds to wait when using --listen")
    login.add_argument("--ignore-state", action="store_true", help="skip the OAuth state check")

    app_token = sub.add_parser("import-app-token", help="import the mobile app's Auth0 token (recommended, unattended)")
    app_token.add_argument("--file", help="read the Auth0 token JSON from a file instead of stdin")
    app_token.add_argument("--refresh-token", help="the app's Auth0 refresh token, if you have only that")
    app_token.add_argument("--access-token", help="the app's Auth0 access token (JWT), optional")

    imp = sub.add_parser("import-session", help="import a web session for a one-off backfill (cannot refresh)")
    imp.add_argument("--file", help="read the JSON from a file instead of stdin")
    imp.add_argument("--refresh-token", help="import a refresh token directly")
    imp.add_argument("--access-token", help="the matching bearer token (optional; minted from the refresh token if omitted)")
    imp.add_argument("--refresh-lifetime", help="seconds the refresh token is valid")

    sub.add_parser("refresh", help="force a token refresh now and report the result")
    sub.add_parser("once", help="sync once and exit")
    sub.add_parser("run", help="sync forever on EDR_POLL_INTERVAL (default command)")
    sub.add_parser("status", help="show session and sync state")
    sub.add_parser("healthcheck", help="exit 0 when the service is healthy")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    _setup_logging(settings.log_level)
    try:
        app = App(settings)
    except AuthError as exc:
        log.error("%s", exc)
        return 1
    handler = getattr(app, f"cmd_{args.command.replace('-', '_')}")
    return int(handler(args))
