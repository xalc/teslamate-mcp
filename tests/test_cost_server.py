from datetime import datetime
from decimal import Decimal

import pytest

from teslamate_mcp import cost_server
from teslamate_mcp.cost_server import TeslaMateCostError


@pytest.fixture(autouse=True)
def signing_secret(monkeypatch):
    monkeypatch.setattr(cost_server, "SIGNING_SECRET", b"test-signing-secret")
    with cost_server._PENDING_PROPOSALS_LOCK:
        cost_server._PENDING_PROPOSALS.clear()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", Decimal("0.00")), ("38.6", Decimal("38.60")), ("9999.99", Decimal("9999.99"))],
)
def test_parse_amount_normalizes_cny(raw, expected):
    assert cost_server._parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["-0.01", "10000", "1.001", "nan", "CNY 8"])
def test_parse_amount_rejects_unsafe_values(raw):
    with pytest.raises(TeslaMateCostError):
        cost_server._parse_amount(raw)


def test_proposal_token_is_actor_bound_and_expires():
    payload = {"actor": "huunter", "expires_at": 200, "session_id": 7}
    token = cost_server._sign_payload(payload)

    assert cost_server._verify_token(token, "huunter", now=199) == payload
    with pytest.raises(TeslaMateCostError, match="different actor"):
        cost_server._verify_token(token, "guoguo", now=199)
    with pytest.raises(TeslaMateCostError, match="expired"):
        cost_server._verify_token(token, "huunter", now=201)


def test_prepare_change_is_read_only_and_contains_exact_summary(monkeypatch):
    monkeypatch.setattr(
        cost_server,
        "_get_session",
        lambda session_id: {
            "id": session_id,
            "start_date": datetime(2026, 8, 9, 12, 0),
            "end_date": datetime(2026, 8, 9, 13, 0),
            "address": "Test Station",
            "charge_energy_used": Decimal("44.06"),
            "charge_energy_added": Decimal("42.56"),
            "cost": None,
        },
    )

    result = cost_server._prepare_change(
        actor="huunter",
        session_id=140,
        operation="set",
        amount_cny="38.6",
        source_kind="screenshot",
        source_summary="  total\npaid 38.60  ",
    )

    assert result["write_performed"] is False
    assert result["requires_host_approval_card"] is True
    assert "interactive approval card" in result["instruction"]
    assert result["proposal"]["new_cost_cny"] == "38.60"
    assert result["proposal"]["energy_kwh"] == "44.06"
    token = cost_server._pending_proposal_token(result["proposal_id"], "huunter")
    payload = cost_server._verify_token(token, "huunter")
    assert payload["source_summary"] == "total paid 38.60"


def test_find_candidates_ranks_location_and_energy(monkeypatch):
    rows = [
        {
            "id": 1,
            "start_date": datetime(2026, 8, 9, 1, 0),
            "address": "Other Station",
            "charge_energy_used": Decimal("8.00"),
            "charge_energy_added": Decimal("7.50"),
            "cost": None,
        },
        {
            "id": 2,
            "start_date": datetime(2026, 8, 9, 2, 0),
            "address": "Blue Test Charging Station",
            "charge_energy_used": Decimal("44.00"),
            "charge_energy_added": Decimal("42.00"),
            "cost": None,
        },
    ]
    monkeypatch.setattr(cost_server, "_query", lambda sql, params=(): rows)
    actor_token = cost_server.CURRENT_ACTOR.set("huunter")
    try:
        result = cost_server._find_charging_sessions(
            from_time="2026-08-09T00:00:00+08:00",
            to_time="2026-08-10T00:00:00+08:00",
            location_hint="Blue Test",
            energy_kwh="44",
            unpriced_only=True,
            limit=2,
        )
    finally:
        cost_server.CURRENT_ACTOR.reset(actor_token)

    assert [item["id"] for item in result["candidates"]] == [2, 1]
    assert result["write_performed"] is False


def test_commit_uses_signed_payload_and_returns_audit(monkeypatch):
    payload = {
        "version": 1,
        "request_id": "b573f18e-f07c-4a1a-a67a-149130a01e22",
        "actor": "huunter",
        "session_id": 140,
        "expected_cost": None,
        "new_cost": "38.60",
        "source_kind": "text",
        "source_summary": "yesterday",
        "issued_at": 1,
        "expires_at": 4102444800,
    }
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            captured["params"] = params

        def fetchone(self):
            return {
            "audit_id": 9,
            "session_id": 140,
            "previous_cost": None,
            "new_cost": Decimal("38.60"),
            "idempotent": False,
        }

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(cost_server, "_connect", FakeConnection)
    proposal_id = cost_server._store_pending_proposal(
        payload,
        cost_server._sign_payload(payload),
    )
    result = cost_server._commit_change(actor="huunter", proposal_id=proposal_id)

    assert captured["params"][1] == "huunter"
    assert captured["params"][4] == Decimal("38.60")
    assert result["saved"]["new_cost"] == "38.60"
    assert result["write_performed"] is True


