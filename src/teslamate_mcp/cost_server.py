from __future__ import annotations

import argparse
import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo

import psycopg
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.rows import dict_row
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from teslamate_mcp.privacy import LocationPrivacy

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("teslamate_cost_mcp")

LOCAL_TZ = ZoneInfo(os.environ.get("TESLAMATE_TIMEZONE", "Asia/Shanghai"))
UTC = timezone.utc
DB_HOST = os.environ.get("TESLAMATE_DB_HOST", "database")
DB_PORT = int(os.environ.get("TESLAMATE_DB_PORT", "5432"))
DB_NAME = os.environ.get("TESLAMATE_DB_NAME", "teslamate")
DB_USER = os.environ.get("TESLAMATE_DB_USER", "teslamate_cost_mcp")
CURRENT_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "teslamate_cost_actor", default=""
)


class TeslaMateCostError(RuntimeError):
    pass


class ChargingCostChange(TypedDict):
    session_id: int
    operation: NotRequired[Literal["set", "clear"]]
    amount_cny: NotRequired[str | None]
    source_kind: NotRequired[Literal["text", "screenshot"]]
    source_summary: NotRequired[str]


class TollExpenseChange(TypedDict):
    operation: NotRequired[Literal["upsert", "void"]]
    expense_id: NotRequired[str]
    expected_revision: NotRequired[int]
    amount_cny: NotRequired[str]
    occurred_at: NotRequired[str]
    entry_name: NotRequired[str]
    exit_name: NotRequired[str]
    provider: NotRequired[Literal["manual", "etc", "wechat", "alipay", "other"]]
    external_ref: NotRequired[str]
    drive_ids: NotRequired[list[int]]
    source_kind: NotRequired[Literal["text", "screenshot"]]
    source_summary: NotRequired[str]


def _read_secret(env_name: str, file_env_name: str) -> str:
    value = os.environ.get(env_name, "")
    path = os.environ.get(file_env_name, "")
    if not value and path:
        value = Path(path).read_text().strip()
    return value


DB_PASSWORD = _read_secret(
    "TESLAMATE_COST_DB_PASSWORD", "TESLAMATE_COST_DB_PASSWORD_FILE"
)
SIGNING_SECRET = _read_secret(
    "TESLAMATE_COST_SIGNING_SECRET", "TESLAMATE_COST_SIGNING_SECRET_FILE"
).encode()
TOKEN_ACTORS = {
    token: actor
    for actor, token in {
        "huunter": _read_secret(
            "TESLAMATE_COST_TOKEN_HUUNTER", "TESLAMATE_COST_TOKEN_HUUNTER_FILE"
        ),
        "guoguo": _read_secret(
            "TESLAMATE_COST_TOKEN_GUOGUO", "TESLAMATE_COST_TOKEN_GUOGUO_FILE"
        ),
    }.items()
    if token
}
ALLOWED_HOSTS = [
    item.strip()
    for item in os.environ.get(
        "TESLAMATE_COST_ALLOWED_HOSTS",
        "127.0.0.1:*,localhost:*",
    ).split(",")
    if item.strip()
]
LOCATION_PRIVACY = LocationPrivacy.from_env()
PENDING_PROPOSAL_LIMIT = 1024
_PENDING_PROPOSALS: dict[str, dict[str, Any]] = {}
_PENDING_PROPOSALS_LOCK = threading.Lock()


