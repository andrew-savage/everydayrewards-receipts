"""Persistent record of which receipts have already been written.

Receipts are tracked by a *stable* identity. The REST list's ``receiptKey`` is a freshly
salted ciphertext on every request, so it must never be used here: keying on it makes every
sync think every receipt is new, which re-downloads the lot and (when the consumer removes
files) loops forever.

Version 1 of this file was keyed that way. On upgrade those entries are collapsed into a
compact ``legacy`` list and used only to recognise receipts that were already saved under an
old identity, so the fix does not trigger one more full re-download.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


class SyncState:
    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, dict[str, Any]] = {}
        self.last_sync: dict[str, Any] | None = None
        # Legacy entries indexed by (date, amount) so migration stays fast even when a
        # previous version's re-download loop bloated the file.
        self._legacy: dict[tuple[Any, Any], list[dict[str, Any]]] = {}

    @property
    def legacy(self) -> list[dict[str, Any]]:
        return [entry for bucket in self._legacy.values() for entry in bucket]

    def _index_legacy(self, entries: list[dict[str, Any]]) -> None:
        self._legacy = {}
        for entry in entries:
            if isinstance(entry, dict):
                self._legacy.setdefault((entry.get("date"), entry.get("amount")), []).append(entry)

    # -- loading ---------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        state = cls(path)
        if not path.exists():
            return state
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        version = int(data.get("version") or 1)
        if version >= SCHEMA_VERSION:
            state.seen = dict(data.get("seen") or {})
            state._index_legacy(list(data.get("legacy") or []))
        else:
            state._index_legacy(cls._legacy_from_v1(data.get("seen") or {}))
        state.last_sync = data.get("last_sync")
        return state

    @staticmethod
    def _legacy_from_v1(seen: dict[str, Any]) -> list[dict[str, Any]]:
        """Collapse v1 entries (keyed by an unstable id) to unique date/amount/file triples."""
        unique: dict[tuple, dict[str, Any]] = {}
        for info in seen.values():
            if not isinstance(info, dict):
                continue
            key = (info.get("date"), info.get("amount"), info.get("file"))
            if key == (None, None, None):
                continue
            unique[key] = {"date": info.get("date"), "amount": info.get("amount"), "file": info.get("file")}
        return list(unique.values())

    # -- queries ---------------------------------------------------------------

    def is_seen(self, stable_id: str) -> bool:
        return stable_id in self.seen

    def content_seen(self, sha256: str) -> bool:
        """True if a PDF with this exact content was already written (under any identity)."""
        return any(entry.get("sha256") == sha256 for entry in self.seen.values())

    def match_legacy(self, *, date: str | None, amount: str | None, store: str | None) -> bool:
        """Claim a pre-upgrade entry matching this receipt (same day, amount and store).

        The entry is consumed so the file shrinks as the migration proceeds, and so two
        genuinely different receipts that share a day and amount each match their own entry.
        """
        if not date or not amount:
            return False
        bucket = self._legacy.get((date, amount))
        if not bucket:
            return False
        for index, entry in enumerate(bucket):
            if store and store not in (entry.get("file") or ""):
                continue
            bucket.pop(index)
            if not bucket:
                self._legacy.pop((date, amount), None)
            return True
        return False

    # -- writes ----------------------------------------------------------------

    def mark_seen(self, stable_id: str, info: dict[str, Any]) -> None:
        self.seen[stable_id] = {**info, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "version": SCHEMA_VERSION,
            "seen": self.seen,
            "legacy": self.legacy,
            "last_sync": self.last_sync,
        }
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
