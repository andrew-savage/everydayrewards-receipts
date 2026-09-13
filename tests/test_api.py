from __future__ import annotations

import json

import httpx
import pytest

from everyday_receipts.api import ApiError, EverydayRewardsClient, GraphQLError
from everyday_receipts.auth import AuthError, AuthManager, TokenStore, ApigeeTokens
from everyday_receipts.models import ActivityItem

from conftest import PDF_BYTES, Recorder, activity_details, feed_item, feed_page, make_client


class FakeAuth:
    """Stand-in for AuthManager: returns OLD until a forced refresh, then NEW."""

    def __init__(self):
        self.token = "OLD"
        self.forced = 0

    def get_bearer(self, force_refresh: bool = False) -> str:
        if force_refresh:
            self.forced += 1
            self.token = "NEW"
        return self.token

    def api_headers(self, client_id: str) -> dict[str, str]:
        return {"client_id": client_id, "api-version": "2", "Content-Type": "application/json"}


def _client(settings, handler, rec=None) -> tuple[EverydayRewardsClient, FakeAuth]:
    auth = FakeAuth()
    client = EverydayRewardsClient(settings, auth, make_client(handler, rec), sleep=lambda s: None)
    return client, auth


def test_activity_feed_pagination(settings):
    rec = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer OLD"
        assert request.headers["client_id"] == settings.rewards_client_id
        page = body["variables"]["page"]
        if page == "FIRST_PAGE":
            return httpx.Response(200, json=feed_page([feed_item(receipt_id="R1"), feed_item(item_id="fuel", receipt_id=None)], "This Month", "PAGE2"))
        if page == "PAGE2":
            return httpx.Response(200, json=feed_page([feed_item(receipt_id="R2")], "August 2026", None))
        raise AssertionError(page)

    client, _ = _client(settings, handler, rec)
    pages = list(client.iter_activity_pages())
    assert [len(p) for p in pages] == [2, 1]
    assert pages[0][0].receipt_id == "R1" and pages[0][0].group_title == "This Month"
    assert pages[0][1].has_receipt is False
    assert pages[1][0].group_title == "August 2026"
    assert rec.paths() == ["apigee-prod.api-wr.com/wx/v1/bff/graphql"] * 2


def test_401_triggers_forced_refresh_and_retry(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer OLD":
            return httpx.Response(401, json={"errors": [{"status": 401, "code": "1007", "message": "Access Token Invalid"}]})
        return httpx.Response(200, json=feed_page([feed_item()]))

    client, auth = _client(settings, handler)
    pages = list(client.iter_activity_pages())
    assert auth.forced == 1
    assert len(pages[0]) == 1


def test_persistent_401_raises_auth_error(settings):
    client, auth = _client(settings, lambda r: httpx.Response(401, json={"errors": [{"code": "1007"}]}))
    with pytest.raises(AuthError):
        list(client.iter_activity_pages())
    assert auth.forced == 1


def test_server_error_retries_then_succeeds(settings):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, text="upstream")
        return httpx.Response(200, json=feed_page([feed_item()]))

    client, _ = _client(settings, handler)
    assert len(list(client.iter_activity_pages())[0]) == 1
    assert attempts["n"] == 2


def test_server_error_gives_up(settings):
    client, _ = _client(settings, lambda r: httpx.Response(502, text="bad"))
    with pytest.raises(ApiError):
        list(client.iter_activity_pages())


def test_graphql_errors_without_data_raise(settings):
    client, _ = _client(settings, lambda r: httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None}))
    with pytest.raises(GraphQLError):
        list(client.iter_activity_pages())


def test_receipt_details_parsing(settings_graphql):
    settings = settings_graphql
    rec = Recorder()
    client, _ = _client(settings, lambda r: httpx.Response(200, json=activity_details()), rec)
    item = ActivityItem.from_graphql(feed_item(), "This Month")
    details = client.get_receipt_details(item)
    assert rec.json_body(0)["variables"] == {"id": "AD-RCPT1"}
    assert details.kind == "ReceiptDetails"
    assert details.download_url == "https://receipts.example/r1.pdf"
    assert details.total == "$90.86"
    assert details.store == "Woolworths Ashfield"
    assert details.transaction_datetime().isoformat() == "2026-09-06T12:34:00"
    assert details.sections["ReceiptDetailsItems"][0]["items"][0]["description"] == "Milk 2L"


