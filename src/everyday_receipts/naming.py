"""Filename construction and transaction-date inference."""

from __future__ import annotations

import calendar
import hashlib
import re
from datetime import date, timedelta

from .config import DEFAULT_FILENAME_TEMPLATE
from .models import ActivityItem, ReceiptDetails

_MONTHS = {name[:3].lower(): index for index, name in enumerate(calendar.month_name) if name}
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_component(text: object, max_len: int = 80) -> str:
    cleaned = _BAD_CHARS.sub(" ", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    return cleaned[:max_len].rstrip()


def infer_date_from_feed(display_date: str | None, group_title: str | None, today: date) -> date | None:
    """Infer a date from 'Sat 06 Sep' plus the month group ('September 2026', 'This Month')."""
    if not display_date:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})", display_date)
    if not m:
        return None
    day = int(m.group(1))
    month = _MONTHS.get(m.group(2)[:3].lower())
    if not month:
        return None

    year: int | None = None
    title = (group_title or "").strip().lower()
    gm = re.match(r"^([a-z]+)\s+(\d{4})$", title)
    if gm:
        year = int(gm.group(2))
    elif title == "this month":
        year = today.year
    elif title == "last month":
        year = (today.replace(day=1) - timedelta(days=1)).year

    if year is None:
        year = today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate > today + timedelta(days=1):
            year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def resolve_transaction_date(item: ActivityItem, receipt: ReceiptDetails | None, today: date | None = None) -> date | None:
    today = today or date.today()
    if item.rest_datetime is not None:
        return item.rest_datetime.date()
    if receipt is not None:
        dt = receipt.transaction_datetime()
        if dt:
            return dt.date()
    ts = item.id_timestamp
    if ts:
        return ts.date()
    return infer_date_from_feed(item.display_date, item.group_title, today)


def short_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def build_filename(
    item: ActivityItem,
    receipt: ReceiptDetails | None,
    template: str = DEFAULT_FILENAME_TEMPLATE,
    today: date | None = None,
) -> str:
    tx_date = resolve_transaction_date(item, receipt, today)
    # Must be the stable id: receiptKey changes on every API call, which would give the
    # same receipt a different filename each sync.
    receipt_id = item.stable_id or item.receipt_id or item.id
    fields = {
        "date": tx_date.isoformat() if tx_date else "undated",
        "partner": sanitize_component(item.partner),
        "store": sanitize_component(item.origin or (receipt.store if receipt else "") or ""),
        "amount": sanitize_component(item.amount or (receipt.total if receipt else "") or ""),
        "short_id": short_id(receipt_id),
        "receipt_id": sanitize_component(receipt_id, 60),
        "id": sanitize_component(item.id, 60),
    }
    name = template.format(**fields)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    name = name[:180].rstrip() or short_id(receipt_id)
    return f"{name}.pdf"