def test_batch_commit_uses_one_transaction_and_returns_all_rows(monkeypatch):
    payloads = []
    for index in range(2):
        payload = {
            "version": 1,
            "request_id": f"00000000-0000-4000-8000-00000000000{index}",
            "actor": "huunter",
            "session_id": 140 + index,
            "expected_cost": None,
            "new_cost": f"{index + 1}.00",
            "source_kind": "text",
            "source_summary": "batch",
            "issued_at": 1,
            "expires_at": 4102444800,
        }
        payloads.append(payload)
        cost_server._store_pending_proposal(payload, cost_server._sign_payload(payload))

    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            executed.append(params)

        def fetchone(self):
            params = executed[-1]
            return {
                "audit_id": len(executed),
                "session_id": params[2],
                "previous_cost": None,
                "new_cost": params[4],
                "idempotent": False,
            }

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(cost_server, "_connect", FakeConnection)
    result = cost_server._commit_changes(
        actor="huunter",
        proposal_ids=[payload["request_id"] for payload in payloads],
    )

    assert len(executed) == 2
    assert result["count"] == 2
    assert [row["session_id"] for row in result["saved"]] == [140, 141]


def test_batch_commit_rejects_duplicates_before_writing(monkeypatch):
    proposal_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(
        cost_server,
        "_connect",
        lambda: pytest.fail("database must not be opened for invalid batch"),
    )
    with pytest.raises(TeslaMateCostError, match="duplicates"):
        cost_server._commit_changes(
            actor="huunter",
            proposal_ids=[proposal_id, proposal_id],
        )


def test_request_changes_prepares_and_commits_without_typed_confirmation(monkeypatch):
    prepared_calls = []

    def fake_prepare(**kwargs):
        prepared_calls.append(kwargs)
        proposal_id = f"00000000-0000-4000-8000-{kwargs['session_id']:012d}"
        return {
            "proposal_id": proposal_id,
            "proposal": {
                "session_id": kwargs["session_id"],
                "new_cost_cny": kwargs["amount_cny"],
            },
            "write_performed": False,
        }

    committed = {}

    def fake_commit(**kwargs):
        committed.update(kwargs)
        return {
            "saved": [{"session_id": item["session_id"]} for item in prepared_calls],
            "count": len(prepared_calls),
            "actor": kwargs["actor"],
            "currency": "CNY",
            "write_performed": True,
        }

    monkeypatch.setattr(cost_server, "_prepare_change", fake_prepare)
    monkeypatch.setattr(cost_server, "_commit_changes", fake_commit)

    result = cost_server._request_changes(
        actor="huunter",
        changes=[
            {"session_id": 90, "amount_cny": "21.22", "source_kind": "screenshot"},
            {"session_id": 89, "amount_cny": "39.66", "source_kind": "screenshot"},
        ],
    )

    assert result["write_performed"] is True
    assert result["count"] == 2
    assert committed["proposal_ids"] == [
        "00000000-0000-4000-8000-000000000090",
        "00000000-0000-4000-8000-000000000089",
    ]


def test_short_proposal_id_is_actor_bound_and_survives_model_roundtrip():
    payload = {
        "version": 1,
        "request_id": "c6ed6005-c322-44aa-9156-cba0aed90f67",
        "actor": "huunter",
        "session_id": 140,
        "expected_cost": None,
        "new_cost": "38.60",
        "source_kind": "text",
        "source_summary": "yesterday",
        "issued_at": 100,
        "expires_at": 4102444800,
    }
    proposal_id = cost_server._store_pending_proposal(
        payload,
        cost_server._sign_payload(payload),
    )

    assert proposal_id == payload["request_id"]
    token = cost_server._pending_proposal_token(proposal_id, "huunter", now=199)
    assert cost_server._verify_token(token, "huunter", now=199) == payload
    with pytest.raises(TeslaMateCostError, match="different actor"):
        cost_server._pending_proposal_token(proposal_id, "guoguo", now=199)
    with cost_server._PENDING_PROPOSALS_LOCK:
        cost_server._PENDING_PROPOSALS[proposal_id]["expires_at"] = 200
    with pytest.raises(TeslaMateCostError, match="expired"):
        cost_server._pending_proposal_token(proposal_id, "huunter", now=201)


def test_missing_pending_proposal_requires_prepare_again():
    with pytest.raises(TeslaMateCostError, match="prepare it again"):
        cost_server._pending_proposal_token(
            "c6ed6005-c322-44aa-9156-cba0aed90f67",
            "huunter",
        )
