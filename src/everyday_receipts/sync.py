"""Fetch new receipts and write them atomically into the consume folder."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .api import ApiError, EverydayRewardsClient
from .auth import ReloginRequired
from .config import Settings
from .models import ActivityItem, ReceiptDetails
from .naming import build_filename, resolve_transaction_date, sanitize_component
from .state import SyncState

log = logging.getLogger(__name__)


@dataclass
class SyncStats:
    pages: int = 0
    items: int = 0
    with_receipt: int = 0
    already_seen: int = 0
    downloaded: int = 0
    failed: int = 0
    stopped_early: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReceiptWriter:
    """Writes PDFs so that a folder watcher never sees a half-written file."""

    def __init__(self, output_dir: Path, json_dir: Path | None, subdir_by_partner: bool):
        self.output_dir = output_dir
        self.json_dir = json_dir
        self.subdir_by_partner = subdir_by_partner

    def target_dir(self, item: ActivityItem) -> Path:
        if self.subdir_by_partner:
            return self.output_dir / sanitize_component(item.partner)
        return self.output_dir

    def write_pdf(self, item: ActivityItem, filename: str, data: bytes) -> tuple[Path, bool]:
        """Return (path, written). Existing files are left untouched."""
        directory = self.target_dir(item)
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / filename
        if final.exists():
            return final, False
        # "._" prefix matches paperless-ngx's default ignore patterns, so the consumer
        # never picks up the partial file; os.replace is atomic on the same filesystem.
        tmp = directory / f"._{filename}.part"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        return final, True

    def write_json(self, stem: str, payload: dict[str, Any]) -> Path | None:
        if self.json_dir is None:
            return None
        self.json_dir.mkdir(parents=True, exist_ok=True)
        final = self.json_dir / f"{stem}.json"
        tmp = self.json_dir / f"._{stem}.json.part"
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)
        return final


class SyncService:
    def __init__(
        self,
        settings: Settings,
        client: EverydayRewardsClient,
        state: SyncState,
        writer: ReceiptWriter,
    ):
        self.settings = settings
        self.client = client
        self.state = state
        self.writer = writer

    def run_once(self) -> SyncStats:
        stats = SyncStats()
        started = time.time()
        try:
            for items in self.client.iter_activity_pages():
                stats.pages += 1
                stats.items += len(items)
                receipt_items = [item for item in items if item.has_receipt]
                stats.with_receipt += len(receipt_items)
                new_on_page = 0
                failed_on_page = 0
                for item in receipt_items:
                    assert item.receipt_id is not None
                    if self.state.is_seen(item.receipt_id):
                        stats.already_seen += 1
                        continue
                    try:
                        self._process(item)
                        new_on_page += 1
                        stats.downloaded += 1
                    except ReloginRequired:
                        raise
                    except (ApiError, OSError, ValueError) as exc:
                        failed_on_page += 1
                        stats.failed += 1
                        log.error("failed to fetch receipt %s (%s): %s", item.receipt_id, item.description, exc)
                if (
                    not self.settings.full_scan
                    and receipt_items
                    and new_on_page == 0
                    and failed_on_page == 0
                ):
                    log.debug("page %d held only known receipts; stopping", stats.pages)
                    stats.stopped_early = True
                    break
        finally:
            self.state.last_sync = {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "duration_s": round(time.time() - started, 1),
                **stats.as_dict(),
            }
            self.state.save()
        log.info(
            "sync finished: %d page(s), %d receipt(s) seen, %d new, %d already saved, %d failed",
            stats.pages, stats.with_receipt, stats.downloaded, stats.already_seen, stats.failed,
        )
        return stats

    def _process(self, item: ActivityItem) -> Path:
        details: ReceiptDetails = self.client.get_receipt_details(item)
        pdf = self.client.download_pdf(details)
        filename = build_filename(item, details, self.settings.filename_template)
        path, written = self.writer.write_pdf(item, filename, pdf)
        if written:
            log.info("saved %s (%d bytes)", path, len(pdf))
        else:
            log.info("%s already existed; not overwriting", path)
        tx_date: date | None = resolve_transaction_date(item, details)
        self.writer.write_json(
            path.stem,
            {
                "receipt_id": item.receipt_id,
                "transaction_date": tx_date.isoformat() if tx_date else None,
                "partner": item.partner,
                "store": item.origin or details.store,
                "amount": item.amount or details.total,
                "pdf": str(path),
                "activity": item.raw,
                "receipt": details.raw,
            },
        )
        assert item.receipt_id is not None
        self.state.mark_seen(
            item.receipt_id,
            {"file": str(path), "date": tx_date.isoformat() if tx_date else None, "amount": item.amount},
        )
        self.state.save()
        return path
