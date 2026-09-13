"""Plain data models for activity-feed items and receipt details."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

PARTNER_NAMES = {
    "woolworths": "Woolworths",
    "supermarkets": "Woolworths",
    "metro": "Woolworths Metro",
    "bws": "BWS",
    "bigw": "Big W",
    "big_w": "Big W",
    "everyday_market": "Everyday Market",
    "online": "Woolworths Online",
    "eg": "EG Ampol",
    "eg_ampol": "EG Ampol",
    "ampol": "Ampol",
    "caltex_woolworths": "Caltex Woolworths",
    "healthylife": "HealthyLife",
    "petculture": "Pet Culture",
    "milkrun": "MILKRUN",
}

_TX_DATETIME_RE = re.compile(r"(\d{1,2}):(\d{2})\D{1,6}(\d{1,2})/(\d{1,2})/(\d{4})")
_TX_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_STORE_NUMBER_PREFIX = re.compile(r"^\d{2,6}\s+")

# Best-effort partner from the REST list's `banner` code / `receiptType`.
_BANNER_PARTNERS = (
    ("BWS", "BWS"),
    ("BIGW", "Big W"),
    ("BIG_W", "Big W"),
    ("METRO", "Woolworths Metro"),
    ("AMPOL", "Ampol"),
    ("EG", "EG Ampol"),
    ("CALTEX", "Caltex Woolworths"),
)


def _clean_store(name: str | None) -> str | None:
    """Drop a leading store number, e.g. '3197 Ivanhoe' -> 'Ivanhoe'."""
    if not name:
        return name
    return _STORE_NUMBER_PREFIX.sub("", name).strip() or name


def _parse_rest_datetime(item: dict[str, Any]) -> datetime | None:
    for key, fmt in (("transactionDate", "%Y-%m-%d %H:%M:%S"), ("date", "%d/%m/%Y")):
        value = item.get(key)
        if value:
            try:
                return datetime.strptime(str(value), fmt)
            except ValueError:
                pass
    iso = item.get("receiptDate")
    if iso:
        try:
            return datetime.fromisoformat(str(iso))
        except ValueError:
            return None
    return None


@dataclass
class ActivityItem:
    id: str
    display_date: str | None
    description: str | None
    icon: str | None
    icon_url: str | None
    activity_details_id: str | None
    receipt_id: str | None
    receipt_source: str | None
    transaction_type: str | None
    origin: str | None
    amount: str | None
    group_title: str | None
    rest_datetime: datetime | None = None
    rest_partner: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rest_list(cls, item: dict[str, Any]) -> "ActivityItem":
        """Build from one entry of /wx/v1/rewards/member/ereceipts/transactions/list."""
        store = _clean_store(item.get("storeName"))
        amount = item.get("total") or item.get("totalSpent")
        receipt_type = (item.get("receiptType") or "").strip().lower()
        banner = (item.get("banner") or "").upper()
        partner = "Woolworths"
        for prefix, name in _BANNER_PARTNERS:
            if banner.startswith(prefix):
                partner = name
                break
        else:
            if receipt_type == "online":
                partner = "Woolworths Online"
        # NB: never fall back to receiptKey for identity - it is a freshly salted
        # ciphertext that differs on every API call.
        identity = item.get("EEReferenceNumber") or item.get("basketKey")
        if not identity:
            identity = "|".join(str(item.get(k) or "") for k in ("receiptDate", "storeNo", "total"))
        return cls(
            id=str(identity),
            display_date=item.get("date"),
            description=f"{amount} at {store}" if amount and store else (store or item.get("date")),
            icon=None,
            icon_url=None,
            activity_details_id=None,
            receipt_id=item.get("receiptKey") or None,
            receipt_source=receipt_type or None,
            transaction_type="purchase",
            origin=store,
            amount=amount,
            group_title=None,
            rest_datetime=_parse_rest_datetime(item),
            rest_partner=partner,
            raw=item,
        )

    @classmethod
    def from_graphql(cls, item: dict[str, Any], group_title: str | None) -> "ActivityItem":
        receipt = item.get("receipt") or {}
        tx = item.get("transaction") or {}
        return cls(
            id=str(item.get("id")),
            display_date=item.get("displayDate"),
            description=item.get("description"),
            icon=item.get("icon"),
            icon_url=item.get("iconUrl"),
            activity_details_id=item.get("activityDetailsId"),
            receipt_id=receipt.get("receiptId"),
            receipt_source=receipt.get("receiptSource"),
            transaction_type=item.get("transactionType"),
            origin=tx.get("origin"),
            amount=tx.get("amountAsDollars"),
            group_title=group_title,
            raw=item,
        )

    @property
    def has_receipt(self) -> bool:
        return bool(self.receipt_id)

    @property
    def stable_id(self) -> str:
        """Identity that survives across API calls: use this for dedupe and filenames.

        The REST list's ``receiptKey`` is re-encrypted with a random salt on every request,
        so it changes each time and must never be used to decide whether a receipt is new.
        ``id`` is the transaction reference (EEReferenceNumber / basketKey / GraphQL id),
        which is stable.
        """
        return self.id or self.receipt_id or ""

    @property
    def details_id(self) -> str:
        """Identifier for the activityDetails GraphQL query.

        The server supplies one (activityDetailsId). If it is missing we rebuild it the
        way the app does: base64 of the JSON receipt reference.
        """
        if self.activity_details_id:
            return self.activity_details_id
        payload: dict[str, Any] = {"receiptId": self.receipt_id}
        if self.receipt_source:
            payload["receiptSource"] = self.receipt_source
        return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()

    @property
    def partner(self) -> str:
        if self.rest_partner:
            return self.rest_partner
        key = (self.icon or "").strip().lower()
        if key in ("", "unknown_partner") and self.icon_url:
            base = self.icon_url.rsplit("/", 1)[-1].lower()
            base = re.sub(r"\.(png|svg|jpg|jpeg|webp)$", "", base)
            key = base.split("_logo")[0].split("_division")[0]
        if (self.receipt_source or "").upper() == "ONLINE" and key in ("woolworths", "supermarkets", ""):
            return "Woolworths Online"
        if key in PARTNER_NAMES:
            return PARTNER_NAMES[key]
        return key.replace("_", " ").title() if key else "Everyday Rewards"

    @property
    def id_timestamp(self) -> datetime | None:
        """Some transaction ids embed the transaction time; recover it when they do."""
        m = re.search(r"T(\d{10})$", self.id)
        if m:
            return datetime.fromtimestamp(int(m.group(1)))
        m = re.match(r"^(\d{14})\d*$", self.id)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
            except ValueError:
                return None
        return None


@dataclass
class ReceiptDetails:
    kind: str  # "ReceiptDetails" (in store) or "OnlineReceiptDetails"
    download_url: str
    filename: str | None
    sections: dict[str, list[dict[str, Any]]]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_activity_details(cls, activity_details: dict[str, Any]) -> "ReceiptDetails":
        errors: list[dict[str, Any]] = []
        for tab in activity_details.get("tabs") or []:
            page = tab.get("page") or {}
            kind = page.get("__typename")
            if kind == "ActivityDetailsTabError":
                errors.append(page)
                continue
            if kind not in ("ReceiptDetails", "OnlineReceiptDetails"):
                continue
            download = page.get("download") or {}
            if not download.get("url"):
                continue
            sections: dict[str, list[dict[str, Any]]] = {}
            for section in (page.get("details") or []) + (page.get("cards") or []):
                if not isinstance(section, dict):
                    continue
                sections.setdefault(section.get("__typename") or "Unknown", []).append(section)
            return cls(
                kind=kind,
                download_url=download["url"],
                filename=download.get("filename"),
                sections=sections,
                raw=activity_details,
            )
        if errors:
            raise ValueError(f"receipt not available: {errors[0].get('title')}: {errors[0].get('message')}")
        raise ValueError("activity details contained no downloadable receipt")

    @classmethod
    def from_rest(cls, receipt_details: dict[str, Any]) -> "ReceiptDetails":
        """Build from the legacy REST /ereceipts/transactions/details response."""
        download = receipt_details.get("download") or {}
        if not download.get("url"):
            raise ValueError("REST receipt details contained no download url")
        sections: dict[str, list[dict[str, Any]]] = {}
        for section in receipt_details.get("details") or []:
            if isinstance(section, dict):
                sections.setdefault(section.get("__typename") or "Unknown", []).append(section)
        return cls(
            kind="ReceiptDetails",
            download_url=download["url"],
            filename=download.get("filename"),
            sections=sections,
            raw={"receiptDetails": receipt_details},
        )

    def first(self, typename: str, key: str) -> Any:
        for section in self.sections.get(typename, []):
            value = section.get(key)
            if value:
                return value
        return None

    @property
    def total(self) -> str | None:
        return self.first("ReceiptDetailsTotal", "total") or self.first("OnlineReceiptTotalCard", "total")

    @property
    def store(self) -> str | None:
        return self.first("ReceiptDetailsHeader", "title") or self.first("OnlineReceiptHeaderCard", "heading")

    @property
    def transaction_details(self) -> str | None:
        return self.first("ReceiptDetailsFooter", "transactionDetails")

    def transaction_datetime(self) -> datetime | None:
        text = self.transaction_details
        if not text:
            return None
        m = _TX_DATETIME_RE.search(text)
        try:
            if m:
                hour, minute, day, month, year = (int(g) for g in m.groups())
                return datetime(year, month, day, hour, minute)
            m = _TX_DATE_RE.search(text)
            if m:
                day, month, year = (int(g) for g in m.groups())
                return datetime(year, month, day)
        except ValueError:
            return None
        return None
