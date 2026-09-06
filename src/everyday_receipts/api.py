"""HTTP client for the Everyday Rewards activity feed and e-receipt endpoints."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterator

import httpx

from . import queries
from .auth import AuthError, AuthManager, ReloginRequired
from .config import Settings
from .models import ActivityItem, ReceiptDetails

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class GraphQLError(ApiError):
    pass


def _is_schema_error(errors: list[dict[str, Any]]) -> bool:
    text = " ".join(str(e.get("message", "")) for e in errors).lower()
    return "cannot query field" in text or "unknown argument" in text or "validation" in text


class EverydayRewardsClient:
    RETRIES = 3

    def __init__(
        self,
        settings: Settings,
        auth: AuthManager,
        http: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.auth = auth
        self.http = http
        self.sleep = sleep

    # -- transport ------------------------------------------------------------

    def _headers(self, bearer: str) -> dict[str, str]:
        headers = self.auth.api_headers(self.settings.rewards_client_id)
        headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def _post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        refreshed = False
        last_error: Exception | None = None
        for attempt in range(self.RETRIES):
            bearer = self.auth.get_bearer()
            try:
                resp = self.http.post(url, json=payload, headers=self._headers(bearer))
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("network error calling %s (attempt %d): %s", url, attempt + 1, exc)
                self.sleep(2**attempt)
                continue
            if resp.status_code == 401:
                if not refreshed:
                    log.info("API returned 401; refreshing the bearer token and retrying")
                    refreshed = True
                    self.auth.get_bearer(force_refresh=True)
                    continue
                raise AuthError(f"API still rejects the bearer after a refresh: {resp.text.strip()[:300]}")
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = ApiError(f"{url} returned {resp.status_code}", resp.status_code, resp.text[:300])
                log.warning("server error %s from %s (attempt %d)", resp.status_code, url, attempt + 1)
                self.sleep(2**attempt)
                continue
            return resp
        raise ApiError(f"giving up on {url}: {last_error}") from last_error

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = self._post(self.settings.graphql_url, {"query": query, "variables": variables})
        try:
            body = resp.json()
        except ValueError as exc:
            raise ApiError(f"GraphQL endpoint returned non-JSON ({resp.status_code})", resp.status_code, resp.text[:300]) from exc
        if resp.status_code >= 400:
            raise ApiError(f"GraphQL endpoint returned {resp.status_code}", resp.status_code, body)
        errors = body.get("errors") or []
        data = body.get("data")
        if errors and not data:
            raise GraphQLError(f"GraphQL errors: {errors}", resp.status_code, errors)
        if errors:
            log.warning("GraphQL returned partial data with errors: %s", errors)
        return data or {}

    # -- activity feed ----------------------------------------------------------

    def iter_activity_pages(self) -> Iterator[list[ActivityItem]]:
        """Yield the activity feed one page at a time, newest first."""
        page_token: str | None = "FIRST_PAGE"
        pages = 0
        while page_token and pages < self.settings.max_pages:
            data = self.graphql(
                queries.ACTIVITY_FEED,
                {"page": page_token, "enableOnlineReceipt": True, "featureFlags": {"activityBreakdown": True}},
            )
            feed = ((data.get("rtlRewardsActivityFeed") or {}).get("list")) or {}
            items: list[ActivityItem] = []
            for group in feed.get("groups") or []:
                if not isinstance(group, dict) or "items" not in group:
                    continue
                for item in group.get("items") or []:
                    items.append(ActivityItem.from_graphql(item, group.get("title")))
            pages += 1
            yield items
            page_token = feed.get("nextPageToken") or None
        if page_token:
            log.warning("stopped after %d pages (EDR_MAX_PAGES); more history remains", pages)

    # -- receipts ---------------------------------------------------------------

    def get_receipt_details(self, item: ActivityItem) -> ReceiptDetails:
        try:
            data = self.graphql(queries.ACTIVITY_DETAILS, {"id": item.details_id})
        except GraphQLError as exc:
            if not (isinstance(exc.body, list) and _is_schema_error(exc.body)):
                raise
            log.warning("detailed receipt query rejected by the schema; falling back to the minimal query")
            data = self.graphql(queries.ACTIVITY_DETAILS_MINIMAL, {"id": item.details_id})
        details = data.get("activityDetails")
        if not details:
            return self._receipt_details_rest(item)
        try:
            return ReceiptDetails.from_activity_details(details)
        except ValueError as exc:
            log.warning("activityDetails for %s unusable (%s); trying the REST endpoint", item.receipt_id, exc)
            return self._receipt_details_rest(item)

    def _receipt_details_rest(self, item: ActivityItem) -> ReceiptDetails:
        url = f"{self.settings.api_base}/wx/v1/rewards/member/ereceipts/transactions/details"
        resp = self._post(url, {"receiptKey": item.receipt_id})
        try:
            body = resp.json()
        except ValueError as exc:
            raise ApiError("receipt details endpoint returned non-JSON", resp.status_code, resp.text[:300]) from exc
        if resp.status_code >= 400:
            raise ApiError(f"receipt details endpoint returned {resp.status_code}", resp.status_code, body)
        receipt_details = ((body.get("data") or {}).get("receiptDetails")) or {}
        try:
            return ReceiptDetails.from_rest(receipt_details)
        except ValueError as exc:
            raise ApiError(f"no receipt available for {item.receipt_id}: {exc}", resp.status_code, body) from exc

    def download_pdf(self, details: ReceiptDetails) -> bytes:
        url = f"{self.settings.api_base}/wx/v1/rewards/member/ereceipts/transactions/details/download"
        resp = self._post(url, {"downloadUrl": details.download_url})
        if resp.status_code < 400 and resp.content.lstrip().startswith(b"%PDF"):
            return resp.content
        log.info("download proxy did not return a PDF (%s); fetching the download url directly", resp.status_code)
        bearer = self.auth.get_bearer()
        try:
            direct = self.http.get(details.download_url, headers=self._headers(bearer), follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ApiError(f"direct receipt download failed: {exc}") from exc
        if direct.status_code < 400 and direct.content.lstrip().startswith(b"%PDF"):
            return direct.content
        raise ApiError(
            "receipt download did not return a PDF",
            direct.status_code,
            (resp.text[:200], direct.text[:200]),
        )


__all__ = ["ApiError", "GraphQLError", "EverydayRewardsClient", "ReloginRequired"]
