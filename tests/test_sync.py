from __future__ import annotations

import json
from datetime import date

import pytest

from everyday_receipts.api import ApiError
from everyday_receipts.auth import ReloginRequired
from everyday_receipts.models import ActivityItem, ReceiptDetails
from everyday_receipts.state import SyncState
from everyday_receipts.sync import ReceiptWriter, SyncService

from conftest import PDF_BYTES, activity_details, feed_item


class FakeClient:
    def __init__(self, pages: list[list[dict]], fail_receipts: set[str] | None = None, relogin: bool = False):
        self.pages = pages
        self.fail_receipts = fail_receipts or set()
        self.relogin = relogin
        self.details_calls: list[str] = []
        self.download_calls = 0

    def iter_receipt_pages(self):
        for idx, page in enumerate(self.pages):
            title = "This Month" if idx == 0 else f"Month {idx}"
            yield [ActivityItem.from_graphql(raw, title) for raw in page]

    def get_receipt_details(self, item: ActivityItem) -> ReceiptDetails:
        if self.relogin:
            raise ReloginRequired("expired")
        self.details_calls.append(item.receipt_id)
        if item.receipt_id in self.fail_receipts:
            raise ApiError("receipt unavailable")
        return ReceiptDetails.from_activity_details(
            activity_details(url=f"https://x/{item.receipt_id}.pdf", tx="POS  001  TRANS  0001   09:00  06/09/2026")["data"]["activityDetails"]
        )

    def download_pdf(self, details: ReceiptDetails) -> bytes:
        self.download_calls += 1
        return PDF_BYTES + details.download_url.encode()


def make_service(settings, client) -> tuple[SyncService, SyncState]:
    state = SyncState.load(settings.state_path)
    writer = ReceiptWriter(settings.output_dir, settings.json_dir, settings.subdir_by_partner)
    return SyncService(settings, client, state, writer), state


def test_first_run_downloads_everything_and_second_run_stops_early(settings):
    client = FakeClient([
        [feed_item(receipt_id="R1", item_id="t1"), feed_item(receipt_id=None, item_id="fuel")],
        [feed_item(receipt_id="R2", item_id="t2", origin="Marrickville", amount="$12.00")],
    ])
    service, state = make_service(settings, client)
    stats = service.run_once()
    assert (stats.pages, stats.items, stats.with_receipt, stats.downloaded, stats.already_seen, stats.failed) == (2, 3, 2, 2, 0, 0)
    assert stats.stopped_early is False

    pdfs = sorted(p.name for p in settings.output_dir.iterdir())
    assert len(pdfs) == 2
    assert pdfs[0].startswith("2026-09-06 Woolworths Ashfield $90.86 [") and pdfs[0].endswith(".pdf")
    assert pdfs[1].startswith("2026-09-06 Woolworths Marrickville $12.00 [")
    assert not any(name.startswith("._") for name in pdfs), "no partial files left behind"
    for name in pdfs:
        assert (settings.output_dir / name).read_bytes().startswith(b"%PDF")

    sidecars = sorted(p.name for p in settings.json_dir.iterdir())
    assert len(sidecars) == 2 and all(s.endswith(".json") for s in sidecars)
    payload = json.loads((settings.json_dir / sidecars[0]).read_text())
    assert payload["receipt_id"] in {"t1", "t2"}  # stable id, not the salted receiptKey
    assert payload["transaction_date"] == "2026-09-06"
    assert payload["receipt"]["tabs"]

    persisted = SyncState.load(settings.state_path)
    assert set(persisted.seen) == {"t1", "t2"}
    assert persisted.last_sync["downloaded"] == 2

    # Second run: everything on page 1 is known, so it stops there without touching the API again.
    client.details_calls.clear()
    service2, _ = make_service(settings, client)
    stats2 = service2.run_once()
    assert stats2.stopped_early is True
    assert stats2.pages == 1
    assert stats2.already_seen == 1
    assert client.details_calls == []
    assert len(list(settings.output_dir.iterdir())) == 2


def test_full_scan_walks_all_pages(settings, tmp_path):
    from everyday_receipts.config import Settings
    full = Settings.from_env({"EDR_OUTPUT_DIR": str(settings.output_dir), "EDR_DATA_DIR": str(settings.data_dir), "EDR_FULL_SCAN": "true"})
    client = FakeClient([[feed_item(receipt_id="R1", item_id="t1")], [feed_item(receipt_id="R2", item_id="t2")]])
    service, _ = make_service(full, client)
    service.run_once()
    stats = make_service(full, client)[0].run_once()
    assert stats.pages == 2 and stats.already_seen == 2 and stats.stopped_early is False