def _connect() -> psycopg.Connection:
    if not DB_PASSWORD:
        raise TeslaMateCostError("database password is not configured")
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        row_factory=dict_row,
        options="-c statement_timeout=5000",
        connect_timeout=5,
    )


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    started = time.monotonic()
    with _connect() as con, con.cursor() as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    logger.info(
        json.dumps(
            {
                "event": "db_query",
                "rows": len(rows),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
    )
    return rows


def _one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


def _actor() -> str:
    actor = CURRENT_ACTOR.get()
    if actor not in {"huunter", "guoguo"}:
        raise TeslaMateCostError("authorized actor context is missing")
    return actor


def _to_local(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ).isoformat()


def _parse_time(value: str) -> datetime:
    if not value or not value.strip():
        raise TeslaMateCostError("from_time and to_time are required")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TeslaMateCostError(f"invalid ISO 8601 time: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _to_local(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _parse_amount(value: str) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise TeslaMateCostError("amount_cny must be a decimal number") from exc
    if not amount.is_finite():
        raise TeslaMateCostError("amount_cny must be finite")
    normalized = amount.quantize(Decimal("0.01"))
    if amount != normalized:
        raise TeslaMateCostError("amount_cny must have at most two decimal places")
    if normalized < 0 or normalized > Decimal("9999.99"):
        raise TeslaMateCostError("amount_cny must be between 0.00 and 9999.99")
    return normalized


def _parse_energy_kwh(value: str) -> Decimal:
    raw = str(value).strip()
    if not raw or len(raw) > 32:
        raise TeslaMateCostError("energy_kwh must be a decimal number")
    try:
        energy = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise TeslaMateCostError("energy_kwh must be a decimal number") from exc
    if not energy.is_finite():
        raise TeslaMateCostError("energy_kwh must be finite")
    if energy < 0 or energy > Decimal("1000"):
        raise TeslaMateCostError("energy_kwh must be between 0 and 1000")
    if energy.as_tuple().exponent < -6:
        raise TeslaMateCostError("energy_kwh must have at most six decimal places")
    return energy


def _validation_error_response(exc: TeslaMateCostError) -> dict[str, Any]:
    """Return argument validation without marking the MCP transport failed."""
    return {
        "ok": False,
        "validation_error": str(exc),
        "retryable": True,
        "write_performed": False,
    }


def _sanitize_summary(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", cleaned).strip()[:500]


def _normalize_location(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").lower())


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise TeslaMateCostError("invalid proposal token") from exc


def _sign_payload(payload: dict[str, Any]) -> str:
    if not SIGNING_SECRET:
        raise TeslaMateCostError("proposal signing secret is not configured")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(SIGNING_SECRET, body, hashlib.sha256).digest()
    return f"{_b64encode(body)}.{_b64encode(signature)}"


def _verify_token(token: str, actor: str, *, now: int | None = None) -> dict[str, Any]:
    try:
        encoded_body, encoded_signature = token.split(".", 1)
    except ValueError as exc:
        raise TeslaMateCostError("invalid proposal token") from exc
    body = _b64decode(encoded_body)
    signature = _b64decode(encoded_signature)
    expected = hmac.new(SIGNING_SECRET, body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise TeslaMateCostError("invalid proposal token signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise TeslaMateCostError("invalid proposal token payload") from exc
    if payload.get("actor") != actor:
        raise TeslaMateCostError("proposal belongs to a different actor")
    current_time = int(time.time()) if now is None else now
    if int(payload.get("expires_at", 0)) < current_time:
        raise TeslaMateCostError("proposal has expired; prepare it again")
    return payload


def _store_pending_proposal(payload: dict[str, Any], token: str) -> str:
    proposal_id = str(uuid.UUID(str(payload["request_id"])))
    entry = {
        "actor": str(payload["actor"]),
        "expires_at": int(payload["expires_at"]),
        "issued_at": int(payload["issued_at"]),
        "token": token,
    }
    now = int(time.time())
    with _PENDING_PROPOSALS_LOCK:
        expired = [
            key
            for key, value in _PENDING_PROPOSALS.items()
            if int(value["expires_at"]) < now
        ]
        for key in expired:
            _PENDING_PROPOSALS.pop(key, None)
        if len(_PENDING_PROPOSALS) >= PENDING_PROPOSAL_LIMIT:
            oldest = min(
                _PENDING_PROPOSALS,
                key=lambda key: int(_PENDING_PROPOSALS[key]["issued_at"]),
            )
            _PENDING_PROPOSALS.pop(oldest, None)
        _PENDING_PROPOSALS[proposal_id] = entry
    return proposal_id


def _pending_proposal_token(
    proposal_id: str,
    actor: str,
    *,
    now: int | None = None,
) -> str:
    try:
        normalized_id = str(uuid.UUID(str(proposal_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TeslaMateCostError("invalid proposal id") from exc
    current_time = int(time.time()) if now is None else now
    with _PENDING_PROPOSALS_LOCK:
        entry = _PENDING_PROPOSALS.get(normalized_id)
        if not entry:
            raise TeslaMateCostError(
                "proposal is unavailable; prepare it again (the service may have restarted)"
            )
        if entry["actor"] != actor:
            raise TeslaMateCostError("proposal belongs to a different actor")
        if int(entry["expires_at"]) < current_time:
            _PENDING_PROPOSALS.pop(normalized_id, None)
            raise TeslaMateCostError("proposal has expired; prepare it again")
        return str(entry["token"])


def _find_charging_sessions(
    *,
    from_time: str,
    to_time: str,
    location_hint: str | None,
    energy_kwh: str | None,
    unpriced_only: bool,
    limit: int,
) -> dict[str, Any]:
    start = _parse_time(from_time)
    end = _parse_time(to_time)
    if start >= end:
        raise TeslaMateCostError("from_time must be earlier than to_time")
    limit = max(1, min(limit, 3))
    requested_energy = (
        _parse_energy_kwh(energy_kwh) if energy_kwh is not None else None
    )
    rows = _query(
        """
        SELECT * FROM teslamate_cost_mcp.charging_sessions
        WHERE start_date >= %s AND start_date < %s
          AND (%s = false OR cost IS NULL)
        ORDER BY start_date DESC, id DESC
        LIMIT 20
        """,
        (start, end, unpriced_only),
    )
    normalized_hint = _normalize_location(location_hint or "")
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        address = _normalize_location(str(row.get("address") or ""))
        location_score = 0.0
        if normalized_hint:
            location_score = SequenceMatcher(None, normalized_hint, address).ratio()
            if normalized_hint in address or address in normalized_hint:
                location_score = 1.0
        energy_delta: Decimal | None = None
        if requested_energy is not None:
            observed_values = [
                value
                for value in (row.get("charge_energy_used"), row.get("charge_energy_added"))
                if value is not None
            ]
            if observed_values:
                energy_delta = min(abs(Decimal(value) - requested_energy) for value in observed_values)
        energy_score = 0.0 if energy_delta is None else 1.0 / (1.0 + float(energy_delta))
        rank = (location_score * 2.0) + energy_score
        candidate = dict(row)
        candidate["match"] = {
            "location_similarity": round(location_score, 3) if normalized_hint else None,
            "energy_delta_kwh": format(energy_delta, "f") if energy_delta is not None else None,
        }
        ranked.append((rank, candidate))
    ranked.sort(key=lambda item: (item[0], item[1]["start_date"], item[1]["id"]), reverse=True)
    return {
        "actor": _actor(),
        "timezone": str(LOCAL_TZ),
        "from": _to_local(start),
        "to": _to_local(end),
        "candidates": _json_value([candidate for _, candidate in ranked[:limit]]),
        "count": min(len(ranked), limit),
        "requires_user_selection": len(ranked) != 1,
        "write_performed": False,
    }


def _get_session(session_id: int) -> dict[str, Any]:
    row = _one(
        "SELECT * FROM teslamate_cost_mcp.charging_sessions WHERE id = %s",
        (session_id,),
    )
    if not row:
        raise TeslaMateCostError(f"unknown charging session: {session_id}")
    if row.get("end_date") is None:
        raise TeslaMateCostError("charging session is not complete")
    return row


def _prepare_change(
    *,
    actor: str,
    session_id: int,
    operation: Literal["set", "clear"],
    amount_cny: str | None,
    source_kind: Literal["text", "screenshot"],
    source_summary: str,
) -> dict[str, Any]:
    if operation not in {"set", "clear"}:
        raise TeslaMateCostError("operation must be set or clear")
    if source_kind not in {"text", "screenshot"}:
        raise TeslaMateCostError("source_kind must be text or screenshot")
    if operation == "set":
        if amount_cny is None:
            raise TeslaMateCostError("amount_cny is required for set")
        new_cost: Decimal | None = _parse_amount(amount_cny)
    else:
        if amount_cny not in {None, ""}:
            raise TeslaMateCostError("amount_cny must be omitted for clear")
        new_cost = None
    session = _get_session(session_id)
    now = int(time.time())
    payload = {
        "version": 1,
        "request_id": str(uuid.uuid4()),
        "actor": actor,
        "session_id": session_id,
        "expected_cost": (
            format(Decimal(session["cost"]), ".2f") if session.get("cost") is not None else None
        ),
        "new_cost": format(new_cost, ".2f") if new_cost is not None else None,
        "source_kind": source_kind,
        "source_summary": _sanitize_summary(source_summary),
        "issued_at": now,
        "expires_at": now + 24 * 60 * 60,
    }
    token = _sign_payload(payload)
    proposal_id = _store_pending_proposal(payload, token)
    energy = session.get("charge_energy_used") or session.get("charge_energy_added")
    return {
        "proposal": {
            "request_id": payload["request_id"],
            "actor": actor,
            "session_id": session_id,
            "start_date": _to_local(session.get("start_date")),
            "end_date": _to_local(session.get("end_date")),
            "address": session.get("address"),
            "energy_kwh": format(Decimal(energy), "f") if energy is not None else None,
            "previous_cost_cny": payload["expected_cost"],
            "new_cost_cny": payload["new_cost"],
            "expires_at": datetime.fromtimestamp(payload["expires_at"], UTC).astimezone(LOCAL_TZ).isoformat(),
        },
        "proposal_id": proposal_id,
        "requires_host_approval_card": True,
        "instruction": (
            "Show the exact proposal. For one proposal call commit_charging_cost_change; when the user supplied "
            "multiple costs, prepare all of them first and then call commit_charging_cost_changes once with every "
            "proposal_id. The Hermes host will display one interactive approval card and block the write until approved; "
            "do not ask the user to type a separate confirmation."
        ),
        "write_performed": False,
    }


def _proposal_payloads(*, actor: str, proposal_ids: list[str]) -> list[dict[str, Any]]:
    if not 1 <= len(proposal_ids) <= 20:
        raise TeslaMateCostError("proposal_ids must contain between 1 and 20 items")
    normalized_ids: list[str] = []
    for proposal_id in proposal_ids:
        try:
            normalized_ids.append(str(uuid.UUID(str(proposal_id))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise TeslaMateCostError("invalid proposal id") from exc
    if len(set(normalized_ids)) != len(normalized_ids):
        raise TeslaMateCostError("proposal_ids must not contain duplicates")
    return [
        _verify_token(_pending_proposal_token(proposal_id, actor), actor)
        for proposal_id in normalized_ids
    ]


def _commit_changes(*, actor: str, proposal_ids: list[str]) -> dict[str, Any]:
    payloads = _proposal_payloads(actor=actor, proposal_ids=proposal_ids)
    saved: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        with _connect() as con, con.cursor() as cur:
            for payload in payloads:
                expected_cost = (
                    Decimal(payload["expected_cost"])
                    if payload["expected_cost"] is not None
                    else None
                )
                new_cost = (
                    Decimal(payload["new_cost"])
                    if payload["new_cost"] is not None
                    else None
                )
                cur.execute(
                    """
                    SELECT * FROM teslamate_cost_mcp.apply_cost_change(
                        %s::uuid, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        payload["request_id"],
                        actor,
                        int(payload["session_id"]),
                        expected_cost,
                        new_cost,
                        payload["source_kind"],
                        payload["source_summary"],
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise TeslaMateCostError("cost update returned no result")
                saved.append(row)
    except psycopg.Error as exc:
        message = getattr(exc.diag, "message_primary", None) or str(exc)
        raise TeslaMateCostError(message) from exc
    logger.info(
        json.dumps(
            {
                "event": "db_batch_write",
                "rows": len(saved),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
    )
    return {
        "saved": _json_value(saved),
        "count": len(saved),
        "actor": actor,
        "currency": "CNY",
        "write_performed": True,
    }


def _commit_change(*, actor: str, proposal_id: str) -> dict[str, Any]:
    result = _commit_changes(actor=actor, proposal_ids=[proposal_id])
    return {**result, "saved": result["saved"][0]}


def _request_changes(
    *, actor: str, changes: list[ChargingCostChange]
) -> dict[str, Any]:
    if not 1 <= len(changes) <= 20:
        raise TeslaMateCostError("changes must contain between 1 and 20 items")
    proposals: list[dict[str, Any]] = []
    proposal_ids: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            raise TeslaMateCostError("each change must be an object")
        try:
            session_id = int(change["session_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TeslaMateCostError("each change requires an integer session_id") from exc
        prepared = _prepare_change(
            actor=actor,
            session_id=session_id,
            operation=change.get("operation", "set"),
            amount_cny=change.get("amount_cny"),
            source_kind=change.get("source_kind", "text"),
            source_summary=change.get("source_summary", ""),
        )
        proposals.append(prepared["proposal"])
        proposal_ids.append(prepared["proposal_id"])
    committed = _commit_changes(actor=actor, proposal_ids=proposal_ids)
    return {**committed, "proposals": proposals}


def _parse_uuid(value: Any, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TeslaMateCostError(f"invalid {field_name}") from exc


def _parse_toll_time(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TeslaMateCostError("occurred_at is required")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TeslaMateCostError(f"invalid ISO 8601 occurred_at: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _sanitize_toll_text(value: Any, *, limit: int = 120) -> str:
    cleaned = _sanitize_summary(str(value or ""))
    return cleaned[:limit]


def _normalize_drive_ids(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise TeslaMateCostError("drive_ids must contain between 0 and 20 items")
    normalized: list[int] = []
    for item in value:
        if isinstance(item, bool):
            raise TeslaMateCostError("drive_ids must contain integers")
        try:
            drive_id = int(item)
        except (TypeError, ValueError) as exc:
            raise TeslaMateCostError("drive_ids must contain integers") from exc
        if drive_id <= 0:
            raise TeslaMateCostError("drive_ids must contain positive integers")
        normalized.append(drive_id)
    if len(set(normalized)) != len(normalized):
        raise TeslaMateCostError("drive_ids must not contain duplicates")
    return normalized


def _haversine_km(
    lat1: Any, lon1: Any, lat2: Any, lon2: Any
) -> float | None:
    if None in {lat1, lon1, lat2, lon2}:
        return None
    try:
        first_lat, first_lon, second_lat, second_lon = map(
            math.radians, (float(lat1), float(lon1), float(lat2), float(lon2))
        )
    except (TypeError, ValueError):
        return None
    delta_lat = second_lat - first_lat
    delta_lon = second_lon - first_lon
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(first_lat)
        * math.cos(second_lat)
        * math.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _drives_are_continuous(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    gap = (current["start_date"] - previous["end_date"]).total_seconds()
    if gap < 0 or gap > 120 * 60:
        return False
    spatial_gap = _haversine_km(
        previous.get("end_latitude"),
        previous.get("end_longitude"),
        current.get("start_latitude"),
        current.get("start_longitude"),
    )
    if spatial_gap is not None:
        return spatial_gap <= 10.0
    previous_place = _normalize_location(previous.get("end_address") or "")
    current_place = _normalize_location(current.get("start_address") or "")
    return gap <= 15 * 60 or bool(
        previous_place
        and current_place
        and (previous_place in current_place or current_place in previous_place)
    )


def _journey_score(
    drives: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    entry_hint: str,
    exit_hint: str,
) -> tuple[float, float | None, float | None, float]:
    entry = _normalize_location(entry_hint)
    exit_name = _normalize_location(exit_hint)
    start_place = _normalize_location(drives[0].get("start_address") or "")
    end_place = _normalize_location(drives[-1].get("end_address") or "")
    has_cjk = lambda value: bool(re.search(r"[\u4e00-\u9fff]", value))
    entry_comparable = bool(entry and start_place) and has_cjk(entry) == has_cjk(start_place)
    exit_comparable = bool(exit_name and end_place) and has_cjk(exit_name) == has_cjk(end_place)
    entry_score = (
        SequenceMatcher(None, entry, start_place).ratio()
        if entry_comparable
        else None
    )
    exit_score = (
        SequenceMatcher(None, exit_name, end_place).ratio()
        if exit_comparable
        else None
    )
    if entry and start_place and (entry in start_place or start_place in entry):
        entry_score = 1.0
    if exit_name and end_place and (
        exit_name in end_place or end_place in exit_name
    ):
        exit_score = 1.0
    scale = max((end - start).total_seconds(), 3600.0)
    edge_delta = abs((drives[0]["start_date"] - start).total_seconds()) + abs(
        (end - drives[-1]["end_date"]).total_seconds()
    )
    time_score = max(0.0, 1.0 - edge_delta / (2 * scale))
    components: list[tuple[float, float]] = [(time_score, 0.2)]
    if entry_score is not None:
        components.append((entry_score, 0.4))
    if exit_score is not None:
        components.append((exit_score, 0.4))
    total_weight = sum(weight for _, weight in components)
    score = sum(value * weight for value, weight in components) / total_weight
    return round(score, 3), entry_score, exit_score, round(time_score, 3)


def _find_toll_journey_candidates(
    *,
    from_time: str,
    to_time: str,
    entry_hint: str | None,
    exit_hint: str | None,
    amount_cny: str | None,
    provider: str | None,
    external_ref: str | None,
    limit: int,
) -> dict[str, Any]:
    start = _parse_time(from_time)
    end = _parse_time(to_time)
    if start >= end:
        raise TeslaMateCostError("from_time must be earlier than to_time")
    # Tool callers are LLM-driven and may occasionally request more choices than
    # the compact approval flow supports. Keep the read path resilient instead
    # of failing the whole expense capture.
    limit = max(1, min(limit, 3))
    rows = _query(
        """
        SELECT * FROM teslamate_cost_mcp.toll_candidate_drives
        WHERE start_date >= %s AND start_date < %s
        ORDER BY start_date, id
        LIMIT 50
        """,
        (start - timedelta(hours=6), end + timedelta(hours=6)),
    )
    sequences: list[dict[str, Any]] = []
    entry = entry_hint or ""
    exit_name = exit_hint or ""
    for first in range(len(rows)):
        current: list[dict[str, Any]] = []
        distance = Decimal("0")
        for index in range(first, min(len(rows), first + 20)):
            row = rows[index]
            if row.get("end_date") is None:
                break
            if current and not _drives_are_continuous(current[-1], row):
                break
            current.append(row)
            distance += Decimal(str(row.get("distance") or 0))
            if distance < Decimal("5"):
                continue
            score, entry_score, exit_score, time_score = _journey_score(
                current,
                start=start,
                end=end,
                entry_hint=entry,
                exit_hint=exit_name,
            )
            sequences.append(
                {
                    "drive_ids": [int(item["id"]) for item in current],
                    "start_at": _to_local(current[0]["start_date"]),
                    "end_at": _to_local(current[-1]["end_date"]),
                    "from": current[0].get("start_address"),
                    "to": current[-1].get("end_address"),
                    "distance_km": format(distance.quantize(Decimal("0.1")), "f"),
                    "drive_count": len(current),
                    "score": score,
                    "match": {
                        "entry_similarity": (
                            round(entry_score, 3) if entry_score is not None else None
                        ),
                        "exit_similarity": (
                            round(exit_score, 3) if exit_score is not None else None
                        ),
                        "time_score": time_score,
                    },
                }
            )
    sequences.sort(
        key=lambda item: (
            item["score"],
            Decimal(item["distance_km"]),
            item["drive_count"],
        ),
        reverse=True,
    )
    candidates: list[dict[str, Any]] = []
    for sequence in sequences:
        sequence_ids = set(sequence["drive_ids"])
        sequence_distance = Decimal(sequence["distance_km"])
        near_equivalent = False
        for selected in candidates:
            selected_ids = set(selected["drive_ids"])
            nested = sequence_ids <= selected_ids or selected_ids <= sequence_ids
            distance_delta = abs(
                sequence_distance - Decimal(selected["distance_km"])
            )
            if nested and distance_delta < Decimal("5.0"):
                near_equivalent = True
                break
        if not near_equivalent:
            candidates.append(sequence)
        if len(candidates) >= limit:
            break
    top_score = candidates[0]["score"] if candidates else 0.0
    next_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
    high_confidence = bool(
        candidates and top_score >= 0.80 and top_score - next_score >= 0.15
    )
    duplicate_candidates: list[dict[str, Any]] = []
    if amount_cny is not None:
        amount = _parse_amount(amount_cny)
        normalized_provider = str(provider or "").strip().lower() or None
        if normalized_provider not in {None, "manual", "etc", "wechat", "alipay", "other"}:
            raise TeslaMateCostError("invalid toll provider")
        normalized_ref = _sanitize_toll_text(external_ref)
        duplicate_candidates = _query(
            """
            SELECT expense_id, amount, occurred_at, entry_name, exit_name,
                   provider, external_ref, status, journey_id
            FROM teslamate_cost_mcp.toll_expense_current
            WHERE status <> 'void' AND amount = %s
              AND occurred_at >= %s AND occurred_at <= %s
              AND (CAST(%s AS text) IS NULL OR provider = %s)
              AND (CAST(%s AS text) = '' OR external_ref = %s)
            ORDER BY occurred_at DESC
            LIMIT 5
            """,
            (
                amount,
                start - timedelta(days=1),
                end + timedelta(days=1),
                normalized_provider,
                normalized_provider,
                normalized_ref,
                normalized_ref,
            ),
        )
    return {
        "actor": _actor(),
        "timezone": str(LOCAL_TZ),
        "candidates": candidates,
        "count": len(candidates),
        "high_confidence": high_confidence,
        "requires_user_selection": bool(candidates) and not high_confidence,
        "unmatched_allowed": True,
        "duplicate_warning": bool(duplicate_candidates),
        "duplicate_candidates": _json_value(duplicate_candidates),
        "write_performed": False,
    }


def _toll_fingerprint(
    *,
    amount: Decimal,
    occurred_at: datetime,
    entry_name: str,
    exit_name: str,
    provider: str,
    external_ref: str,
) -> str:
    normalized = "|".join(
        (
            format(amount, ".2f"),
            occurred_at.isoformat(timespec="minutes"),
            _normalize_location(entry_name),
            _normalize_location(exit_name),
            provider,
            external_ref.strip().lower(),
        )
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def _request_toll_changes(
    *, actor: str, changes: list[TollExpenseChange]
) -> dict[str, Any]:
    if not 1 <= len(changes) <= 20:
        raise TeslaMateCostError("changes must contain between 1 and 20 items")
    prepared: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            raise TeslaMateCostError("each change must be an object")
        operation = str(change.get("operation", "upsert"))
        if operation not in {"upsert", "void"}:
            raise TeslaMateCostError("operation must be upsert or void")
        expense_id = (
            _parse_uuid(change["expense_id"], "expense_id")
            if change.get("expense_id")
            else str(uuid.uuid4())
        )
        expected_revision = change.get("expected_revision")
        if change.get("expense_id") and not isinstance(expected_revision, int):
            raise TeslaMateCostError(
                "expected_revision is required when changing an existing expense"
            )
        if not change.get("expense_id") and operation == "void":
            raise TeslaMateCostError("expense_id is required for void")
        drive_ids = _normalize_drive_ids(change.get("drive_ids"))
        request_id = str(uuid.uuid4())
        if operation == "void":
            prepared.append(
                {
                    "request_id": request_id,
                    "operation": operation,
                    "expense_id": expense_id,
                    "expected_revision": expected_revision,
                    "amount": None,
                    "occurred_at": None,
                    "entry_name": "",
                    "exit_name": "",
                    "provider": "manual",
                    "external_ref": "",
                    "fingerprint": "",
                    "source_kind": "text",
                    "source_summary": "",
                    "journey_id": None,
                    "drive_ids": [],
                }
            )
            continue
        amount = _parse_amount(change.get("amount_cny", ""))
        occurred_at = _parse_toll_time(change.get("occurred_at", ""))
        provider = str(change.get("provider", "manual")).strip().lower()
        if provider not in {"manual", "etc", "wechat", "alipay", "other"}:
            raise TeslaMateCostError("invalid toll provider")
        source_kind = str(change.get("source_kind", "text"))
        if source_kind not in {"text", "screenshot"}:
            raise TeslaMateCostError("source_kind must be text or screenshot")
        entry_name = _sanitize_toll_text(change.get("entry_name"))
        exit_name = _sanitize_toll_text(change.get("exit_name"))
        external_ref = _sanitize_toll_text(change.get("external_ref"))
        journey_id = (
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "teslamate:toll-journey:" + ",".join(map(str, drive_ids)),
                )
            )
            if drive_ids
            else None
        )
        prepared.append(
            {
                "request_id": request_id,
                "operation": operation,
                "expense_id": expense_id,
                "expected_revision": expected_revision,
                "amount": amount,
                "occurred_at": occurred_at,
                "entry_name": entry_name,
                "exit_name": exit_name,
                "provider": provider,
                "external_ref": external_ref,
                "fingerprint": _toll_fingerprint(
                    amount=amount,
                    occurred_at=occurred_at,
                    entry_name=entry_name,
                    exit_name=exit_name,
                    provider=provider,
                    external_ref=external_ref,
                ),
                "source_kind": source_kind,
                "source_summary": _sanitize_summary(change.get("source_summary", "")),
                "journey_id": journey_id,
                "drive_ids": drive_ids,
            }
        )

    saved: list[dict[str, Any]] = []
    try:
        with _connect() as con, con.cursor() as cur:
            for item in prepared:
                cur.execute(
                    """
                    SELECT * FROM teslamate_cost_mcp.apply_toll_expense_change(
                        %s::uuid, %s, %s, %s::uuid, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s::uuid, %s::integer[]
                    )
                    """,
                    (
                        item["request_id"], actor, item["operation"],
                        item["expense_id"], item["expected_revision"],
                        item["amount"], item["occurred_at"], item["entry_name"],
                        item["exit_name"], item["provider"], item["external_ref"],
                        item["fingerprint"], item["source_kind"],
                        item["source_summary"], item["journey_id"], item["drive_ids"],
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise TeslaMateCostError("toll expense update returned no result")
                saved.append(row)
    except psycopg.Error as exc:
        message = getattr(exc.diag, "message_primary", None) or str(exc)
        raise TeslaMateCostError(message) from exc
    return {
        "saved": _json_value(saved),
        "count": len(saved),
        "actor": actor,
        "currency": "CNY",
        "write_performed": True,
    }


def _toll_history(
    *, expense_id: str | None, status: str | None, limit: int
) -> dict[str, Any]:
    if limit < 1 or limit > 50:
        raise TeslaMateCostError("limit must be between 1 and 50")
    normalized_id = _parse_uuid(expense_id, "expense_id") if expense_id else None
    if status not in {None, "matched", "pending_match", "void"}:
        raise TeslaMateCostError("invalid toll expense status")
    current = _query(
        """
        SELECT * FROM teslamate_cost_mcp.toll_expense_current
        WHERE (%s::uuid IS NULL OR expense_id = %s::uuid)
          AND (%s IS NULL OR status = %s)
        ORDER BY occurred_at DESC, expense_id DESC
        LIMIT %s
        """,
        (normalized_id, normalized_id, status, status, limit),
    )
    audit = _query(
        """
        SELECT * FROM teslamate_cost_mcp.toll_expense_history
        WHERE (%s::uuid IS NULL OR expense_id = %s::uuid)
        ORDER BY created_at DESC, audit_id DESC
        LIMIT %s
        """,
        (normalized_id, normalized_id, limit),
    )
    return {
        "expenses": _json_value(current),
        "audit": _json_value(audit),
        "count": len(current),
        "currency": "CNY",
    }


def _road_trip_cost_summary(journey_id: str) -> dict[str, Any]:
    normalized_id = _parse_uuid(journey_id, "journey_id")
    journey = _one(
        "SELECT * FROM teslamate_cost_mcp.road_journey_summary WHERE journey_id = %s::uuid",
        (normalized_id,),
    )
    if not journey:
        raise TeslaMateCostError(f"unknown journey_id: {normalized_id}")
    charges = _one(
        """
        SELECT count(*) AS session_count,
               count(*) FILTER (WHERE cost IS NULL) AS missing_cost_count,
               sum(cost) FILTER (WHERE cost IS NOT NULL) AS charging_total
        FROM teslamate_cost_mcp.charging_sessions
        WHERE car_id = %s AND start_date >= %s AND start_date <= %s
        """,
        (journey["car_id"], journey["start_date"], journey["end_date"]),
    ) or {}
    toll_total = Decimal(journey.get("toll_total") or 0)
    charging_total = Decimal(charges.get("charging_total") or 0)
    missing_count = int(charges.get("missing_cost_count") or 0)
    return {
        "journey": _json_value(journey),
        "toll_total_cny": format(toll_total, ".2f"),
        "charging_total_cny": format(charging_total, ".2f"),
        "known_total_cny": format(toll_total + charging_total, ".2f"),
        "charging_session_count": int(charges.get("session_count") or 0),
        "missing_charging_cost_count": missing_count,
        "complete": missing_count == 0,
        "currency": "CNY",
    }


def _history(*, session_id: int | None, limit: int) -> dict[str, Any]:
    if limit < 1 or limit > 50:
        raise TeslaMateCostError("limit must be between 1 and 50")
    rows = _query(
        """
        SELECT * FROM teslamate_cost_mcp.cost_history
        WHERE (%s IS NULL OR charging_process_id = %s)
        ORDER BY created_at DESC, audit_id DESC
        LIMIT %s
        """,
        (session_id, session_id, limit),
    )
    return {"changes": _json_value(rows), "count": len(rows), "currency": "CNY"}


mcp = FastMCP(
    "TeslaMate Expense Writer",
    instructions=(
        "Record TeslaMate charging and highway toll costs in CNY for authorized Feishu users. For charging, extract the final "
        "amount actually paid and a time hint from text or an attached screenshot. Resolve fuzzy times "
        "in Asia/Shanghai, call find_charging_sessions_for_cost, and show at most three candidates. Never "
        "guess when multiple candidates exist. Once every order has exactly one match, call "
        "request_charging_cost_changes immediately with all matched changes. That single call makes the Hermes host "
        "show one interactive approval card before this service runs. Never stop to ask the user for a typed confirmation, "
        "never end the turn after merely summarizing proposals, and never use the legacy prepare/commit tools for a new "
        "request. The approval card is the only confirmation. A screenshot alone "
        "does not bypass the host approval card. "
        "Treat the bill's final paid total as the cost, including service, parking and occupancy fees after "
        "discounts. For a highway toll, extract amount, passage time, entrance, exit, provider and external reference when shown, "
        "then call find_toll_journey_candidates. Use the sole high-confidence candidate automatically; when candidates are ambiguous, "
        "ask the user to select one before writing. If no candidate exists, an unmatched pending expense is allowed. Call "
        "request_toll_expense_changes immediately after matching; its host approval card is the only confirmation. Never store the "
        "original screenshot. Do not convert foreign currency or accept negative amounts."
    ),
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=[],
    ),
)


@mcp.tool()
async def find_charging_sessions_for_cost(
    from_time: str,
    to_time: str,
    location_hint: str | None = None,
    energy_kwh: str | None = None,
    unpriced_only: bool = True,
    limit: int = 3,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Find candidate sessions without writing. Resolve fuzzy dates to Asia/Shanghai before calling."""
    try:
        result = await asyncio.to_thread(
            _find_charging_sessions,
            from_time=from_time,
            to_time=to_time,
            location_hint=location_hint,
            energy_kwh=energy_kwh,
            unpriced_only=unpriced_only,
            limit=limit,
        )
        return LOCATION_PRIVACY.apply(result)
    except TeslaMateCostError as exc:
        return LOCATION_PRIVACY.apply(_validation_error_response(exc))


@mcp.tool()
async def request_charging_cost_changes(
    changes: list[ChargingCostChange],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Final action after exact matching: directly open one host approval card, then atomically write 1-20 changes. Never ask for typed confirmation first."""
    actor = _actor()
    result = await asyncio.to_thread(
        _request_changes,
        actor=actor,
        changes=changes,
    )
    return LOCATION_PRIVACY.apply(result)


@mcp.tool()
async def get_charging_cost_history(
    session_id: int | None = None,
    limit: int = 20,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List audited cost changes for correction or verification."""
    _actor()
    try:
        result = await asyncio.to_thread(_history, session_id=session_id, limit=limit)
        return LOCATION_PRIVACY.apply(result)
    except TeslaMateCostError as exc:
        return LOCATION_PRIVACY.apply(_validation_error_response(exc))


@mcp.tool()
async def find_toll_journey_candidates(
    from_time: str,
    to_time: str,
    entry_hint: str | None = None,
    exit_hint: str | None = None,
    amount_cny: str | None = None,
    provider: Literal["manual", "etc", "wechat", "alipay", "other"] | None = None,
    external_ref: str | None = None,
    limit: int = 3,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Find one-to-many TeslaMate drive sequences for a highway toll without writing."""
    try:
        result = await asyncio.to_thread(
            _find_toll_journey_candidates,
            from_time=from_time,
            to_time=to_time,
            entry_hint=entry_hint,
            exit_hint=exit_hint,
            amount_cny=amount_cny,
            provider=provider,
            external_ref=external_ref,
            limit=limit,
        )
        return LOCATION_PRIVACY.apply(result)
    except TeslaMateCostError as exc:
        return LOCATION_PRIVACY.apply(_validation_error_response(exc))


@mcp.tool()
async def request_toll_expense_changes(
    changes: list[TollExpenseChange],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create, correct, link or void 1-20 toll expenses after one host approval card."""
    actor = _actor()
    result = await asyncio.to_thread(
        _request_toll_changes,
        actor=actor,
        changes=changes,
    )
    return LOCATION_PRIVACY.apply(result)


@mcp.tool()
async def get_toll_expense_history(
    expense_id: str | None = None,
    status: Literal["matched", "pending_match", "void"] | None = None,
    limit: int = 20,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List current toll expenses and their append-only change audit."""
    _actor()
    try:
        result = await asyncio.to_thread(
            _toll_history,
            expense_id=expense_id,
            status=status,
            limit=limit,
        )
        return LOCATION_PRIVACY.apply(result)
    except TeslaMateCostError as exc:
        return LOCATION_PRIVACY.apply(_validation_error_response(exc))


@mcp.tool()
async def get_road_trip_cost_summary(
    journey_id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return a matched road journey's toll, charging and known total costs."""
    _actor()
    try:
        result = await asyncio.to_thread(_road_trip_cost_summary, journey_id)
        return LOCATION_PRIVACY.apply(result)
    except TeslaMateCostError as exc:
        return LOCATION_PRIVACY.apply(_validation_error_response(exc))


class ActorBearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, token_actors: dict[str, str]):
        super().__init__(app)
        self.token_actors = token_actors

    async def dispatch(self, request: Request, call_next: Any):
        if request.url.path.rstrip("/") == "/healthz":
            return JSONResponse({"ok": True, "service": "teslamate-cost-mcp"})
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        actor = ""
        for token, candidate_actor in self.token_actors.items():
            if hmac.compare_digest(supplied, token):
                actor = candidate_actor
                break
        if not actor:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        actor_token = CURRENT_ACTOR.set(actor)
        try:
            return await call_next(request)
        finally:
            CURRENT_ACTOR.reset(actor_token)


def main() -> None:
    parser = argparse.ArgumentParser(description="TeslaMate charging cost MCP server")
    parser.add_argument("--host", default=os.environ.get("TESLAMATE_COST_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TESLAMATE_COST_PORT", "8767")))
    args = parser.parse_args()
    if set(TOKEN_ACTORS.values()) != {"huunter", "guoguo"}:
        raise SystemExit("both Huunter and Guoguo bearer tokens are required")
    if not SIGNING_SECRET:
        raise SystemExit("TESLAMATE_COST_SIGNING_SECRET or its file is required")
    if not DB_PASSWORD:
        raise SystemExit("TESLAMATE_COST_DB_PASSWORD or its file is required")
    import uvicorn

    app = mcp.streamable_http_app()
    app.add_middleware(ActorBearerAuthMiddleware, token_actors=TOKEN_ACTORS)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
