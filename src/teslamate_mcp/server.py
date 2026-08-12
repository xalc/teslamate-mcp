from __future__ import annotations

import argparse
import functools
import json
import logging
import math
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
import psycopg
from psycopg.rows import dict_row
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("teslamate_mcp")

LOCAL_TZ = ZoneInfo(os.environ.get("TESLAMATE_TIMEZONE", "Asia/Shanghai"))
UTC = timezone.utc
DB_HOST = os.environ.get("TESLAMATE_DB_HOST", "database")
DB_PORT = int(os.environ.get("TESLAMATE_DB_PORT", "5432"))
DB_NAME = os.environ.get("TESLAMATE_DB_NAME", "teslamate")
DB_USER = os.environ.get("TESLAMATE_DB_USER", "teslamate_mcp")
MQTT_HOST = os.environ.get("TESLAMATE_MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("TESLAMATE_MQTT_PORT", "1883"))
WEB_URL = os.environ.get("TESLAMATE_WEB_URL", "http://teslamate:4000/")


def _read_secret(env_name: str, file_env_name: str) -> str:
    value = os.environ.get(env_name, "")
    path = os.environ.get(file_env_name, "")
    if not value and path:
        value = Path(path).read_text().strip()
    return value


DB_PASSWORD = _read_secret("TESLAMATE_DB_PASSWORD", "TESLAMATE_DB_PASSWORD_FILE")
MCP_TOKEN = _read_secret("TESLAMATE_MCP_TOKEN", "TESLAMATE_MCP_TOKEN_FILE")
ALLOWED_HOSTS = [
    item.strip()
    for item in os.environ.get(
        "TESLAMATE_MCP_ALLOWED_HOSTS",
        "127.0.0.1:*,localhost:*,100.84.50.63:*,main-oci.tail7affc2.ts.net:*",
    ).split(",")
    if item.strip()
]


class TeslaMateMCPError(RuntimeError):
    pass


def _connect() -> psycopg.Connection:
    if not DB_PASSWORD:
        raise TeslaMateMCPError("database password is not configured")
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on -c statement_timeout=5000",
        connect_timeout=5,
    )


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    started = time.monotonic()
    with _connect() as con, con.cursor() as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    logger.info(json.dumps({
        "event": "db_query", "rows": len(rows),
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }))
    return rows


def _one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


def _parse_value(value: str) -> Any:
    value = value.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"null", "none", ""}:
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value


class MQTTCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, tuple[Any, datetime]] = {}
        self.connected = False
        self.error: str | None = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="teslamate-mcp-readonly")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        self.connected = reason_code == 0
        self.error = None if self.connected else f"MQTT connect failed: {reason_code}"
        if self.connected:
            client.subscribe("teslamate/cars/#", qos=0)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        self.connected = False
        if reason_code != 0:
            self.error = f"MQTT disconnected: {reason_code}"

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        value = _parse_value(message.payload.decode("utf-8", errors="replace"))
        with self._lock:
            self._values[message.topic] = (value, datetime.now(UTC))

    def start(self) -> None:
        try:
            self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
            self.client.loop_start()
        except Exception as exc:
            self.error = str(exc)

    def stop(self) -> None:
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            pass

    def car(self, car_id: int) -> tuple[dict[str, Any], datetime | None]:
        prefix = f"teslamate/cars/{car_id}/"
        values: dict[str, Any] = {}
        latest: datetime | None = None
        with self._lock:
            for topic, (value, received_at) in self._values.items():
                if topic.startswith(prefix):
                    values[topic[len(prefix):]] = value
                    latest = max(latest, received_at) if latest else received_at
        return values, latest


mqtt_cache = MQTTCache()


def _to_local(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ).isoformat()