def test_failed_receipt_is_retried_next_run(settings):
    client = FakeClient([[feed_item(receipt_id="R1", item_id="t1"), feed_item(receipt_id="R2", item_id="t2")]], fail_receipts={"R2"})
    service, state = make_service(settings, client)
    stats = service.run_once()
    assert stats.downloaded == 1 and stats.failed == 1
    assert set(SyncState.load(settings.state_path).seen) == {"t1"}

    client.fail_receipts.clear()
    stats2 = make_service(settings, client)[0].run_once()
    assert stats2.downloaded == 1 and stats2.already_seen == 1
    assert set(SyncState.load(settings.state_path).seen) == {"t1", "t2"}


def test_relogin_required_propagates(settings):
    client = FakeClient([[feed_item(receipt_id="R1")]], relogin=True)
    service, _ = make_service(settings, client)
    with pytest.raises(ReloginRequired):
        service.run_once()
    assert SyncState.load(settings.state_path).last_sync is not None


def test_existing_file_not_overwritten(settings):
    client = FakeClient([[feed_item(receipt_id="R1")]])
    service, _ = make_service(settings, client)
    service.run_once()
    pdf = next(settings.output_dir.iterdir())
    pdf.write_bytes(b"%PDF-user-edited")
    settings.state_path.unlink()  # forget it was seen, forcing a re-download attempt
    make_service(settings, client)[0].run_once()
    assert pdf.read_bytes() == b"%PDF-user-edited"


def test_subdir_by_partner_and_json_off(settings):
    from everyday_receipts.config import Settings
    s = Settings.from_env({
        "EDR_OUTPUT_DIR": str(settings.output_dir), "EDR_DATA_DIR": str(settings.data_dir),
        "EDR_SUBDIR_BY_PARTNER": "true", "EDR_JSON_DIR": "off",
    })
    client = FakeClient([[feed_item(receipt_id="R1", icon="bws"), feed_item(receipt_id="R2", item_id="t2", icon="woolworths")]])
    make_service(s, client)[0].run_once()
    assert sorted(p.name for p in s.output_dir.iterdir()) == ["BWS", "Woolworths"]
    assert len(list((s.output_dir / "BWS").glob("*.pdf"))) == 1
    assert not (settings.data_dir / "json").exists()


# --------------------------------------------------------------------------- stable identity
# The REST list re-encrypts receiptKey with a random salt on every call, so it must never be
# used as identity. These cover the loop that caused: re-download -> consumer deletes -> repeat.

import uuid


def rest_row(ref="S3197W070SN1170T1789279972", basket="20260913161252070011703197",
             store="3197 Ivanhoe", total="$44.40"):
    return {
        "basketKey": basket, "EEReferenceNumber": ref, "storeName": store, "banner": "SO1005",
        "storeNo": store.split()[0], "receiptType": "instore", "total": total,
        "transactionDate": "2026-09-13 16:14:24", "date": "13/09/2026",
        "receiptDate": "2026-09-13T16:14:24+10:00",
    }


class RestFakeClient:
    """Mimics the real API: a freshly salted receiptKey on every fetch."""

    def __init__(self, rows, same_content=False):
        self.rows = rows
        self.same_content = same_content
        self.details_calls: list[str] = []
        self.downloads = 0

    def iter_receipt_pages(self):
        yield [
            ActivityItem.from_rest_list({**row, "receiptKey": "U2FsdGVkX1" + uuid.uuid4().hex})
            for row in self.rows
        ]

    def get_receipt_details(self, item):
        self.details_calls.append(item.stable_id)
        return ReceiptDetails.from_rest({
            "download": {"url": f"https://x/{item.stable_id}.pdf", "filename": "r.pdf"},
            "details": [{"__typename": "ReceiptDetailsTotal", "total": "$44.40"}],
        })

    def download_pdf(self, details):
        self.downloads += 1
        return PDF_BYTES if self.same_content else PDF_BYTES + details.download_url.encode()


