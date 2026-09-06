from datetime import date

from everyday_receipts.models import ActivityItem, ReceiptDetails
from everyday_receipts.naming import build_filename, infer_date_from_feed, resolve_transaction_date, sanitize_component

from conftest import activity_details, feed_item


def test_sanitize_component():
    assert sanitize_component('  Big W: "Toys" / Games?  ') == "Big W Toys Games"
    assert sanitize_component("a" * 100, max_len=10) == "a" * 10


def test_infer_date_from_feed_variants():
    today = date(2026, 9, 6)
    assert infer_date_from_feed("Sat 06 Sep", "September 2025", today) == date(2025, 9, 6)
    assert infer_date_from_feed("Sat 06 Sep", "This Month", today) == date(2026, 9, 6)
    assert infer_date_from_feed("Wed 20 Aug", "Last Month", today) == date(2026, 8, 20)
    assert infer_date_from_feed("Mon 15 Dec", None, date(2026, 1, 10)) == date(2025, 12, 15)
    assert infer_date_from_feed("Sat 20 Dec", "Last Month", date(2026, 1, 10)) == date(2025, 12, 20)
    assert infer_date_from_feed(None, "This Month", today) is None
    assert infer_date_from_feed("garbage", "This Month", today) is None


def test_id_timestamp_formats():
    item = ActivityItem.from_graphql(feed_item(item_id="S3060W084SN2594T1667044800"), "October 2022")
    assert item.id_timestamp.date() == date(2022, 10, 29)
    item2 = ActivityItem.from_graphql(feed_item(item_id="20200804183836062051773127"), None)
    assert item2.id_timestamp.date() == date(2020, 8, 4)
    item3 = ActivityItem.from_graphql(feed_item(item_id="abc"), None)
    assert item3.id_timestamp is None


def test_receipt_date_wins_over_id_and_feed():
    item = ActivityItem.from_graphql(feed_item(display_date="Sat 06 Sep"), "This Month")
    receipt = ReceiptDetails.from_activity_details(activity_details()["data"]["activityDetails"])
    assert resolve_transaction_date(item, receipt, date(2026, 9, 10)) == date(2026, 9, 6)
    # Without a receipt the id timestamp is used.
    assert resolve_transaction_date(item, None, date(2026, 9, 10)) == date(2022, 10, 29)
    # Without either, the feed date is used.
    item_no_ts = ActivityItem.from_graphql(feed_item(item_id="x1", display_date="Thu 03 Sep"), "This Month")
    assert resolve_transaction_date(item_no_ts, None, date(2026, 9, 10)) == date(2026, 9, 3)


def test_build_filename_default_template():
    item = ActivityItem.from_graphql(feed_item(), "This Month")
    receipt = ReceiptDetails.from_activity_details(activity_details()["data"]["activityDetails"])
    name = build_filename(item, receipt, today=date(2026, 9, 10))
    assert name.startswith("2026-09-06 Woolworths Ashfield $90.86 [")
    assert name.endswith("].pdf")
    assert name == build_filename(item, receipt, today=date(2026, 9, 10))  # deterministic


def test_build_filename_custom_template_and_sanitising():
    item = ActivityItem.from_graphql(feed_item(origin='Store/With:Bad*Chars', icon="bws"), "This Month")
    name = build_filename(item, None, template="{partner} - {store} - {receipt_id}", today=date(2026, 9, 10))
    assert name == "BWS - Store With Bad Chars - RCPT1.pdf"


def test_partner_names():
    assert ActivityItem.from_graphql(feed_item(icon="bigw"), None).partner == "Big W"
    assert ActivityItem.from_graphql(feed_item(icon="unknown_partner"), None).partner == "Woolworths"  # from iconUrl
    assert ActivityItem.from_graphql(feed_item(icon="woolworths", receipt_source="ONLINE"), None).partner == "Woolworths Online"
    assert ActivityItem.from_graphql(feed_item(icon="some_new_thing"), None).partner == "Some New Thing"


def test_details_id_fallback():
    raw = feed_item()
    raw["activityDetailsId"] = None
    item = ActivityItem.from_graphql(raw, None)
    assert item.details_id == "eyJyZWNlaXB0SWQiOiJSQ1BUMSIsInJlY2VpcHRTb3VyY2UiOiJJTlNUT1JFIn0="
    assert ActivityItem.from_graphql(feed_item(), None).details_id == "AD-RCPT1"