def test_receipt_details_schema_fallback(settings_graphql):
    settings = settings_graphql
    rec = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "ActivityDetailsMinimal" in body["query"]:
            return httpx.Response(200, json={"data": {"activityDetails": {"tabs": [{"page": {"__typename": "ReceiptDetails", "download": {"url": "https://x/min.pdf", "filename": "m.pdf"}}}]}}})
        return httpx.Response(200, json={"errors": [{"message": 'Cannot query field "storeNo" on type "ReceiptDetailsHeader".'}], "data": None})

    client, _ = _client(settings, handler, rec)
    details = client.get_receipt_details(ActivityItem.from_graphql(feed_item(), None))
    assert details.download_url == "https://x/min.pdf"
    assert len(rec.requests) == 2


def test_receipt_details_rest_fallback_when_graphql_has_no_receipt(settings):
    rec = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wx/v1/bff/graphql":
            return httpx.Response(200, json={"data": {"activityDetails": {"tabs": [{"page": {"__typename": "ActivityDetailsTabError", "title": "Receipt unavailable", "message": "try later"}}]}}})
        if request.url.path == "/wx/v1/rewards/member/ereceipts/transactions/details":
            assert json.loads(request.content) == {"receiptKey": "RCPT1"}
            return httpx.Response(200, json={"data": {"receiptDetails": {"download": {"url": "https://x/rest.pdf", "filename": "r.pdf"}, "details": [{"__typename": "ReceiptDetailsTotal", "total": "$1.00"}]}}})
        raise AssertionError(request.url)

    client, _ = _client(settings, handler, rec)
    details = client.get_receipt_details(ActivityItem.from_graphql(feed_item(), None))
    assert details.download_url == "https://x/rest.pdf"
    assert details.total == "$1.00"


def test_receipt_details_unavailable_everywhere(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wx/v1/bff/graphql":
            return httpx.Response(200, json={"data": {"activityDetails": None}})
        return httpx.Response(200, json={"data": {"receiptDetails": {}}})

    client, _ = _client(settings, handler)
    with pytest.raises(ApiError):
        client.get_receipt_details(ActivityItem.from_graphql(feed_item(), None))


def test_download_pdf_via_proxy(settings):
    rec = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wx/v1/rewards/member/ereceipts/transactions/details/download"
        assert json.loads(request.content) == {"downloadUrl": "https://receipts.example/r1.pdf"}
        return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})

    client, _ = _client(settings, handler, rec)
    from everyday_receipts.models import ReceiptDetails
    details = ReceiptDetails.from_activity_details(activity_details()["data"]["activityDetails"])
    assert client.download_pdf(details) == PDF_BYTES


def test_download_pdf_falls_back_to_direct_get(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"note": "not a pdf"}})
        assert request.method == "GET" and str(request.url) == "https://receipts.example/r1.pdf"
        assert request.headers["Authorization"] == "Bearer OLD"
        return httpx.Response(200, content=PDF_BYTES)

    client, _ = _client(settings, handler)
    from everyday_receipts.models import ReceiptDetails
    details = ReceiptDetails.from_activity_details(activity_details()["data"]["activityDetails"])
    assert client.download_pdf(details) == PDF_BYTES


def test_download_pdf_failure(settings):
    client, _ = _client(settings, lambda r: httpx.Response(200, text="<html>nope</html>"))
    from everyday_receipts.models import ReceiptDetails
    details = ReceiptDetails.from_activity_details(activity_details()["data"]["activityDetails"])
    with pytest.raises(ApiError):
        client.download_pdf(details)


