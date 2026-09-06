"""Command line entry point: login, import-session, once, run, status, healthcheck."""

from __future__ import annotations

import argparse
import logging
import secrets
import signal
import sys
import time
from pathlib import Path

import httpx

from . import __version__
from .api import ApiError, EverydayRewardsClient
from .auth import (
    AuthError,
    AuthManager,
    ReloginRequired,
    TokenStore,
    build_authorize_url,
    generate_pkce,
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
        first_page = next(self.client.iter_activity_pages(), [])
        return sum(1 for item in first_page if item.has_receipt)

    # -- commands --------------------------------------------------------------

    def cmd_login(self, args: argparse.Namespace) -> int:
        redirect_uri = args.redirect_uri or self.settings.auth0_redirect_uri
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(16)
        url = build_authorize_url(self.settings, state=state, code_challenge=challenge, redirect_uri=redirect_uri)

        print()
        print("=" * 78)
        print("Everyday Rewards login")
        print("=" * 78)
        print("1. Open this URL in a browser and log in (email, password, any one-time code):")
        print()
        print(f"   {url}")
        print()
        print(f"2. After logging in you will be redirected to {redirect_uri}")
        if redirect_uri.startswith("https://www.everyday.com.au"):
            print("   That page will spin and may try to log you in itself. Before it moves on,")
            print("   copy the FULL address from the browser's address bar (it contains code=...).")
            print("   Tip: temporarily blocking JavaScript for www.everyday.com.au in your browser's")
            print("   site settings keeps the page from navigating away, making this easy.")
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
            code, returned_state = parse_redirect(pasted)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if returned_state is not None and returned_state != state and not args.ignore_state:
            print("error: the state in the redirect does not match this login attempt; start again", file=sys.stderr)
            return 2

        try:
            self.auth.complete_pkce_login(code, verifier, redirect_uri)
            receipts = self.verify_session()
        except (AuthError, ApiError) as exc:
            print(f"\nlogin failed: {exc}", file=sys.stderr)
            return 1
        self._set_needs_login(None)
        info = self.auth.describe()
        print()
        print("Login successful. Session stored in", self.settings.token_path)
        print(f"  API bearer valid for   : {info.get('api_bearer_expires_in_s', 0) // 60} min (auto-refreshed)")
        print(f"  Auth0 access token     : {info.get('auth0_access_expires_in_s', 0) // 60} min")
        print(f"  Auth0 refresh token    : {'yes' if info.get('auth0_refresh_token') else 'NO - unattended refresh will not work'}")
        print(f"  Receipts on first page : {receipts}")
        return 0

    def cmd_import_session(self, args: argparse.Namespace) -> int:
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8")
        else:
            print("Log in at https://www.everyday.com.au, open the browser console and run:")
            print("    copy(localStorage.getItem('authStatusData'))")
            print("then paste the result here and press Enter:")
            try:
                text = input("> ")
            except EOFError:
                print("error: no input received", file=sys.stderr)
                return 2
        try:
            self.auth.import_auth_status(text)
            receipts = self.verify_session()
        except (AuthError, ApiError) as exc:
            print(f"import failed: {exc}", file=sys.stderr)
            return 1
        self._set_needs_login(None)
        info = self.auth.describe()
        print("Session imported.")
        print(f"  API bearer valid for : {info.get('api_bearer_expires_in_s', 0) // 60} min")
        print(f"  Refresh token        : {'yes' if info.get('api_refresh_token') else 'NO - this session dies in ~30 min'}")
        if info.get("api_refresh_expires_in_s") is not None:
            print(f"  Refresh token valid  : {info['api_refresh_expires_in_s'] // 86400} days")
        print(f"  Receipts on first page: {receipts}")
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
                log.error("LOGIN REQUIRED - run `everyday-receipts login` in this container: %s", exc)
            except (AuthError, ApiError) as exc:
                log.error("sync failed; will retry next cycle: %s", exc)
            except Exception:  # keep the service alive on unexpected errors
                log.exception("unexpected error during sync")
            deadline = time.time() + interval
            while not stop["flag"] and time.time() < deadline:
                time.sleep(min(5, max(0.0, deadline - time.time())))
        return 0

    def cmd_status(self, args: argparse.Namespace) -> int:
        info = self.auth.describe()
        print(f"auth mode            : {info['mode']}")
        if "api_bearer_expires_in_s" in info:
            print(f"API bearer expires in: {info['api_bearer_expires_in_s'] // 60} min")
        if "auth0_refresh_token" in info:
            print(f"Auth0 refresh token  : {'yes' if info['auth0_refresh_token'] else 'no'}")
        if "api_refresh_token" in info:
            print(f"API refresh token    : {'yes' if info['api_refresh_token'] else 'no'}")
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

    login = sub.add_parser("login", help="interactive one-time login (Auth0 PKCE flow)")
    login.add_argument("--redirect-uri", help="override the redirect URI (default: the web app's callback URL)")
    login.add_argument("--listen", metavar="[HOST:]PORT", help="listen locally for the redirect instead of pasting it")
    login.add_argument("--timeout", type=int, default=600, help="seconds to wait when using --listen")
    login.add_argument("--ignore-state", action="store_true", help="skip the OAuth state check")

    imp = sub.add_parser("import-session", help="import the browser's authStatusData JSON")
    imp.add_argument("--file", help="read the JSON from a file instead of stdin")

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
