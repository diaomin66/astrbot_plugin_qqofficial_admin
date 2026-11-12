"""Normalize direct commands and actual QQ member mentions without guessing names."""

from __future__ import annotations

import re
from typing import Any

from .integration import MENTION_MARKUP, MentionContext, _field, mention_context

DIRECT_COMMANDS = frozenset(
    {
        "面板",
        "菜单",
        "帮助",
        "help",
        "身份",
        "我的ID",
        "信息",
        "状态",
        "成员",
        "成员详情",
        "移出",
        "黑名单",
        "拉黑",
        "取消拉黑",
        "禁言状态",
        "禁言",
        "更新禁言",
        "解禁",
        "入群",
        "同意",
        "拒绝",
        "策略列表",
        "策略创建",
        "策略修改",
        "策略删除",
        "策略执行",
        "策略白名单",
        "最近消息",
        "撤回",
        "通知",
        "全体通知",
        "日志",
        "结果",
        "确认",
        "取消",
    }
)
MEMBER_COMMANDS = frozenset(
    {"禁言", "更新禁言", "解禁", "移出", "拉黑", "取消拉黑", "成员详情", "同意", "拒绝"}
)
_DURATION = re.compile(r"[0-9]+(?:s|m|h|d|秒|分钟|小时|天)?", re.I)
_DURATION_LIKE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[a-z]+|秒|分钟|小时|天)?", re.I)
_SEPARATORS = re.compile(r"^[,，\s]+")
_HEAD = re.compile(r"(?P<head>\S+)(?P<space>\s*)(?P<body>[\s\S]*)")
_TOKEN = re.compile(r"[^,，\s]+")
_TARGET_ERROR = "无法识别 @成员，请在 QQ 中输入 @ 并选择成员；也可填写 /成员 查询到的 OpenID。"


def _command_parts(text: str, mentions: MentionContext) -> tuple[str, str]:
    text = text.strip()
    while match := MENTION_MARKUP.match(text):
        # Compatible wrappers may retain the initial mention used to wake the
        # bot. Only discard identities explicitly known to be robots.
        value = match[1] or match[2]
        if value not in mentions.excluded:
            break
        text = text[match.end() :].lstrip()
    text = text.removeprefix("/")
    match = _HEAD.fullmatch(text)
    if match is None:
        return "面板", ""
    command, body = match["head"], match["body"]
    if command in {"群管", "qqadmin"}:
        return _command_parts(body, mentions)
    return command, body


def _member_source(event: Any, command: str, body: str, mentions: MentionContext) -> str:
    """Recover mention positions when an adapter omitted them from message_str."""
    raw = _field(event.message_obj, "raw_message")
    for source in (raw, _field(raw, "raw_data")):
        text = str(_field(source, "content", "") or "")
        source_command, source_body = _command_parts(text, mentions)
        if source_command == command and MENTION_MARKUP.search(source_body):
            return source_body
    components: list[str] = []
    has_member_at = False
    for component in event.get_messages():
        kind = type(component).__name__
        if kind == "Plain":
            components.append(str(_field(component, "text", "") or ""))
        elif kind == "At":
            user_id = str(_field(component, "qq", "") or "")
            member_id = mentions.resolve(user_id)
            if member_id:
                components.append(f" <@{member_id}> ")
                has_member_at = True
    if has_member_at:
        source_command, source_body = _command_parts("".join(components), mentions)
        if source_command == command:
            return source_body
    return body


def _member_tokens(
    text: str, mentions: MentionContext, *, mute_targets: bool = False
) -> tuple[list[str], bool]:
    values: list[str] = []
    has_markup = False
    remaining = text.strip()
    while remaining:
        if match := MENTION_MARKUP.match(remaining):
            has_markup = True
            value = mentions.resolve(match[1] or match[2])
        else:
            match = _TOKEN.match(remaining)
            if match is None:
                remaining = _SEPARATORS.sub("", remaining)
                continue
            value = match[0]
            if any(character in value for character in "@<>"):
                raise ValueError(_TARGET_ERROR)
            if (
                mute_targets
                and _DURATION_LIKE.fullmatch(value)
                and re.fullmatch(r"[A-Fa-f0-9]{32}", value) is None
            ):
                raise ValueError("禁言时长请放在所有 @成员 之后，例如 /禁言 @甲 @乙 10m。")
        if value and value not in values:
            values.append(value)
        remaining = _SEPARATORS.sub("", remaining[match.end() :])
    return values, has_markup