def test_changing_receipt_key_does_not_cause_redownload(settings):
    client = RestFakeClient([rest_row()])
    first = make_service(settings, client)[0].run_once()
    assert first.downloaded == 1
    assert client.downloads == 1
    saved = SyncState.load(settings.state_path)
    assert list(saved.seen) == ["S3197W070SN1170T1789279972"]  # stable id, not receiptKey

    client.details_calls.clear()
    second = make_service(settings, client)[0].run_once()
    assert (second.downloaded, second.already_seen) == (0, 1)
    assert client.details_calls == [] and client.downloads == 1


def test_consumer_deleting_the_file_does_not_cause_redownload(settings):
    """paperless-ngx removes files from the consume folder; that must not look like new work."""
    client = RestFakeClient([rest_row()])
    make_service(settings, client)[0].run_once()
    pdf = next(settings.output_dir.iterdir())
    pdf.unlink()  # the consumer takes the file away

    stats = make_service(settings, client)[0].run_once()
    assert (stats.downloaded, stats.already_seen) == (0, 1)
    assert client.downloads == 1
    assert list(settings.output_dir.iterdir()) == []  # nothing written back


def test_v1_state_is_migrated_without_refetching(settings):
    """Entries saved under the old unstable key are recognised, not downloaded again."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.state_path.write_text(json.dumps({
        "seen": {
            "U2FsdGVkX1oldsaltedkey": {
                "file": str(settings.output_dir / "2026-09-13 Woolworths Ivanhoe $44.40 [abc12345].pdf"),
                "date": "2026-09-13",
                "amount": "$44.40",
            }
        },
        "last_sync": {"at": "earlier"},
    }))
    client = RestFakeClient([rest_row()])
    stats = make_service(settings, client)[0].run_once()
    assert (stats.downloaded, stats.migrated, stats.already_seen) == (0, 1, 1)
    assert client.details_calls == [] and client.downloads == 0
    assert "S3197W070SN1170T1789279972" in SyncState.load(settings.state_path).seen


def test_identical_pdf_under_a_new_id_is_not_written_again(settings):
    """Belt and braces: if identity ever changes, matching content is still not re-filed."""
    client = RestFakeClient([rest_row()], same_content=True)
    make_service(settings, client)[0].run_once()
    assert len(list(settings.output_dir.iterdir())) == 1

    # A different transaction reference, but the very same PDF bytes.
    client.rows = [rest_row(ref="DIFFERENT-REF", basket="999")]
    stats = make_service(settings, client)[0].run_once()
    assert (stats.downloaded, stats.deduped) == (0, 1)
    assert len(list(settings.output_dir.iterdir())) == 1  # not filed a second time


def test_legacy_entries_are_consumed_and_shrink_the_state_file(settings):
    """A bloated v1 file (many entries per receipt) migrates once and gets smaller."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Five loop iterations recorded the same receipt under five different unstable keys.
    bloated = {
        f"U2FsdGVkX1salt{n}": {
            "file": str(settings.output_dir / f"2026-09-13 Woolworths Ivanhoe $44.40 [hash{n}].pdf"),
            "date": "2026-09-13",
            "amount": "$44.40",
        }
        for n in range(5)
    }
    settings.state_path.write_text(json.dumps({"seen": bloated}))

    client = RestFakeClient([rest_row()])
    stats = make_service(settings, client)[0].run_once()
    assert (stats.downloaded, stats.migrated) == (0, 1)

    after = SyncState.load(settings.state_path)
    assert len(after.seen) == 1
    assert len(after.legacy) == 4  # one entry claimed; the rest remain for other receipts
    assert json.loads(settings.state_path.read_text())["version"] == 2


def test_two_receipts_same_day_and_amount_each_match_their_own_legacy_entry(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.state_path.write_text(json.dumps({"seen": {
        "old1": {"file": "/receipts/2026-09-13 Woolworths Ivanhoe $5.00 [a].pdf", "date": "2026-09-13", "amount": "$5.00"},
        "old2": {"file": "/receipts/2026-09-13 Woolworths Ivanhoe $5.00 [b].pdf", "date": "2026-09-13", "amount": "$5.00"},
    }}))
    client = RestFakeClient([
        rest_row(ref="REF-A", basket="1", total="$5.00"),
        rest_row(ref="REF-B", basket="2", total="$5.00"),
    ])
    stats = make_service(settings, client)[0].run_once()
    assert (stats.downloaded, stats.migrated) == (0, 2)
    assert client.downloads == 0
