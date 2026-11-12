"""Compact, field-aware query replies; escaping and pagination belong to callers.

Only documented business fields are presented. Every item in an API page is
retained, including an explicit placeholder for malformed rows. This module
never serializes an unknown object or silently turns missing data into success.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

_MISSING = object()
_BEIJING = timezone(timedelta(hours=8))
_ROLES = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
_RECEIVE_MODES = {
    "all": "全部消息",
    "only_mention": "仅 @机器人 的消息",
    "mention_and_context": "@机器人 及相关上下文",
}
_AUDIT_STATUS = {
    "success": "成功",
    "partial": "部分失败",
    "failed": "失败",
    "unknown": "结果未知",
    "executing": "执行中",
}
_WEEKDAYS = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


def _text(value: Any, label: str, *, empty: str = "") -> str:
    if isinstance(value, str):
        if value.strip():
            return value
        if empty:
            return empty
    return f"未返回{label}"


def _nickname(row: Mapping[str, Any]) -> str:
    value = row.get("username")
    if not isinstance(value, str) or not value.strip():
        return "未返回昵称"
    value = " ".join(value.split())
    return value if len(value) <= 30 else value[:29] + "…"


def _enum(value: Any, choices: Mapping[str, str], label: str) -> str:
    return (
        choices.get(value, f"未返回可识别的{label}") if isinstance(value, str) else f"未返回{label}"
    )


def _boolean(value: Any, label: str, *, yes: str = "开启", no: str = "关闭") -> str:
    if value is True:
        return yes
    if value is False:
        return no
    return f"未返回{label}"


def _number(value: Any, label: str) -> str:
    return str(value) if type(value) is int and value >= 0 else f"未返回{label}"


def _time(value: Any, label: str) -> str:
    try:
        if type(value) in (int, float):
            parsed = datetime.fromtimestamp(value, _BEIJING)
        elif isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return f"未返回有效{label}"
            parsed = parsed.astimezone(_BEIJING)
        else:
            return f"未返回{label}"
        return parsed.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）"
    except (ValueError, OverflowError, OSError):
        return f"未返回有效{label}"


def _optional_text(lines: list[str], row: Mapping[str, Any], key: str, label: str) -> None:
    value = row.get(key, _MISSING)
    if value is _MISSING or value == "":
        return
    lines.append(f"{label}：{_text(value, label)}")


def _collection(data: Mapping[str, Any], key: str, label: str) -> tuple[list[Any] | None, str]:
    value = data.get(key)
    if not isinstance(value, list):
        return None, f"未返回{label}。"
    return value, ""


def _with_next_page(text: str, data: Mapping[str, Any]) -> str:
    cursor = data.get("next_cursor")
    return text + "\n还有下一页。" if isinstance(cursor, str) and cursor else text


def _member_heading(row: Mapping[str, Any], *, role: bool = True) -> str:
    details = []
    if role:
        details.append(_enum(row.get("member_role"), _ROLES, "群身份"))
    if row.get("bot") is True:
        details.append("机器人")
    return _nickname(row) + ("（" + " · ".join(details) + "）" if details else "")


def _group_info(data: Mapping[str, Any]) -> str:
    lines = [
        "群名：" + _text(data.get("group_name"), "群名"),
        "人数：" + _number(data.get("group_member_num"), "群人数"),
    ]
    _optional_text(lines, data, "group_class_text", "分类")
    _optional_text(lines, data, "group_finger_memo", "简介")
    tags = data.get("group_tags", _MISSING)
    if isinstance(tags, list) and tags:
        lines.append("标签：" + "、".join(_text(tag, "标签") for tag in tags))
    elif tags is not _MISSING and not isinstance(tags, list):
        lines.append("未返回有效群标签。")
    return "\n".join(lines)


def _bot_state(data: Mapping[str, Any]) -> str:
    lines = [
        "群内身份：" + _enum(data.get("member_role"), _ROLES, "群身份"),
        "主动消息："
        + _boolean(data.get("allow_proactive_msg"), "主动消息设置", yes="允许", no="不允许"),
        "接收消息：" + _enum(data.get("recv_msg_setting"), _RECEIVE_MODES, "消息接收设置"),
    ]
    if "joined_at" in data:
        lines.append("入群时间：" + _time(data["joined_at"], "入群时间"))
    return "\n".join(lines)


def _members(data: Mapping[str, Any], *, blacklist: bool = False) -> str:
    label = "黑名单列表" if blacklist else "成员列表"
    rows, error = _collection(data, "users" if blacklist else "members", label)
    if rows is None:
        return error
    if not rows:
        return _with_next_page("暂无黑名单成员。" if blacklist else "暂无成员。", data)
    blocks = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            blocks.append(f"{index}. 未返回完整成员信息。")
            continue
        lines = [
            f"{index}. {_member_heading(row, role=not blacklist)}",
            "成员ID：" + _text(row.get("member_openid"), "成员ID"),
        ]
        if blacklist:
            lines.append("拉黑时间：" + _time(row.get("banned_at"), "拉黑时间"))
        blocks.append("\n".join(lines))
    return _with_next_page("\n\n".join(blocks), data)


def _member_detail(data: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            _member_heading(data),
            "成员ID：" + _text(data.get("member_openid"), "成员ID"),
            "账号类型：" + _boolean(data.get("bot"), "账号类型", yes="机器人", no="用户"),
            "入群时间：" + _time(data.get("joined_at"), "入群时间"),
        ]
    )


def _mute_rules(global_rule: Any) -> list[str]:
    if not isinstance(global_rule, Mapping):
        return ["未返回全员禁言设置。"]
    lines = [
        "全员禁言："
        + _enum(
            global_rule.get("mode"),
            {"none": "关闭", "always": "开启", "schedule": "按计划"},
            "全员禁言模式",
        )
    ]
    for key, label in (("schedule_rules", "定时规则"), ("recurring_rules", "周期规则")):
        rules = global_rule.get(key, _MISSING)
        if rules is _MISSING:
            if global_rule.get("mode") == "schedule":
                lines.append(f"未返回{label}。")
            continue
        if not isinstance(rules, list):
            lines.append(f"未返回有效{label}。")
            continue
        for index, rule in enumerate(rules, 1):
            if not isinstance(rule, Mapping):
                lines.append(f"{label}{index}：未返回完整规则。")
                continue
            enabled = _boolean(rule.get("enabled"), "启用状态", yes="启用", no="停用")
            if key == "schedule_rules":
                period = (
                    _time(rule.get("start_at"), "开始时间")
                    + " 至 "
                    + _time(rule.get("end_at"), "结束时间")
                )
            else:
                weekdays = rule.get("weekdays")
                days = (
                    "、".join(
                        _WEEKDAYS.get(day, "未知星期") if type(day) is int else "未知星期"
                        for day in weekdays
                    )
                    if isinstance(weekdays, list) and weekdays
                    else "未返回周期星期"
                )
                period = (
                    days
                    + " "
                    + _text(rule.get("start_time"), "开始时刻")
                    + " 至 "
                    + _text(rule.get("end_time"), "结束时刻")
                    + "（北京时间）"
                )
            lines.append(f"{label}{index}：{period} · {enabled}")
    return lines


def _mute_state(data: Mapping[str, Any]) -> str:
    blocks = ["\n".join(_mute_rules(data.get("global_rule")))]
    rows, error = _collection(data, "members", "成员禁言列表")
    if rows is None:
        blocks.append(error)
    elif not rows:
        blocks.append("暂无成员禁言。")
    else:
        for index, row in enumerate(rows, 1):
            if not isinstance(row, Mapping):
                blocks.append(f"{index}. 未返回完整禁言信息。")
                continue
            blocks.append(
                f"{index}. {_nickname(row)}\n"
                "成员ID："
                + _text(row.get("member_openid"), "成员ID")
                + "\n禁言至："
                + _time(row.get("mute_expire_at"), "禁言到期时间")
            )
    return "\n\n".join(blocks)


def _verification(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["未返回入群验证信息。"]
    method = value.get("method")
    lines: list[str] = []
    if method not in ("verify_message", "admin_review_qa"):
        lines.append("未返回可识别的入群验证方式。")
    # Render both documented forms if present. A malformed method must not hide
    # a supplied verification message or any question/answer from the reviewer.
    if method == "verify_message" or "verify_message" in value:
        message = value.get("verify_message", _MISSING)
        if method == "verify_message" or message not in (_MISSING, ""):
            lines.append("验证消息：" + _text(message, "验证消息", empty="未填写"))
    if method == "admin_review_qa" or "review_qa_list" in value:
        pairs = value.get("review_qa_list")
        if not isinstance(pairs, list):
            lines.append("未返回验证问答。")
        elif not pairs and method == "admin_review_qa":
            lines.append("验证问答：未填写。")
        for index, pair in enumerate(pairs if isinstance(pairs, list) else [], 1):
            if not isinstance(pair, Mapping):
                lines.append(f"问答{index}：未返回完整内容。")
                continue
            lines.extend(
                [
                    f"问题{index}：" + _text(pair.get("question"), "问题", empty="未填写"),
                    "回答：" + _text(pair.get("answer"), "回答", empty="未填写"),
                ]
            )
    return lines


def _join_requests(data: Mapping[str, Any]) -> str:
    rows, error = _collection(data, "list", "入群申请列表")
    if rows is None:
        return error
    if not rows:
        return _with_next_page("暂无待处理入群申请。", data)
    blocks = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            blocks.append(f"申请{index}：未返回完整申请信息。")
            continue
        lines = [
            f"申请{index}：{_member_heading(row, role=False)}",
            "成员ID：" + _text(row.get("member_openid"), "成员ID"),
            "申请ID：" + _text(row.get("join_request_id"), "申请ID"),
            "申请时间：" + _time(row.get("apply_at"), "申请时间"),
            "来源："
            + _enum(
                row.get("apply_source"),
                {"self_apply": "主动申请", "invited": "成员邀请"},
                "申请来源",
            ),
        ]
        if row.get("apply_source") == "invited" or row.get("invited_by"):
            lines.append("邀请人：" + _text(row.get("invited_by"), "邀请人ID"))
        if "auto_approved" in row:
            approval = row["auto_approved"]
            if isinstance(approval, Mapping):
                lines.append("状态：已自动通过")
                _optional_text(lines, approval, "strategy_id", "审批策略")
            else:
                lines.append("未返回有效的自动审批状态。")
        lines.extend(_verification(row.get("verify_info")))
        risk = row.get("risk_tips", _MISSING)
        if risk != "":
            lines.append("风险提示：" + _text(risk, "风险提示"))
        blocks.append("\n".join(lines))
    return _with_next_page("\n\n".join(blocks), data)


def _strategy_scope(row: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    known = False
    for key, label in (("group_openids", "群 OpenID"), ("group_ids", "QQ 群号")):
        groups = row.get(key, _MISSING)
        if groups is _MISSING:
            continue
        if not isinstance(groups, list):
            lines.append(f"未返回有效关联{label}。")
            continue
        known = True
        if groups:
            values = [
                str(group)
                if key == "group_ids" and type(group) is int and group > 0
                else _text(group, label)
                for group in groups
            ]
            lines.append(f"关联{label}（{len(groups)} 个）：" + "、".join(values))
    if not lines:
        lines.append("关联群：无。" if known else "未返回关联群范围。")
    return lines


def _strategies(data: Mapping[str, Any]) -> str:
    rows, error = _collection(data, "strategies", "审批策略列表")
    if rows is None:
        return error
    if not rows:
        return _with_next_page("暂无生效中的审批策略。", data)
    blocks = ["以下策略作用于关联的全部群。"]
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            blocks.append(f"策略{index}：未返回完整策略信息。")
            continue
        lines = [
            f"策略{index}：" + _text(row.get("strategy_id"), "策略ID"),
            "状态：" + _enum(row.get("is_enable"), {"on": "启用", "off": "关闭"}, "启用状态"),
            "到期：" + _time(row.get("expire_at"), "策略到期时间"),
        ]
        lines.extend(_strategy_scope(row))
        count = row.get("whitelist_user_count")
        lines.append(
            "白名单：" + (f"约 {count} 人" if type(count) is int and count >= 0 else "未返回人数")
        )
        _optional_text(lines, row, "remark", "备注")
        if "updated_at" in row:
            lines.append("更新：" + _time(row["updated_at"], "更新时间"))
        blocks.append("\n".join(lines))
    return _with_next_page("\n\n".join(blocks), data)


def _audit(data: Mapping[str, Any]) -> str:
    rows, error = _collection(data, "audit", "操作日志")
    if rows is None:
        return error
    if not rows:
        return "暂无操作记录。"
    blocks = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            blocks.append(f"记录{index}：未返回完整操作记录。")
            continue
        identifier = _number(row.get("id"), "记录编号")
        action = _text(row.get("action"), "操作名称")
        state = _enum(row.get("status"), _AUDIT_STATUS, "操作状态")
        lines = [f"#{identifier} {action} · {state}", _time(row.get("created"), "操作时间")]
        if row.get("status") != "success":
            lines.append(_text(row.get("detail"), "失败详情"))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _recent_messages(data: Mapping[str, Any]) -> str:
    rows, error = _collection(data, "messages", "最近消息列表")
    if rows is None:
        return error
    if not rows:
        return "最近两分钟暂无可见消息。"
    blocks = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            blocks.append(f"消息{index}：未返回完整消息信息。")
            continue
        blocks.append(
            f"消息{index}："
            + _text(row.get("message_id"), "消息ID")
            + "\n发送者："
            + _text(row.get("member_openid"), "发送者ID")
            + "\n时间："
            + _time(row.get("received_at"), "消息时间")
        )
    return "\n\n".join(blocks)


def format_read_result(command: str, data: dict) -> str:
    """Render one complete API page without raw JSON or unrelated identifiers."""
    if not isinstance(data, Mapping):
        return "未返回有效查询结果。"
    formatters = {
        "信息": _group_info,
        "状态": _bot_state,
        "禁言状态": _mute_state,
        "成员": _members,
        "成员详情": _member_detail,
        "黑名单": lambda value: _members(value, blacklist=True),
        "入群": _join_requests,
        "策略列表": _strategies,
        "日志": _audit,
        "最近消息": _recent_messages,
    }
    formatter = formatters.get(command)
    if formatter is None:
        raise ValueError("不支持的查询指令。")
    return formatter(data)