def _parse_time(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TeslaMateMCPError(f"invalid ISO 8601 time: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(UTC)


def _time_range(from_time: str | None, to_time: str | None, default_days: int = 30) -> tuple[datetime, datetime]:
    end = _parse_time(to_time, datetime.now(UTC))
    start = _parse_time(from_time, end - timedelta(days=default_days))
    if start >= end:
        raise TeslaMateMCPError("from_time must be earlier than to_time")
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _to_local(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _vehicle_id(car_id: int | None) -> int:
    rows = _query("SELECT id FROM teslamate_mcp.vehicles ORDER BY display_priority, id")
    ids = [int(row["id"]) for row in rows]
    if car_id is None:
        if len(ids) == 1:
            return ids[0]
        raise TeslaMateMCPError("car_id is required when multiple vehicles exist")
    if car_id not in ids:
        raise TeslaMateMCPError(f"unknown car_id: {car_id}")
    return car_id


def _limit(value: int, maximum: int = 100) -> int:
    if not 1 <= value <= maximum:
        raise TeslaMateMCPError(f"limit must be between 1 and {maximum}")
    return value


def audited(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        record = {
            "event": "tool_call", "tool": fn.__name__,
            "car_id": kwargs.get("car_id"),
            "from_time": kwargs.get("from_time"), "to_time": kwargs.get("to_time"),
        }
        try:
            result = fn(*args, **kwargs)
            record["status"] = "ok"
            return result
        except Exception:
            record["status"] = "error"
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
            logger.info(json.dumps(record, default=str))
    return wrapper


mcp = FastMCP(
    "TeslaMate Read-Only",
    instructions=(
        "Query TeslaMate vehicle status, drives, charging sessions, efficiency and battery range. "
        "All tools are read-only. For routine questions, answer in the user's language with the "
        "result first and no more than 3 short sentences or 5 short bullets. Only include fields "
        "the user asked for. Do not add greetings, praise, unsolicited advice, methodology, or "
        "multiple freshness disclaimers. If data is stale, append one short sentence with its "
        "timestamp. For a single vehicle's location, state, battery, or odometer, call only "
        "get_vehicle_state unless the user explicitly asks for service diagnostics."
    ),
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=[],
    ),
)


@mcp.tool()
@audited
def get_health() -> dict[str, Any]:
    """Diagnose MCP/TeslaMate service health. Do not call for routine vehicle questions."""
    db_ok = True
    db_error = None
    latest: dict[str, Any] | None = None
    migration = None
    try:
        latest = _one("""
            SELECT GREATEST(
              COALESCE((SELECT max(date) FROM teslamate_mcp.positions), '-infinity'),
              COALESCE((SELECT max(start_date) FROM teslamate_mcp.states), '-infinity'),
              COALESCE((SELECT max(start_date) FROM teslamate_mcp.drives), '-infinity'),
              COALESCE((SELECT max(start_date) FROM teslamate_mcp.charging_sessions), '-infinity')
            ) AS last_event
        """)
        migration = _one("SELECT migration_version AS version FROM teslamate_mcp.schema_info")
    except Exception as exc:
        db_ok = False
        db_error = str(exc)
    web_ok = False
    web_error = None
    try:
        with urllib.request.urlopen(WEB_URL, timeout=3) as response:
            web_ok = 200 <= response.status < 500
    except Exception as exc:
        web_error = str(exc)
    last_event = latest.get("last_event") if latest else None
    age_seconds = None
    if isinstance(last_event, datetime):
        age_seconds = max(0, (datetime.now(UTC) - last_event.replace(tzinfo=UTC)).total_seconds())
    return {
        "database": {"ok": db_ok, "error": db_error},
        "mqtt": {"connected": mqtt_cache.connected, "error": mqtt_cache.error},
        "teslamate_web": {"ok": web_ok, "error": web_error},
        "schema_migration": migration.get("version") if migration else None,
        "last_database_event": _to_local(last_event) if isinstance(last_event, datetime) else None,
        "data_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "data_status": "stale" if age_seconds is None or age_seconds > 3600 else "fresh",
        "timezone": str(LOCAL_TZ),
    }


@mcp.tool()
@audited
def list_vehicles() -> dict[str, Any]:
    """List vehicles for fleet selection. For one vehicle's current data, use get_vehicle_state only."""
    rows = _query("""
        SELECT v.*, p.date AS observed_at, p.odometer, p.battery_level,
               p.rated_battery_range_km, p.ideal_battery_range_km,
               s.state, s.start_date AS state_since
        FROM teslamate_mcp.vehicles v
        LEFT JOIN teslamate_mcp.latest_positions p ON p.car_id = v.id
        LEFT JOIN LATERAL (
          SELECT state, start_date FROM teslamate_mcp.states
          WHERE car_id = v.id ORDER BY start_date DESC LIMIT 1
        ) s ON true
        ORDER BY v.display_priority, v.id
    """)
    return {"count": len(rows), "vehicles": _json_value(rows), "source": "postgresql"}


@mcp.tool()
@audited
def get_vehicle_state(car_id: int | None = None, detail: bool = False) -> dict[str, Any]:
    """Compact location/status/battery snapshot. Use alone for routine current-vehicle questions."""
    cid = _vehicle_id(car_id)
    vehicle = _one("SELECT * FROM teslamate_mcp.vehicles WHERE id = %s", (cid,))
    position = _one("SELECT * FROM teslamate_mcp.latest_positions WHERE car_id = %s", (cid,))
    state = _one(
        "SELECT state, start_date, end_date FROM teslamate_mcp.states WHERE car_id = %s ORDER BY start_date DESC LIMIT 1",
        (cid,),
    )
    values, received_at = mqtt_cache.car(cid)
    allowed = {
        "state", "since", "healthy", "version", "update_available", "battery_level",
        "usable_battery_level", "rated_battery_range_km", "ideal_battery_range_km",
        "est_battery_range_km", "odometer", "speed", "power", "outside_temp", "inside_temp",
        "is_climate_on", "is_preconditioning", "locked", "doors_open", "windows_open",
        "trunk_open", "frunk_open", "sentry_mode", "plugged_in", "charging_state",
        "charger_power", "charge_energy_added", "charge_limit_soc", "time_to_full_charge",
        "latitude", "longitude", "location", "elevation", "heading",
        "tpms_pressure_fl", "tpms_pressure_fr", "tpms_pressure_rl", "tpms_pressure_rr",
    }
    mqtt_state = {key: value for key, value in values.items() if key in allowed}
    position_date = position.get("date") if position else None
    state_date = state.get("start_date") if state else None
    observed_at = max(
        (item for item in (position_date, state_date) if isinstance(item, datetime)),
        default=None,
    )
    age_seconds = None
    if observed_at is not None:
        age_seconds = max(0, (datetime.now(UTC) - observed_at.replace(tzinfo=UTC)).total_seconds())

    def current(key: str) -> Any:
        if key in mqtt_state:
            return mqtt_state[key]
        return position.get(key) if position else None

    result: dict[str, Any] = {
        "vehicle": {
            "id": cid,
            "name": vehicle.get("name") if vehicle else None,
            "model": vehicle.get("model") if vehicle else None,
            "marketing_name": vehicle.get("marketing_name") if vehicle else None,
        },
        "state": mqtt_state.get("state") or (state.get("state") if state else None),
        "battery_percent": current("battery_level"),
        "range_km": current("rated_battery_range_km"),
        "odometer_km": current("odometer"),
        "locked": mqtt_state.get("locked"),
        "plugged_in": mqtt_state.get("plugged_in"),
        "charging_state": mqtt_state.get("charging_state"),
        "location": {
            "latitude": current("latitude"),
            "longitude": current("longitude"),
            "observed_at": _to_local(position_date),
        },
        "state_since": _to_local(state_date),
        "observed_at": _to_local(observed_at),
        "stale": age_seconds is None or age_seconds > 3600,
    }
    if detail:
        result["detail"] = {
            "mqtt": mqtt_state,
            "mqtt_received_at": _to_local(received_at),
            "database_position": _json_value(position),
            "database_state": _json_value(state),
        }
    return result


@mcp.tool()
@audited
def list_drives(
    car_id: int | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    limit: int = 20,
    before: str | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    """List drives compactly. For the latest trip use limit=2; respond in 1-2 natural sentences."""
    cid = _vehicle_id(car_id)
    start, end = _time_range(from_time, to_time)
    page_before = _parse_time(before, end.replace(tzinfo=UTC)).replace(tzinfo=None) if before else end
    size = _limit(limit)
    rows = _query("""
        SELECT *,
          CASE WHEN distance > 0 THEN
            (start_rated_range_km - end_rated_range_km) * efficiency * 1000 / distance
          END AS consumption_wh_per_km,
          (start_rated_range_km - end_rated_range_km) * efficiency AS estimated_energy_kwh
        FROM teslamate_mcp.drives
        WHERE car_id = %s AND start_date >= %s AND start_date < %s AND start_date < %s
        ORDER BY start_date DESC, id DESC LIMIT %s
    """, (cid, start, end, page_before, size))
    cursor = _to_local(rows[-1]["start_date"]) if len(rows) == size else None
    if detail:
        drives = _json_value(rows)
    else:
        drives = [
            {
                "id": row["id"],
                "start_at": _to_local(row["start_date"]),
                "end_at": _to_local(row["end_date"]),
                "from": row["start_address"],
                "to": row["end_address"],
                "distance_km": _json_value(row["distance"]),
                "duration_min": row["duration_min"],
                "max_speed_kmh": row["speed_max"],
                "battery_start": row["start_battery_level"],
                "battery_end": row["end_battery_level"],
            }
            for row in rows
        ]
    return {"car_id": cid, "drives": drives, "next_before": cursor, "count": len(rows)}


@mcp.tool()
@audited
def get_drive(drive_id: int) -> dict[str, Any]:
    """Get one drive. Mention only details the user asked for; avoid headings and metric inventories."""
    row = _one("""
        SELECT *,
          CASE WHEN distance > 0 THEN
            (start_rated_range_km - end_rated_range_km) * efficiency * 1000 / distance
          END AS consumption_wh_per_km,
          (start_rated_range_km - end_rated_range_km) * efficiency AS estimated_energy_kwh
        FROM teslamate_mcp.drives WHERE id = %s
    """, (drive_id,))
    if not row:
        raise TeslaMateMCPError(f"unknown drive_id: {drive_id}")
    return {"drive": _json_value(row), "source": "postgresql"}


@mcp.tool()
@audited
def get_drive_route(drive_id: int, max_points: int = 300) -> dict[str, Any]:
    """Return an exact-coordinate route downsampled to at most 500 points."""
    count = _one("SELECT count(*) AS count FROM teslamate_mcp.positions WHERE drive_id = %s", (drive_id,))
    total = int(count["count"] if count else 0)
    if total == 0:
        raise TeslaMateMCPError(f"unknown drive_id or no route points: {drive_id}")
    points = _limit(max_points, 500)
    step = max(1, math.ceil(total / points))
    rows = _query("""
        WITH numbered AS (
          SELECT date, latitude, longitude, elevation, speed, power, battery_level,
                 row_number() OVER (ORDER BY date) AS rn,
                 count(*) OVER () AS total
          FROM teslamate_mcp.positions WHERE drive_id = %s
        )
        SELECT date, latitude, longitude, elevation, speed, power, battery_level
        FROM numbered
        WHERE rn = 1 OR rn = total OR mod(rn - 1, %s) = 0
        ORDER BY date LIMIT %s
    """, (drive_id, step, points))
    return {"drive_id": drive_id, "original_points": total, "returned_points": len(rows), "points": _json_value(rows)}


@mcp.tool()
@audited
def export_drive_data(drive_id: int, max_points: int = 20000) -> dict[str, Any]:
    """Export one drive as versioned JSON with its summary and complete ordered telemetry points."""
    row = _one("""
        SELECT d.*,
          CASE WHEN d.distance > 0 THEN
            (d.start_rated_range_km - d.end_rated_range_km) * d.efficiency * 1000 / d.distance
          END AS consumption_wh_per_km,
          (d.start_rated_range_km - d.end_rated_range_km) * d.efficiency AS estimated_energy_kwh,
          v.name AS vehicle_name, v.model AS vehicle_model,
          v.marketing_name AS vehicle_marketing_name
        FROM teslamate_mcp.drives d
        JOIN teslamate_mcp.vehicles v ON v.id = d.car_id
        WHERE d.id = %s
    """, (drive_id,))
    if not row:
        raise TeslaMateMCPError(f"unknown drive_id: {drive_id}")

    size = _limit(max_points, 20000)
    count = _one("SELECT count(*) AS count FROM teslamate_mcp.positions WHERE drive_id = %s", (drive_id,))
    total = int(count["count"] if count else 0)
    if total > size:
        raise TeslaMateMCPError(
            f"drive {drive_id} has {total} telemetry points; max_points={size}. "
            f"Retry with max_points={total} to export the complete drive"
        )

    points = _query("""
        SELECT date, latitude, longitude, elevation, speed, power, odometer,
               battery_level, usable_battery_level, ideal_battery_range_km,
               rated_battery_range_km, est_battery_range_km,
               outside_temp, inside_temp, is_climate_on,
               tpms_pressure_fl, tpms_pressure_fr,
               tpms_pressure_rl, tpms_pressure_rr
        FROM teslamate_mcp.positions
        WHERE drive_id = %s
        ORDER BY date, id
        LIMIT %s
    """, (drive_id, size))

    vehicle = {
        "id": row.pop("car_id"),
        "name": row.pop("vehicle_name"),
        "model": row.pop("vehicle_model"),
        "marketing_name": row.pop("vehicle_marketing_name"),
    }
    return {
        "schema": "teslamate.drive-export",
        "schema_version": "1.0",
        "timezone": str(LOCAL_TZ),
        "source": "postgresql",
        "drive_id": drive_id,
        "vehicle": _json_value(vehicle),
        "drive": _json_value(row),
        "telemetry": {
            "point_count": total,
            "complete": len(points) == total,
            "points": _json_value(points),
        },
    }


@mcp.tool()
@audited
def driving_summary(
    car_id: int | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
) -> dict[str, Any]:
    """Summarize driving. Lead with the main result and keep routine answers to 1-2 sentences."""
    cid = _vehicle_id(car_id)
    start, end = _time_range(from_time, to_time)
    row = _one("""
        SELECT count(*) AS drive_count, sum(distance) AS distance_km,
               sum(duration_min) AS duration_min, avg(distance) AS average_drive_km,
               max(speed_max) AS maximum_speed_kmh,
               sum((start_rated_range_km - end_rated_range_km) * efficiency) AS estimated_net_energy_kwh,
               CASE WHEN sum(distance) > 0 THEN
                 sum((start_rated_range_km - end_rated_range_km) * efficiency) * 1000 / sum(distance)
               END AS estimated_consumption_wh_per_km
        FROM teslamate_mcp.drives
        WHERE car_id = %s AND start_date >= %s AND start_date < %s AND end_date IS NOT NULL
    """, (cid, start, end))
    return {
        "car_id": cid, "from": _to_local(start), "to": _to_local(end),
        "summary": _json_value(row),
        "method": "TeslaMate rated-range delta multiplied by vehicle efficiency; this is an estimate.",
    }


@mcp.tool()
@audited
def list_charging_sessions(
    car_id: int | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    limit: int = 20,
    before: str | None = None,
) -> dict[str, Any]:
    """List charging sessions. Answer naturally and include only the requested charging facts."""
    cid = _vehicle_id(car_id)
    start, end = _time_range(from_time, to_time)
    page_before = _parse_time(before, end.replace(tzinfo=UTC)).replace(tzinfo=None) if before else end
    size = _limit(limit)
    rows = _query("""
        SELECT * FROM teslamate_mcp.charging_sessions
        WHERE car_id = %s AND start_date >= %s AND start_date < %s AND start_date < %s
        ORDER BY start_date DESC, id DESC LIMIT %s
    """, (cid, start, end, page_before, size))
    cursor = _to_local(rows[-1]["start_date"]) if len(rows) == size else None
    return {"car_id": cid, "sessions": _json_value(rows), "next_before": cursor, "count": len(rows)}


@mcp.tool()
@audited
def get_charging_session(session_id: int, max_points: int = 200) -> dict[str, Any]:
    """Get one charging session and a downsampled charging curve."""
    session = _one("SELECT * FROM teslamate_mcp.charging_sessions WHERE id = %s", (session_id,))
    if not session:
        raise TeslaMateMCPError(f"unknown session_id: {session_id}")
    size = _limit(max_points, 500)
    count = _one("SELECT count(*) AS count FROM teslamate_mcp.charge_samples WHERE charging_process_id = %s", (session_id,))
    total = int(count["count"] if count else 0)
    step = max(1, math.ceil(total / size))
    samples = _query("""
        WITH numbered AS (
          SELECT *, row_number() OVER (ORDER BY date) AS rn, count(*) OVER () AS total
          FROM teslamate_mcp.charge_samples WHERE charging_process_id = %s
        )
        SELECT date, battery_level, usable_battery_level, charge_energy_added,
               charger_actual_current, charger_phases, charger_power, charger_voltage,
               fast_charger_present, outside_temp
        FROM numbered
        WHERE rn = 1 OR rn = total OR mod(rn - 1, %s) = 0
        ORDER BY date LIMIT %s
    """, (session_id, step, size))
    return {"session": _json_value(session), "original_points": total, "samples": _json_value(samples)}


@mcp.tool()
@audited
def charging_summary(
    car_id: int | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    group_by: str = "month",
) -> dict[str, Any]:
    """Aggregate charging. Give the requested total or comparison without narrating every field."""
    cid = _vehicle_id(car_id)
    start, end = _time_range(from_time, to_time)
    if group_by not in {"day", "month", "location"}:
        raise TeslaMateMCPError("group_by must be day, month, or location")
    if group_by == "location":
        group_expr = "COALESCE(address, 'Unknown')"
    else:
        group_expr = f"date_trunc('{group_by}', timezone('UTC', start_date), 'Asia/Shanghai')"
    rows = _query(f"""
        SELECT {group_expr} AS bucket, count(*) AS sessions,
               sum(charge_energy_added) AS energy_added_kwh,
               sum(charge_energy_used) AS energy_used_kwh,
               sum(cost) AS cost,
               sum(duration_min) AS duration_min,
               count(*) FILTER (WHERE is_dc) AS dc_sessions,
               count(*) FILTER (WHERE NOT COALESCE(is_dc, false)) AS ac_sessions,
               CASE WHEN sum(charge_energy_added) > 0 THEN sum(cost) / sum(charge_energy_added) END AS cost_per_kwh
        FROM teslamate_mcp.charging_sessions
        WHERE car_id = %s AND start_date >= %s AND start_date < %s
        GROUP BY 1 ORDER BY 1
    """, (cid, start, end))
    return {"car_id": cid, "group_by": group_by, "groups": _json_value(rows)}


@mcp.tool()
@audited
def battery_range_trend(
    car_id: int | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    group_by: str = "month",
) -> dict[str, Any]:
    """Return weekly or monthly projected full-charge rated and ideal range estimates."""
    cid = _vehicle_id(car_id)
    start, end = _time_range(from_time, to_time, default_days=365)
    if group_by not in {"week", "month"}:
        raise TeslaMateMCPError("group_by must be week or month")
    rows = _query(f"""
        SELECT date_trunc('{group_by}', timezone('UTC', date), 'Asia/Shanghai') AS bucket,
               avg(rated_battery_range_km / NULLIF(COALESCE(usable_battery_level, battery_level), 0) * 100) AS projected_rated_range_km,
               avg(ideal_battery_range_km / NULLIF(COALESCE(usable_battery_level, battery_level), 0) * 100) AS projected_ideal_range_km,
               avg(odometer) AS odometer_km,
               count(*) AS samples
        FROM teslamate_mcp.positions
        WHERE car_id = %s AND date >= %s AND date < %s
          AND battery_level BETWEEN 5 AND 100
          AND ideal_battery_range_km IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """, (cid, start, end))
    return {
        "car_id": cid, "group_by": group_by, "points": _json_value(rows),
        "warning": "Projected range is an estimate from TeslaMate telemetry, not a measured battery capacity test.",
    }


@mcp.tool()
@audited
def lifetime_stats(car_id: int | None = None) -> dict[str, Any]:
    """Return lifetime statistics. Keep the answer compact unless the user asks for a full breakdown."""
    cid = _vehicle_id(car_id)
    row = _one("""
        SELECT
          (SELECT count(*) FROM teslamate_mcp.drives WHERE car_id = %s) AS drive_count,
          (SELECT sum(distance) FROM teslamate_mcp.drives WHERE car_id = %s) AS distance_km,
          (SELECT sum(duration_min) FROM teslamate_mcp.drives WHERE car_id = %s) AS drive_minutes,
          (SELECT count(*) FROM teslamate_mcp.charging_sessions WHERE car_id = %s) AS charging_sessions,
          (SELECT sum(charge_energy_added) FROM teslamate_mcp.charging_sessions WHERE car_id = %s) AS energy_added_kwh,
          (SELECT sum(cost) FROM teslamate_mcp.charging_sessions WHERE car_id = %s) AS charging_cost,
          (SELECT odometer FROM teslamate_mcp.latest_positions WHERE car_id = %s) AS latest_odometer_km,
          (SELECT count(*) FROM teslamate_mcp.updates WHERE car_id = %s) AS software_updates
    """, (cid, cid, cid, cid, cid, cid, cid, cid))
    return {"car_id": cid, "stats": _json_value(row), "source": "postgresql"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: Any):
        if request.url.path.rstrip("/") == "/healthz":
            return JSONResponse({"ok": True})
        if not self.token or request.headers.get("authorization", "") != f"Bearer {self.token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only TeslaMate MCP server")
    parser.add_argument("--host", default=os.environ.get("TESLAMATE_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TESLAMATE_MCP_PORT", "8766")))
    args = parser.parse_args()
    if not MCP_TOKEN:
        raise SystemExit("TESLAMATE_MCP_TOKEN or TESLAMATE_MCP_TOKEN_FILE is required")
    mqtt_cache.start()
    import uvicorn
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=MCP_TOKEN)
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        mqtt_cache.stop()


if __name__ == "__main__":
    main()
