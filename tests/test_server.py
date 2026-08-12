from datetime import datetime, timezone
from decimal import Decimal

import pytest

from teslamate_mcp.server import (
    TeslaMateMCPError,
    _json_value,
    _limit,
    _parse_time,
    _parse_value,
    export_drive_data,
)


def test_parse_time_treats_naive_input_as_configured_local_time():
    parsed = _parse_time("2026-07-17T08:00:00", datetime.now(timezone.utc))
    assert parsed.isoformat() == "2026-07-17T00:00:00+00:00"


def test_parse_time_accepts_zulu_time():
    parsed = _parse_time("2026-07-17T08:00:00Z", datetime.now(timezone.utc))
    assert parsed.isoformat() == "2026-07-17T08:00:00+00:00"


def test_json_value_converts_nested_decimals_and_datetimes():
    value = {"energy": Decimal("12.5"), "at": datetime(2026, 7, 17, tzinfo=timezone.utc)}
    assert _json_value(value) == {"energy": 12.5, "at": "2026-07-17T08:00:00+08:00"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("false", False), ("42", 42), ("3.5", 3.5), ("null", None), ("parked", "parked")],
)
def test_parse_mqtt_value(raw, expected):
    assert _parse_value(raw) == expected


def test_limit_rejects_out_of_range_values():
    with pytest.raises(TeslaMateMCPError):
        _limit(0)
    with pytest.raises(TeslaMateMCPError):
        _limit(101)


def test_export_drive_data_returns_versioned_complete_dataset(monkeypatch):
    drive = {
        "id": 7,
        "car_id": 1,
        "start_date": datetime(2026, 7, 17, tzinfo=timezone.utc),
        "distance": Decimal("12.5"),
        "vehicle_name": "My Tesla",
        "vehicle_model": "3",
        "vehicle_marketing_name": "Model 3",
    }
    points = [
        {
            "date": datetime(2026, 7, 17, tzinfo=timezone.utc),
            "latitude": Decimal("31.2"),
            "longitude": Decimal("121.5"),
        }
    ]

    def fake_one(sql, params=()):
        return drive.copy() if "FROM teslamate_mcp.drives d" in sql else {"count": 1}

    monkeypatch.setattr("teslamate_mcp.server._one", fake_one)
    monkeypatch.setattr("teslamate_mcp.server._query", lambda sql, params=(): points)

    result = export_drive_data(7)

    assert result["schema"] == "teslamate.drive-export"
    assert result["schema_version"] == "1.0"
    assert result["vehicle"] == {"id": 1, "name": "My Tesla", "model": "3", "marketing_name": "Model 3"}
    assert result["drive"]["distance"] == 12.5
    assert result["telemetry"]["complete"] is True
    assert result["telemetry"]["points"][0]["latitude"] == 31.2


def test_export_drive_data_refuses_silent_truncation(monkeypatch):
    drive = {
        "id": 8,
        "car_id": 1,
        "vehicle_name": "My Tesla",
        "vehicle_model": "3",
        "vehicle_marketing_name": "Model 3",
    }

    def fake_one(sql, params=()):
        return drive.copy() if "FROM teslamate_mcp.drives d" in sql else {"count": 6}

    monkeypatch.setattr("teslamate_mcp.server._one", fake_one)

    with pytest.raises(TeslaMateMCPError, match="Retry with max_points=6"):
        export_drive_data(8, max_points=5)
