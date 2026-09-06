from pathlib import Path

import pytest

from everyday_receipts.config import Settings, parse_duration


def test_parse_duration():
    assert parse_duration("30") == 30
    assert parse_duration("30m") == 1800
    assert parse_duration("6h") == 21600
    assert parse_duration("1d") == 86400
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_defaults(tmp_path):
    s = Settings.from_env({"EDR_DATA_DIR": str(tmp_path)})
    assert s.output_dir == Path("/receipts")
    assert s.json_dir == tmp_path / "json"
    assert s.poll_interval == 21600
    assert s.full_scan is False
    assert s.token_path == tmp_path / "tokens.json"
    assert s.auth0_domain.startswith("https://auth.everyday.com.au")


def test_json_dir_off(tmp_path):
    s = Settings.from_env({"EDR_DATA_DIR": str(tmp_path), "EDR_JSON_DIR": "off"})
    assert s.json_dir is None
    s2 = Settings.from_env({"EDR_DATA_DIR": str(tmp_path), "EDR_JSON_DIR": "/elsewhere"})
    assert s2.json_dir == Path("/elsewhere")
