"""Require a fresh, two-button approval for TeslaMate expense writes."""

from __future__ import annotations

import json
import logging
import types
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

COMMIT_TOOL = "mcp__teslamate_cost__commit_charging_cost_change"
BATCH_COMMIT_TOOL = "mcp__teslamate_cost__commit_charging_cost_changes"
REQUEST_TOOL = "mcp__teslamate_cost__request_charging_cost_changes"
TOLL_REQUEST_TOOL = "mcp__teslamate_cost__request_toll_expense_changes"
_COST_TOOLS = {COMMIT_TOOL, BATCH_COMMIT_TOOL, REQUEST_TOOL, TOLL_REQUEST_TOOL}
_PATCH_MARKER = "_teslamate_cost_two_button_approval"

logger = logging.getLogger(__name__)


def _is_cost_approval(command: str) -> bool:
    return any(f"<{tool_name}>" in str(command or "") for tool_name in _COST_TOOLS)


def _build_cost_approval_card(
    adapter: Any,
    *,
    command: str,
    description: str,
    approval_id: int,
    smart_denied: bool = False,
) -> dict[str, Any]:
    """Build the TeslaMate-only card with one approve and one reject action."""

    def _button(label: str, action: str, button_type: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": button_type,
            "value": {"hermes_action": action, "approval_id": approval_id},
        }

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "TeslaMate Expense Approval", "tag": "plain_text"},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": adapter._format_exec_approval(
                    command, description, smart_denied
                ),
            },
            {
                "tag": "action",
                "actions": [
                    _button("Approve", "approve_once", "primary"),
                    _button("Reject", "deny", "danger"),
                ],
            },
        ],
    }