def test_real_auth_manager_headers_are_used(settings):
    """The client must send the bearer from a real AuthManager with the rewards client id."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["cid"] = request.headers["client_id"]
        seen["ua"] = request.headers["User-Agent"]
        return httpx.Response(200, json=feed_page([]))

    http = make_client(handler)
    store = TokenStore(mode="static", path=settings.token_path)
    store.apigee = ApigeeTokens(access_token="STATIC", expires_at=9e12)
    client = EverydayRewardsClient(settings, AuthManager(settings, store, http), http)
    list(client.iter_activity_pages())
    assert seen == {"auth": "Bearer STATIC", "cid": settings.rewards_client_id, "ua": settings.user_agent}


# --------------------------------------------------------------------------- REST feed

REST_ITEM = {
    "basketKey": "20260913161252070011703197",
    "storeName": "3197 Ivanhoe",
    "banner": "SO1005",
    "storeNo": "3197",
    "receiptType": "instore",
    "receiptKind": ["MainReceipt"],
    "pointsEarned": "44",
    "receiptDate": "2026-09-13T16:14:24+10:00",
    "EEReferenceNumber": "S3197W070SN1170T1789279972",
    "total": "$44.40",
    "transactionDate": "2026-09-13 16:14:24",
    "receiptKey": "KEY-ABC",
    "date": "13/09/2026",
}


def test_rest_transaction_pages(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "/wx/v1/rewards/member/ereceipts/transactions/list" in str(request.url)
        assert request.headers["Authorization"] == "Bearer OLD"
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json={"data": [REST_ITEM, {**REST_ITEM, "receiptKey": ""}]})
        if page == 2:
            return httpx.Response(200, json={"data": [{**REST_ITEM, "receiptKey": "KEY-DEF", "storeName": "1248 Town Hall"}]})
        return httpx.Response(200, json={"data": []})

    client, _ = _client(settings, handler)
    pages = list(client.iter_receipt_pages())
    assert [len(p) for p in pages] == [1, 1]  # empty receiptKey filtered out; stops on empty page
    item = pages[0][0]
    assert item.receipt_id == "KEY-ABC"
    assert item.origin == "Ivanhoe"  # store number stripped
    assert item.amount == "$44.40"
    assert item.partner == "Woolworths"
    assert item.rest_datetime.isoformat() == "2026-09-13T16:14:24"
    assert pages[1][0].origin == "Town Hall"


def test_rest_feed_is_default_and_details_go_rest(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/transactions/details"):
            assert json.loads(request.content) == {"receiptKey": "KEY-ABC"}
            return httpx.Response(200, json={"data": {"receiptDetails": {"download": {"url": "https://x/r.pdf", "filename": "r.pdf"}, "details": [{"__typename": "ReceiptDetailsTotal", "total": "$44.40"}]}}})
        raise AssertionError("unexpected " + str(request.url))

    client, _ = _client(settings, handler)
    item = ActivityItem.from_rest_list(REST_ITEM)
    details = client.get_receipt_details(item)
    assert details.download_url == "https://x/r.pdf"
    assert all("graphql" not in c for c in calls)  # never touches GraphQL in rest mode


def test_rest_list_non_list_is_error(settings):
    client, _ = _client(settings, lambda r: httpx.Response(200, json={"data": {"oops": True}}))
    with pytest.raises(ApiError):
        list(client.iter_receipt_pages())


# --------------------------------------------------------------------------- multi-line paste reader

def test_read_json_block_multiline_and_single_line():
    from everyday_receipts.cli import _read_json_block

    pretty = '{\n  "access_token": "ey.J.s",\n  "refresh_token": "r",\n  "expires_in": 86400\n}'
    lines = iter(pretty.split("\n"))
    text = _read_json_block(read_line=lambda: next(lines))
    import json as _json
    assert _json.loads(text)["refresh_token"] == "r"

    # A single-line authStatusData (braces are inside a quoted string) returns after one line.
    one = '"{\\"reason\\":\\"AUTHENTICATED\\",\\"access_token\\":\\"A\\"}"'
    called = {"n": 0}

    def one_then_block():
        called["n"] += 1
        if called["n"] == 1:
            return one
        raise AssertionError("should not read a second line for a single-line paste")

    assert _read_json_block(read_line=one_then_block) == one
