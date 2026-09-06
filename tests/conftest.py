from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from everyday_receipts.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "EDR_OUTPUT_DIR": str(tmp_path / "receipts"),
            "EDR_DATA_DIR": str(tmp_path / "data"),
            "EDR_POLL_INTERVAL": "1h",
        }
    )


class Recorder:
    """Collects requests made through a MockTransport."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [f"{r.url.host}{r.url.path}" for r in self.requests]

    def json_body(self, index: int) -> Any:
        return json.loads(self.requests[index].content.decode())

    def form_body(self, index: int) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.requests[index].content.decode()).items()}


def make_client(handler, recorder: Recorder | None = None) -> httpx.Client:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.requests.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped))


def feed_item(
    item_id: str = "S3060W084SN2594T1667044800",
    receipt_id: str | None = "RCPT1",
    origin: str = "Ashfield",
    amount: str = "$90.86",
    display_date: str = "Sat 06 Sep",
    icon: str = "woolworths",
    receipt_source: str = "INSTORE",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "displayDate": display_date,
        "description": f"{amount} at {origin}",
        "message": None,
        "displayValue": "+ 44 pts",
        "icon": icon,
        "iconUrl": "https://cdn/x/supermarkets_division_logo.png",
        "activityDetailsId": f"AD-{receipt_id}" if receipt_id else None,
        "transaction": {"origin": origin, "amountAsDollars": amount},
        "receipt": {"receiptId": receipt_id, "receiptSource": receipt_source} if receipt_id else None,
        "transactionType": "purchase",
    }


def feed_page(items: list[dict[str, Any]], title: str = "This Month", next_token: str | None = None) -> dict[str, Any]:
    return {
        "data": {
            "rtlRewardsActivityFeed": {
                "list": {
                    "groups": [{"__typename": "RewardsActivityFeedGroup", "id": "g1", "title": title, "items": items}],
                    "nextPageToken": next_token,
                }
            }
        }
    }


def activity_details(url: str = "https://receipts.example/r1.pdf", tx: str = "POS  003  TRANS  1234   12:34  06/09/2026") -> dict[str, Any]:
    return {
        "data": {
            "activityDetails": {
                "__typename": "ActivityDetails",
                "tabs": [
                    {"__typename": "Tab", "label": "Points", "page": {"__typename": "ActivityBreakdown"}},
                    {
                        "__typename": "Tab",
                        "label": "Receipt",
                        "page": {
                            "__typename": "ReceiptDetails",
                            "download": {"url": url, "filename": "receipt.pdf"},
                            "details": [
                                {"__typename": "ReceiptDetailsHeader", "title": "Woolworths Ashfield", "content": "1 Liverpool Rd", "storeNo": "3060"},
                                {"__typename": "ReceiptDetailsItems", "items": [{"prefixChar": "", "description": "Milk 2L", "amount": "3.10"}]},
                                {"__typename": "ReceiptDetailsTotal", "total": "$90.86"},
                                {"__typename": "ReceiptDetailsFooter", "transactionDetails": tx, "abnAndStore": "ABN 88 000 014 675"},
                            ],
                        },
                    },
                ],
            }
        }
    }


PDF_BYTES = b"%PDF-1.4\n%fake receipt\n%%EOF\n"
