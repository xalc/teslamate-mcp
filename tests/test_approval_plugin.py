import importlib.util
from pathlib import Path

import pytest

PLUGIN_FILES = [Path(__file__).parents[1] / "plugins/teslamate_cost_approval/__init__.py"]


def _load_plugin(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("path", "name"), zip(PLUGIN_FILES, ("approval",)))
def test_commit_requires_unique_per_proposal_approval(path, name):
    proposal_id = "f082c6f8-542a-4db6-9702-3c2df9ad635a"
    plugin = _load_plugin(path, name)

    directive = plugin._on_pre_tool_call(
        tool_name=plugin.COMMIT_TOOL,
        args={"proposal_id": proposal_id},
    )

    assert directive["action"] == "approve"
    assert directive["rule_key"].startswith("teslamate_cost_commit:")
    assert directive["once_only"] is True
    assert proposal_id in directive["message"]
    assert "Allow Once" in directive["message"]


@pytest.mark.parametrize(("path", "name"), zip(PLUGIN_FILES, ("batch",)))
def test_batch_commit_requires_one_once_only_approval(path, name):
    proposal_ids = [
        "f082c6f8-542a-4db6-9702-3c2df9ad635a",
        "63ec42b6-0c35-40ef-b20f-1cb634d302c5",
        "149c4c79-925b-4ae6-bd66-5a6d81df91b9",
    ]
    plugin = _load_plugin(path, name)

    directive = plugin._on_pre_tool_call(
        tool_name=plugin.BATCH_COMMIT_TOOL,
        args={"proposal_ids": proposal_ids},
    )

    assert directive["action"] == "approve"
    assert directive["once_only"] is True
    assert "3 条" in directive["message"]
    assert all(proposal_id in directive["message"] for proposal_id in proposal_ids)


@pytest.mark.parametrize(("path", "name"), zip(PLUGIN_FILES, ("request",)))
def test_direct_request_opens_approval_without_intermediate_confirmation(path, name):
    plugin = _load_plugin(path, name)
    directive = plugin._on_pre_tool_call(
        tool_name=plugin.REQUEST_TOOL,
        args={
            "changes": [
                {"session_id": 90, "operation": "set", "amount_cny": "21.22"},
                {"session_id": 89, "operation": "set", "amount_cny": "39.66"},
            ]
        },
    )

    assert directive["action"] == "approve"
    assert directive["once_only"] is True
    assert "session 90：¥21.22" in directive["message"]
    assert "session 89：¥39.66" in directive["message"]


@pytest.mark.parametrize(("path", "name"), zip(PLUGIN_FILES, ("invalid",)))
def test_invalid_commit_proposal_is_blocked(path, name):
    plugin = _load_plugin(path, name)

    directive = plugin._on_pre_tool_call(
        tool_name=plugin.COMMIT_TOOL,
        args={"proposal_id": "invalid"},
    )

    assert directive["action"] == "block"


def test_unrelated_tool_is_not_gated():
    plugin = _load_plugin(PLUGIN_FILES[0], "unrelated_huunter")
    assert plugin._on_pre_tool_call(tool_name="mcp__teslamate__get_health", args={}) is None
