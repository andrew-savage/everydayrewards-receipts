"""Runtime configuration, read from environment variables (EDR_* prefix)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# Public identifiers baked into the Everyday Rewards web app (www.everyday.com.au).
DEFAULT_REWARDS_CLIENT_ID = "8h41mMOiDULmlLT28xKSv5ITpp3XBRvH"
DEFAULT_LOGIN_REDIRECT_URI = "https://www.everyday.com.au/callback"
DEFAULT_API_BASE = "https://api.everyday.com.au"
DEFAULT_GRAPHQL_URL = "https://apigee-prod.api-wr.com/wx/v1/bff/graphql"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DEFAULT_FILENAME_TEMPLATE = "{date} {partner} {store} {amount} [{short_id}]"

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """Parse '30m', '6h', '1d' or plain seconds into seconds."""
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"invalid duration {text!r}; use a number with an s/m/h/d suffix, e.g. 6h")
    return int(m.group(1)) * _UNITS[m.group(2).lower()]


def parse_bool(text: str) -> bool:
    return text.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    data_dir: Path
    json_dir: Path | None
    poll_interval: int
    full_scan: bool
    max_pages: int
    filename_template: str
    subdir_by_partner: bool
    api_base: str
    graphql_url: str
    rewards_client_id: str
    login_redirect_uri: str
    apigee_refresh_body_key: str
    user_agent: str
    request_timeout: float
    static_access_token: str | None
    auth_status_json: str | None
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e: Mapping[str, str] = os.environ if env is None else env

        def get(name: str, default: str) -> str:
            value = e.get(name)
            return default if value is None or value.strip() == "" else value.strip()

        data_dir = Path(get("EDR_DATA_DIR", "/data"))
        json_raw = e.get("EDR_JSON_DIR")
        if json_raw is None:
            json_dir: Path | None = data_dir / "json"
        elif json_raw.strip() == "" or json_raw.strip().lower() in {"off", "none", "false", "0"}:
            json_dir = None
        else:
            json_dir = Path(json_raw.strip())

        return cls(
            output_dir=Path(get("EDR_OUTPUT_DIR", "/receipts")),
            data_dir=data_dir,
            json_dir=json_dir,
            poll_interval=parse_duration(get("EDR_POLL_INTERVAL", "6h")),
            full_scan=parse_bool(get("EDR_FULL_SCAN", "false")),
            max_pages=int(get("EDR_MAX_PAGES", "60")),
            filename_template=get("EDR_FILENAME_TEMPLATE", DEFAULT_FILENAME_TEMPLATE),
            subdir_by_partner=parse_bool(get("EDR_SUBDIR_BY_PARTNER", "false")),
            api_base=get("EDR_API_BASE", DEFAULT_API_BASE).rstrip("/"),
            graphql_url=get("EDR_GRAPHQL_URL", DEFAULT_GRAPHQL_URL),
            rewards_client_id=get("EDR_REWARDS_CLIENT_ID", DEFAULT_REWARDS_CLIENT_ID),
            login_redirect_uri=get("EDR_LOGIN_REDIRECT_URI", DEFAULT_LOGIN_REDIRECT_URI),
            apigee_refresh_body_key=get("EDR_APIGEE_REFRESH_BODY_KEY", "refresh_token"),
            user_agent=get("EDR_USER_AGENT", DEFAULT_USER_AGENT),
            request_timeout=float(get("EDR_REQUEST_TIMEOUT", "30")),
            static_access_token=e.get("EDR_ACCESS_TOKEN") or None,
            auth_status_json=e.get("EDR_AUTH_STATUS_JSON") or None,
            log_level=get("EDR_LOG_LEVEL", "INFO").upper(),
        )

    @property
    def token_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def needs_login_path(self) -> Path:
        return self.data_dir / "NEEDS_LOGIN"
