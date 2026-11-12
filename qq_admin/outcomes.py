"""Short, truthful mutation receipts with explicitly typed member mentions."""

from __future__ import annotations

import re
from typing import Any

from .panels import MemberMention, Panel, text_reply


def mute_duration(seconds: int) -> str:
    minutes, remainder = divmod(seconds, 60)
    return (f"{minutes}分钟" if minutes else "") + (f"{remainder}秒" if remainder else "")


def _mentions(members: list[str]) -> list[str | MemberMention]:
    parts: list[str | MemberMention] = []
    for index, member in enumerate(members):
        if index:
            parts.append(" ")
        parts.append(MemberMention(member))
    return parts


def write_result(action: str, params: dict[str, Any], result: dict) -> tuple[str, Panel]:
    """Return audit status and a concise receipt; never dump upstream fields."""
    members = params.get("members", [])
    failures = result.get("fail_openids", [])
    blacklist_failures = result.get("add_to_member_blacklist_fail_openids", [])
    for values in (failures, blacklist_failures):
        if not isinstance(values, list) or any(
            not isinstance(member, str) or member not in members for member in values
        ):
            return "unknown", text_reply("结果未确认，请先查询状态。")
    failures = set(failures)
    blacklist_failures = set(blacklist_failures)
    succeeded = [member for member in members if member not in failures]
    failed = [member for member in members if member in failures]
    if failures or blacklist_failures:
        status = "partial" if succeeded or blacklist_failures else "failed"
    else:
        status = "success"

    if action in {"禁言", "更新禁言", "解禁"}:
        parts: list[str | MemberMention] = []
        if succeeded:
            parts.append({"禁言": "已禁言 ", "更新禁言": "已更新禁言 ", "解禁": "已解禁 "}[action])
            parts.extend(_mentions(succeeded))
            if action != "解禁":
                parts.append(" " + mute_duration(params["seconds"]))
        if failed:
            if parts:
                parts.append("\n")
            parts.append(f"{action}失败 ")
            parts.extend(_mentions(failed))
        return status, text_reply(*parts)

    if action == "移出":
        if result.get("remove_members_result") != "success":
            return "unknown", text_reply("结果未确认，请先查询状态。")
        message = f"已移出{len(succeeded)}名成员" if succeeded else "移出失败"
        if params.get("add_to_blacklist") and succeeded:
            if blacklist_failures:
                message += f"，其中{len(blacklist_failures)}名拉黑失败"
            else:
                message += "并拉黑"
        if failed:
            message += f"；{len(failed)}名移出失败"
        return status, text_reply(message)

    if action in {"拉黑", "取消拉黑"}:
        message = f"已{action}{len(succeeded)}名成员" if succeeded else f"{action}失败"
        if failed:
            message += "；失败成员：" + "、".join(failed)
        return status, text_reply(message)

    if action in {"同意", "拒绝"}:
        message = f"已{action}入群申请"
        if action == "拒绝" and params.get("add_to_blacklist"):
            message += "并拉黑"
        return status, text_reply(message)
    if action == "策略创建":
        strategy_id = result.get("strategy_id")
        suffix = (
            f"：{strategy_id}"
            if isinstance(strategy_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", strategy_id)
            else "，可用 /策略列表 查看"
        )
        return status, text_reply("已创建策略" + suffix)
    messages = {
        "策略修改": "已修改策略",
        "策略删除": "已删除策略",
        "策略执行": "策略执行已提交，预计约10分钟完成",
        "策略白名单": "已更新策略白名单",
        "撤回": "已撤回消息",
        "通知": "通知已发送",
        "全体通知": "全体通知已发送",
    }
    return status, text_reply(messages.get(action, "操作已完成"))