def _single_target(body: str, mentions: MentionContext) -> str:
    """Resolve only the first argument; preserve request IDs and rejection text."""
    body = body.lstrip()
    if match := MENTION_MARKUP.match(body):
        member_id = mentions.resolve(match[1] or match[2])
        if not member_id:
            raise ValueError("请选择要操作的成员，不能将机器人作为目标。")
        return member_id + body[match.end() :]
    match = _TOKEN.match(body)
    if match and any(character in match[0] for character in "@<>"):
        raise ValueError(_TARGET_ERROR)
    return body


def _mute_parts(body: str, mentions: MentionContext) -> tuple[str, str]:
    last = re.search(r"(?:^|\s)(\S+)\s*$", body)
    if last is None:
        return body, ""
    # The final whitespace-separated argument is a duration, including a typo
    # that the manager must reject. A complete mention (or the final fragment
    # of QQ's XML tag) remains a target. Extra explicit IDs use a comma list.
    if any(match.end() > last.start(1) for match in MENTION_MARKUP.finditer(body)):
        return body, ""
    value = last[1]
    if any(character in value for character in "@<>"):
        return body, ""
    if "," in value or "，" in value:
        return body, ""
    before = body[: last.start()]
    numeric_openid = re.fullmatch(r"[0-9]{32}", value) is not None
    known_member = value in mentions.ids or value in mentions.aliases
    if before.strip() or (
        not numeric_openid and (_DURATION.fullmatch(value) or (mentions.ids and not known_member))
    ):
        return before, value
    return body, ""


def normalize_command(event: Any) -> str:
    """Return the manager command, preserving unrelated command payloads.

    Native tags and At components are actual mentions. A manually typed
    ``@nickname`` is not enough to identify a group member. Batch member
    commands combine actual mentions with explicit OpenIDs; single-member
    approval commands retain their request/reason argument boundaries.
    """
    mentions = mention_context(event)
    command, body = _command_parts(str(event.get_message_str() or ""), mentions)
    if command not in MEMBER_COMMANDS:
        return command + (" " + body if body else "")
    body = _member_source(event, command, body, mentions)
    if command in {"同意", "拒绝"}:
        body = _single_target(body, mentions)
        return command + (" " + body if body else "")

    targets, suffix = body, ""
    if command in {"禁言", "更新禁言"}:
        targets, suffix = _mute_parts(body, mentions)
    elif command == "移出" and re.search(r"(?:^|\s)拉黑\s*$", body):
        targets, suffix = re.sub(r"(?:^|\s)拉黑\s*$", "", body), "拉黑"

    values, has_markup = _member_tokens(
        targets, mentions, mute_targets=command in {"禁言", "更新禁言"}
    )
    if not has_markup and not mentions.ids:
        # Keep the existing explicit-OpenID grammar and its validation intact.
        return command + (" " + body if body else "")
    if not has_markup:
        if not targets.strip():
            values = list(mentions.ids)
        else:
            # If the source gives positions, they define the entire target
            # list. Never append other metadata mentions to that list. Without
            # positions, only an empty target slot or matching identities are
            # unambiguous enough to use the metadata fallback.
            if any(value not in mentions.ids and value not in mentions.aliases for value in values):
                raise ValueError("无法确定 @成员对应的目标，请重新选择成员发送完整命令。")
            values = list(dict.fromkeys(mentions.resolve(value) for value in values))
    if not values:
        raise ValueError("请直接 @要操作的成员，或填写 /成员 查询到的成员 OpenID。")
    normalized = ",".join(values)
    if suffix:
        normalized += " " + suffix
    return command + " " + normalized
