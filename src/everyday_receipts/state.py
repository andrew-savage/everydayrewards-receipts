"""Persistent record of which receipts have already been written."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class SyncState:
    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, dict[str, Any]] = {}
        self.last_sync: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        state = cls(path)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            state.seen = dict(data.get("seen") or {})
            state.last_sync = data.get("last_sync")
        return state

    def is_seen(self, receipt_id: str) -> bool:
        return receipt_id in self.seen

    def mark_seen(self, receipt_id: str, info: dict[str, Any]) -> None:
        self.seen[receipt_id] = {**info, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"seen": self.seen, "last_sync": self.last_sync}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
