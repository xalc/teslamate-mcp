import asyncio
import importlib.util
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

PLUGIN_FILE = Path(__file__).parents[1] / "plugins/teslamate_cost_approval/__init__.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("approval_plugin", PLUGIN_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_request_requires_fresh_approval_per_tool_call():
    plugin = _load_plugin()
    args = {
        "changes": [
            {"session_id": 90, "operation": "set", "amount_cny": "21.22"},
            {"session_id": 89, "operation": "clear"},
        ]
    }

    first = plugin._on_pre_tool_call(
        tool_name=plugin.REQUEST_TOOL,
        args=args,
        session_id="s1",
        turn_id="turn-1",
        tool_call_id="call-1",
    )
    repeated = plugin._on_pre_tool_call(
        tool_name=plugin.REQUEST_TOOL,
        args=args,
        session_id="s1",
        turn_id="turn-2",
        tool_call_id="call-2",
    )

    assert first["action"] == "approve"
    assert first["rule_key"].startswith("teslamate_cost_commit:")
    assert first["rule_key"] != repeated["rule_key"]
    assert "session 90：¥21.22" in first["message"]
    assert "session 89：清除费用" in first["message"]
    assert "once_only" not in first


def test_same_tool_call_is_deterministic():
    plugin = _load_plugin()
    kwargs = {
        "tool_name": plugin.COMMIT_TOOL,
        "args": {"proposal_id": "f082c6f8-542a-4db6-9702-3c2df9ad635a"},
        "session_id": "s1",
        "turn_id": "turn-1",
        "tool_call_id": "call-1",
    }
    assert plugin._on_pre_tool_call(**kwargs)["rule_key"] == plugin._on_pre_tool_call(
        **kwargs
    )["rule_key"]


def test_invalid_and_unrelated_calls_fail_safe():
    plugin = _load_plugin()
    invalid = plugin._on_pre_tool_call(
        tool_name=plugin.COMMIT_TOOL,
        args={"proposal_id": "invalid"},
    )
    assert invalid["action"] == "block"
    assert plugin._on_pre_tool_call(
        tool_name="mcp__teslamate__get_health", args={}
    ) is None


class _FakeAdapter:
    def __init__(self):
        self._client = object()
        self._approval_counter = itertools.count(10)
        self._approval_state = {}
        self.sent = []
        self.standard_calls = []

    def _format_exec_approval(self, command, description, smart_denied):
        return f"{command}\n{description}\nsmart={smart_denied}"

    async def _feishu_send_with_retry(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(code=0, data=SimpleNamespace(message_id="msg-1"))

    def _finalize_send_result(self, response, default_message):
        if response is None:
            return SimpleNamespace(success=False, message_id=None, error=default_message)
        return SimpleNamespace(success=True, message_id="msg-1", error=None)

    async def send_exec_approval(self, *args, **kwargs):
        self.standard_calls.append((args, kwargs))
        return SimpleNamespace(success=True, message_id="standard", error=None)


def test_teslamate_card_has_only_approve_and_reject():
    plugin = _load_plugin()
    adapter = _FakeAdapter()
    assert plugin._install_adapter_patch(adapter) is True
    assert plugin._install_adapter_patch(adapter) is False

    result = asyncio.run(
        adapter.send_exec_approval(
            "chat-1",
            f"<{plugin.REQUEST_TOOL}> (plugin approval rule)",
            "session-1",
            description="write one cost",
        )
    )

    assert result.success is True
    payload = json.loads(adapter.sent[0]["payload"])
    actions = payload["elements"][1]["actions"]
    assert [button["text"]["content"] for button in actions] == [
        "Approve",
        "Reject",
    ]
    assert [button["value"]["hermes_action"] for button in actions] == [
        "approve_once",
        "deny",
    ]
    assert adapter._approval_state[10] == {
        "session_key": "session-1",
        "message_id": "msg-1",
        "chat_id": "chat-1",
    }


def test_non_teslamate_approval_uses_standard_card():
    plugin = _load_plugin()
    adapter = _FakeAdapter()
    plugin._install_adapter_patch(adapter)

    result = asyncio.run(
        adapter.send_exec_approval("chat-1", "rm -rf example", "session-1")
    )

    assert result.message_id == "standard"
    assert len(adapter.standard_calls) == 1
    assert adapter.sent == []


def test_gateway_dispatch_patches_resolved_transport():
    plugin = _load_plugin()
    adapter = _FakeAdapter()
    source = object()
    event = SimpleNamespace(source=source)
    gateway = SimpleNamespace(_registered_transport_adapter=lambda value: adapter)

    assert plugin._on_pre_gateway_dispatch(event=event, gateway=gateway) is None
    assert getattr(adapter, plugin._PATCH_MARKER) is True


def test_toll_request_requires_fresh_two_button_approval():
    plugin = _load_plugin()
    first = plugin._on_pre_tool_call(
        tool_name=plugin.TOLL_REQUEST_TOOL,
        args={
            "changes": [
                {
                    "amount_cny": "89.2",
                    "occurred_at": "2026-08-25T15:00:00+08:00",
                    "entry_name": "Example East",
                    "exit_name": "Example West",
                    "drive_ids": [1855, 1857],
                    "source_kind": "screenshot",
                }
            ]
        },
        session_id="s1",
        turn_id="t1",
        tool_call_id="c1",
    )
    second = plugin._on_pre_tool_call(
        tool_name=plugin.TOLL_REQUEST_TOOL,
        args={
            "changes": [
                {
                    "amount_cny": "89.2",
                    "occurred_at": "2026-08-25T15:00:00+08:00",
                    "entry_name": "Example East",
                    "exit_name": "Example West",
                    "drive_ids": [1855, 1857],
                }
            ]
        },
        session_id="s1",
        turn_id="t2",
        tool_call_id="c2",
    )

    assert first["action"] == "approve"
    assert "Example East → Example West：¥89.20（关联 2 段行程）" in first["message"]
    assert first["rule_key"] != second["rule_key"]
    assert "once_only" not in first


def test_toll_request_rejects_invalid_payload():
    plugin = _load_plugin()
    directive = plugin._on_pre_tool_call(
        tool_name=plugin.TOLL_REQUEST_TOOL,
        args={"changes": [{"amount_cny": "-1", "occurred_at": "today"}]},
    )
    assert directive["action"] == "block"
