import json
from decimal import Decimal

import pytest

from teslamate_mcp.privacy import LocationPrivacy, _load_aliases


PAYLOAD = {
    "from": "2026-09-13T08:00:00+00:00",
    "drive": {
        "start_address": "10 Example Road, Example City",
        "end_address": "Private Place",
        "start_city": "Example City",
        "start_geofence": "Home",
        "start_latitude": Decimal("31.234567"),
        "start_longitude": Decimal("121.567891"),
    },
    "points": [{"latitude": 31.2399, "longitude": 121.5611}],
}


def test_coarse_is_the_default(monkeypatch):
    monkeypatch.delenv("TESLAMATE_LOCATION_PRIVACY", raising=False)
    monkeypatch.delenv("TESLAMATE_LOCATION_COORD_DECIMALS", raising=False)
    monkeypatch.delenv("TESLAMATE_LOCATION_ALIASES_FILE", raising=False)

    privacy = LocationPrivacy.from_env()

    assert privacy.mode == "coarse"
    assert privacy.coordinate_decimals == 2


def test_coarse_removes_street_and_rounds_coordinates():
    result = LocationPrivacy(mode="coarse", aliases={"Home": "Home area"}).apply(PAYLOAD)

    assert result["from"] == PAYLOAD["from"]
    assert result["drive"]["start_address"] == "Example City"
    assert result["drive"]["end_address"] is None
    assert result["drive"]["start_city"] == "Example City"
    assert result["drive"]["start_geofence"] == "Home area"
    assert result["drive"]["start_latitude"] == 31.23
    assert result["drive"]["start_longitude"] == 121.57
    assert result["points"][0] == {"latitude": 31.24, "longitude": 121.56}
    assert result["location_privacy"] == {
        "mode": "coarse",
        "coordinate_decimals": 2,
    }


def test_hidden_keeps_shape_but_removes_locations():
    result = LocationPrivacy(mode="hidden").apply(PAYLOAD)

    assert result["from"] == PAYLOAD["from"]
    assert result["drive"]["start_address"] is None
    assert result["drive"]["end_address"] is None
    assert result["drive"]["start_city"] is None
    assert result["drive"]["start_geofence"] is None
    assert result["drive"]["start_latitude"] is None
    assert result["points"][0]["longitude"] is None
    assert result["location_privacy"]["mode"] == "hidden"


def test_precise_preserves_location_values():
    result = LocationPrivacy(mode="precise").apply(PAYLOAD)

    assert result["drive"]["start_address"] == "10 Example Road, Example City"
    assert result["drive"]["end_address"] == "Private Place"
    assert result["drive"]["start_latitude"] == Decimal("31.234567")
    assert result["points"][0]["longitude"] == 121.5611
    assert result["location_privacy"] == {
        "mode": "precise",
        "coordinate_decimals": None,
    }


@pytest.mark.parametrize(
    ("mode", "decimals"),
    [("invalid", 2), ("coarse", -1), ("coarse", 4)],
)
def test_invalid_privacy_configuration_fails_closed(mode, decimals):
    with pytest.raises(ValueError):
        LocationPrivacy(mode=mode, coordinate_decimals=decimals)


def test_alias_file_is_private_configuration(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"aliases": {"Private Place": "Home area"}}))

    assert _load_aliases(str(path)) == {"Private Place": "Home area"}
