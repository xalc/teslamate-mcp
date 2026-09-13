from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal


PrivacyMode = Literal["hidden", "coarse", "precise"]

_COORDINATE_KEYS = {
    "latitude",
    "longitude",
    "start_latitude",
    "start_longitude",
    "end_latitude",
    "end_longitude",
}
_PLACE_KEYS = {
    "address",
    "start_address",
    "end_address",
    "entry_name",
    "exit_name",
    "from",
    "to",
    "bucket",
    "city",
    "geofence",
}


def _looks_like_time(value: str) -> bool:
    normalized = value.strip().replace("Z", "+00:00")
    if not normalized:
        return False
    try:
        datetime.fromisoformat(normalized)
        return True
    except ValueError:
        try:
            date.fromisoformat(normalized)
            return True
        except ValueError:
            return False


def _load_aliases(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    source = raw.get("aliases", raw) if isinstance(raw, dict) else None
    if not isinstance(source, dict):
        raise ValueError("location aliases file must contain a JSON object")
    aliases: dict[str, str] = {}
    for original, public_name in source.items():
        if not isinstance(original, str) or not isinstance(public_name, str):
            raise ValueError("location aliases must map strings to strings")
        if original.strip() and public_name.strip():
            aliases[original.strip()] = public_name.strip()
    return aliases


@dataclass(frozen=True)
class LocationPrivacy:
    mode: PrivacyMode = "coarse"
    coordinate_decimals: int = 2
    aliases: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"hidden", "coarse", "precise"}:
            raise ValueError("location privacy must be hidden, coarse, or precise")
        if not 0 <= self.coordinate_decimals <= 3:
            raise ValueError("coordinate decimals must be between 0 and 3")

    @classmethod
    def from_env(cls) -> "LocationPrivacy":
        return cls(
            mode=os.environ.get("TESLAMATE_LOCATION_PRIVACY", "coarse").strip().lower(),  # type: ignore[arg-type]
            coordinate_decimals=int(
                os.environ.get("TESLAMATE_LOCATION_COORD_DECIMALS", "2")
            ),
            aliases=_load_aliases(os.environ.get("TESLAMATE_LOCATION_ALIASES_FILE")),
        )

    def _coordinate(self, value: Any) -> Any:
        if self.mode == "hidden" or value is None:
            return None
        if self.mode == "precise":
            return value
        if isinstance(value, Decimal):
            value = float(value)
        try:
            return round(float(value), self.coordinate_decimals)
        except (TypeError, ValueError):
            return None

    def _place(self, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return None if self.mode != "precise" else value
        if _looks_like_time(value):
            return value
        if self.mode == "hidden":
            return None
        if self.mode == "precise":
            return value
        aliases = self.aliases or {}
        stripped = value.strip()
        if stripped in aliases:
            return aliases[stripped]
        # TeslaMate's fallback address is "road house_number, city". Returning
        # only the final component avoids exposing a street or house number.
        parts = [part.strip() for part in stripped.split(",") if part.strip()]
        return parts[-1] if len(parts) > 1 else None

    def _filter(self, value: Any, key: str | None = None) -> Any:
        normalized_key = (key or "").lower()
        if normalized_key in _COORDINATE_KEYS:
            return self._coordinate(value)
        if normalized_key.endswith("_city") or normalized_key == "city":
            if self.mode == "hidden":
                return None
            if self.mode == "precise" or not isinstance(value, str):
                return value
            return (self.aliases or {}).get(value.strip(), value.strip()) or None
        if normalized_key.endswith("_geofence") or normalized_key == "geofence":
            if self.mode == "hidden":
                return None
            if self.mode == "precise" or not isinstance(value, str):
                return value
            return (self.aliases or {}).get(value.strip(), value.strip()) or None
        if normalized_key in _PLACE_KEYS or normalized_key.endswith("_address"):
            return self._place(value)
        if normalized_key == "location" and not isinstance(value, (dict, list)):
            return self._place(value)
        if isinstance(value, dict):
            return {item_key: self._filter(item, item_key) for item_key, item in value.items()}
        if isinstance(value, list):
            return [self._filter(item) for item in value]
        return value

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        filtered = self._filter(payload)
        filtered["location_privacy"] = {
            "mode": self.mode,
            "coordinate_decimals": (
                self.coordinate_decimals if self.mode == "coarse" else None
            ),
        }
        return filtered
