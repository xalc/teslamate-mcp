"""Require a one-time human approval for every TeslaMate charging-cost write."""

from __future__ import annotations

import uuid
from typing import Any


COMMIT_TOOL = "mcp__teslamate_cost__commit_charging_cost_change"
BATCH_COMMIT_TOOL = "mcp__teslamate_cost__commit_charging_cost_changes"
REQUEST_TOOL = "mcp__teslamate_cost__request_charging_cost_changes"


def _parse_proposal_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid proposal id") from exc


def _on_pre_tool_call(
    tool_name: str = "", args: Any = None, **_: Any
) -> dict[str, Any] | None:
    if tool_name not in {COMMIT_TOOL, BATCH_COMMIT_TOOL, REQUEST_TOOL}:
        return None
    if not isinstance(args, dict):
        return {"action": "block", "message": "费用写入缺少有效提案，已阻止。"}
    try:
        if tool_name == REQUEST_TOOL:
            changes = args.get("changes")
            if not isinstance(changes, list) or not 1 <= len(changes) <= 20:
                raise ValueError("changes must contain between 1 and 20 items")
            normalized_changes = []
            for change in changes:
                if not isinstance(change, dict) or isinstance(
                    change.get("session_id"), bool
                ):
                    raise ValueError("each change requires an integer session_id")
                try:
                    session_id = int(change["session_id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "each change requires an integer session_id"
                    ) from exc
                operation = change.get("operation", "set")
                amount = change.get("amount_cny")
                if operation not in {"set", "clear"}:
                    raise ValueError("operation must be set or clear")
                if operation == "set" and not isinstance(amount, str):
                    raise ValueError("amount_cny is required for set")
                normalized_changes.append((session_id, operation, amount))
            details = "；".join(
                f"session {session_id}："
                f"{'清除费用' if operation == 'clear' else '¥' + str(amount)}"
                for session_id, operation, amount in normalized_changes
            )
            reason = (
                f"确认一次性写入 {len(normalized_changes)} 条 TeslaMate 充电费用："
                f"{details}。点击 Allow Once 后整批写入；任一条失败则全部回滚。"
                "拒绝或超时不会写入。"
            )
            batch_key = uuid.uuid5(uuid.NAMESPACE_URL, repr(normalized_changes))
            return {
                "action": "approve",
                "message": reason,
                "rule_key": f"teslamate_cost_commit:{batch_key}",
                "once_only": True,
            }
        if tool_name == COMMIT_TOOL:
            proposal_ids = [_parse_proposal_id(args.get("proposal_id"))]
        else:
            raw_ids = args.get("proposal_ids")
            if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 20:
                raise ValueError("proposal_ids must contain between 1 and 20 items")
            proposal_ids = [_parse_proposal_id(value) for value in raw_ids]
            if len(set(proposal_ids)) != len(proposal_ids):
                raise ValueError("proposal_ids must not contain duplicates")
    except ValueError as exc:
        return {"action": "block", "message": f"费用写入提案无效，已阻止：{exc}"}

    proposal_list = "、".join(proposal_ids)
    reason = (
        f"确认一次性写入刚才展示的 {len(proposal_ids)} 条 TeslaMate 充电费用提案："
        f"{proposal_list}。点击 Allow Once 后整批写入；任一条失败则全部回滚。"
        "拒绝或超时不会写入。"
    )
    batch_key = uuid.uuid5(uuid.NAMESPACE_URL, "|".join(proposal_ids))
    return {
        "action": "approve",
        "message": reason,
        "rule_key": f"teslamate_cost_commit:{batch_key}",
        "once_only": True,
    }


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)