def _install_adapter_patch(adapter: Any) -> bool:
    """Patch the live Feishu adapter without modifying the official image."""
    if adapter is None or getattr(adapter, _PATCH_MARKER, False):
        return False
    required = (
        "send_exec_approval",
        "_format_exec_approval",
        "_feishu_send_with_retry",
        "_finalize_send_result",
        "_approval_counter",
        "_approval_state",
    )
    if any(not hasattr(adapter, name) for name in required):
        return False

    original = adapter.send_exec_approval

    async def _send_exec_approval(
        self: Any,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: dict[str, Any] | None = None,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> Any:
        if not _is_cost_approval(command):
            return await original(
                chat_id,
                command,
                session_key,
                description=description,
                metadata=metadata,
                allow_permanent=allow_permanent,
                allow_session=allow_session,
                smart_denied=smart_denied,
            )
        if not getattr(self, "_client", None):
            return await original(
                chat_id,
                command,
                session_key,
                description=description,
                metadata=metadata,
                allow_permanent=False,
                allow_session=False,
                smart_denied=smart_denied,
            )

        try:
            approval_id = next(self._approval_counter)
            card = _build_cost_approval_card(
                self,
                command=command,
                description=description,
                approval_id=approval_id,
                smart_denied=smart_denied,
            )
            response = await self._feishu_send_with_retry(
                chat_id=chat_id,
                msg_type="interactive",
                payload=json.dumps(card, ensure_ascii=False),
                reply_to=None,
                metadata=metadata,
            )
            result = self._finalize_send_result(
                response, "TeslaMate cost approval send failed"
            )
            if result.success:
                self._approval_state[approval_id] = {
                    "session_key": session_key,
                    "message_id": result.message_id or "",
                    "chat_id": chat_id,
                }
            return result
        except Exception:
            logger.exception("failed to send TeslaMate two-button approval card")
            return self._finalize_send_result(
                None, "TeslaMate cost approval send failed"
            )

    adapter.send_exec_approval = types.MethodType(_send_exec_approval, adapter)
    setattr(adapter, _PATCH_MARKER, True)
    logger.info("TeslaMate two-button Feishu approval patch installed")
    return True


def _on_pre_gateway_dispatch(
    event: Any = None,
    gateway: Any = None,
    **_: Any,
) -> None:
    """Install the card patch on the transport handling the inbound request."""
    source = getattr(event, "source", None)
    if source is None or gateway is None:
        return None
    for resolver_name in ("_registered_transport_adapter", "_adapter_for_source"):
        resolver = getattr(gateway, resolver_name, None)
        if not callable(resolver):
            continue
        try:
            adapter = resolver(source)
        except Exception:
            continue
        if adapter is not None:
            _install_adapter_patch(adapter)
            break
    return None


def _parse_proposal_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid proposal id") from exc


def _parse_toll_amount(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("amount_cny is required for upsert")
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount_cny must be a decimal number") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("9999.99"):
        raise ValueError("amount_cny must be between 0.00 and 9999.99")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("amount_cny must have at most two decimal places")
    return format(amount, ".2f")


def _parse_drive_ids(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("drive_ids must contain between 0 and 20 items")
    result = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("drive_ids must contain positive integers")
        try:
            drive_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("drive_ids must contain positive integers") from exc
        if drive_id <= 0:
            raise ValueError("drive_ids must contain positive integers")
        result.append(drive_id)
    if len(set(result)) != len(result):
        raise ValueError("drive_ids must not contain duplicates")
    return result


def _fresh_rule_key(
    subject: str,
    *,
    session_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
) -> str:
    """Return an approval key that cannot authorize a later tool call.

    Official Hermes v0.20.1 supports ``action: approve`` and ``rule_key`` but
    not the old local ``once_only`` extension.  Binding the rule to Hermes'
    per-call correlation IDs preserves one-write-per-approval semantics even
    if a client offers (and the user selects) session/permanent approval.
    """

    call_scope = "|".join(
        value for value in (session_id, turn_id, tool_call_id) if value
    )
    if not call_scope:
        call_scope = uuid.uuid4().hex
    unique_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{call_scope}|{subject}")
    return f"teslamate_cost_commit:{unique_id}"


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    session_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> dict[str, Any] | None:
    if tool_name not in _COST_TOOLS:
        return None
    if not isinstance(args, dict):
        return {"action": "block", "message": "费用写入缺少有效提案，已阻止。"}

    try:
        if tool_name == TOLL_REQUEST_TOOL:
            changes = args.get("changes")
            if not isinstance(changes, list) or not 1 <= len(changes) <= 20:
                raise ValueError("changes must contain between 1 and 20 items")
            normalized_tolls = []
            details = []
            for change in changes:
                if not isinstance(change, dict):
                    raise ValueError("each change must be an object")
                operation = change.get("operation", "upsert")
                if operation not in {"upsert", "void"}:
                    raise ValueError("operation must be upsert or void")
                expense_id = change.get("expense_id")
                if expense_id:
                    expense_id = _parse_proposal_id(expense_id)
                    if not isinstance(change.get("expected_revision"), int):
                        raise ValueError(
                            "expected_revision is required for an existing expense"
                        )
                if operation == "void":
                    if not expense_id:
                        raise ValueError("expense_id is required for void")
                    normalized_tolls.append((operation, expense_id))
                    details.append(f"作废高速费 {expense_id}")
                    continue
                amount = _parse_toll_amount(change.get("amount_cny"))
                occurred_at = change.get("occurred_at")
                if not isinstance(occurred_at, str) or not occurred_at.strip():
                    raise ValueError("occurred_at is required for upsert")
                drive_ids = _parse_drive_ids(change.get("drive_ids"))
                source_kind = change.get("source_kind", "text")
                if source_kind not in {"text", "screenshot"}:
                    raise ValueError("source_kind must be text or screenshot")
                entry_name = str(change.get("entry_name") or "未提供入口")[:120]
                exit_name = str(change.get("exit_name") or "未提供出口")[:120]
                normalized_tolls.append(
                    (operation, expense_id, amount, occurred_at, tuple(drive_ids))
                )
                match_text = (
                    f"关联 {len(drive_ids)} 段行程"
                    if drive_ids
                    else "待匹配行程"
                )
                details.append(
                    f"{entry_name} → {exit_name}：¥{amount}（{match_text}）"
                )
            subject = repr(normalized_tolls)
            reason = (
                f"确认本次处理 {len(normalized_tolls)} 条 TeslaMate 高速费用："
                + "；".join(details)
                + "。批准后整批写入并记录审计；任一条失败则全部回滚。"
                "拒绝或超时不会写入。该授权只绑定当前工具调用。"
            )
        elif tool_name == REQUEST_TOOL:
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
                    charging_session_id = int(change["session_id"])
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
                normalized_changes.append(
                    (charging_session_id, operation, amount)
                )

            details = "；".join(
                f"session {charging_session_id}："
                f"{'清除费用' if operation == 'clear' else '¥' + str(amount)}"
                for charging_session_id, operation, amount in normalized_changes
            )
            subject = repr(normalized_changes)
            reason = (
                f"确认本次写入 {len(normalized_changes)} 条 TeslaMate 充电费用：{details}。"
                "批准后整批写入；任一条失败则全部回滚。拒绝或超时不会写入。"
                "该授权只绑定当前工具调用，后续费用写入仍会重新审批。"
            )
        else:
            if tool_name == COMMIT_TOOL:
                proposal_ids = [_parse_proposal_id(args.get("proposal_id"))]
            else:
                raw_ids = args.get("proposal_ids")
                if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 20:
                    raise ValueError(
                        "proposal_ids must contain between 1 and 20 items"
                    )
                proposal_ids = [_parse_proposal_id(value) for value in raw_ids]
                if len(set(proposal_ids)) != len(proposal_ids):
                    raise ValueError("proposal_ids must not contain duplicates")

            proposal_list = "、".join(proposal_ids)
            subject = "|".join(proposal_ids)
            reason = (
                f"确认本次写入刚才展示的 {len(proposal_ids)} 条 TeslaMate 充电费用提案："
                f"{proposal_list}。批准后整批写入；任一条失败则全部回滚。"
                "拒绝或超时不会写入。该授权只绑定当前工具调用，"
                "后续费用写入仍会重新审批。"
            )
    except ValueError as exc:
        return {"action": "block", "message": f"费用写入提案无效，已阻止：{exc}"}

    return {
        "action": "approve",
        "message": reason,
        "rule_key": _fresh_rule_key(
            subject,
            session_id=session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        ),
    }


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
