"""Documented QQ group-management API contracts, independent of AstrBot.

Sources: https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/
Verified against the official documentation on 2026-11-11.  In particular,
``restrict_chat_setting`` only documents *member* mutations; its global mute
configuration is read-only through the public API.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote


class QQAPIError(RuntimeError):
    """An API failure whose display text never includes an upstream credential."""

    def __init__(
        self,
        status: int,
        code: int | str | None,
        message: str,
        trace_id: str = "",
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.trace_id = trace_id
        # Raw API messages can echo request data; do not put them in exceptions
        # that third-party loggers may print without redaction.
        super().__init__(self.user_message())

    def user_message(self) -> str:
        code = str(self.code or "")
        reasons = {
            "AUTH_UNAVAILABLE": "QQ 官方机器人尚未登录或令牌刷新失败，请检查连接状态。",
            "TRANSPORT_CLOSED": "插件正在关闭，请稍后重试。",
            "TIMEOUT": "QQ 接口连接超时，操作结果尚未确认，请先查询状态后再决定是否重试。",
            "NETWORK_ERROR": "QQ 接口连接失败，操作结果尚未确认，请先查询状态后再决定是否重试。",
            "REPLY_EXPIRED": "被动回复已超过 5 分钟，请重新发起操作。",
            "INVALID_RESPONSE": "QQ 接口返回异常，操作结果尚未确认，请先查询状态。",
            "11253": "该机器人尚未获准调用此接口，请向 QQ 开放平台申请白名单或内邀权限。",
            "11254": "该机器人的接口权限已被平台封禁。",
            "11282": "机器人缺少所需管理员权限，请在 QQ 群内授予机器人管理员身份。",
            "11243": "机器人访问凭证无效，请检查 QQ 官方机器人配置。",
            "304036": "该机器人没有 Markdown 模板权限，请检查模板授权或使用纯文本模式。",
            "304037": "该机器人没有消息按钮权限，请检查开放平台授权或使用纯文本模式。",
            "304103": "用于回复的消息已过期，请重新发送群管命令。",
            "40034005": "用于回复的消息已过期，请重新发送群管命令。",
            "40034024": "回复消息 ID 无效或不属于当前机器人。",
            "40034025": "用于回复的事件 ID 无效，请重新打开群管面板。",
            "40034026": "用于回复的事件已过期，请重新打开群管面板。",
            "40034027": "该事件不支持直接回复消息，请在群内发送群管命令。",
            "40034029": "按钮行数或列数超过 QQ 平台限制。",
            "40034100": "主动消息超过平台频率或配额限制，请稍后再试。",
            "40034105": "当前群未允许机器人发送主动消息。",
            "40034127": "该机器人没有 Markdown 模板权限。",
            "40034128": "被动回复的时间或次数已达上限，请重新发送群管命令。",
            "40054002": "机器人已被禁言，暂时无法发送消息。",
            "40054003": "机器人不在此 QQ 群中。",
            "40054005": "平台已去重此消息，请重新发送群管命令。",
        }
        if code in reasons:
            reason = reasons[code]
        elif self.status == 429:
            reason = "接口请求过于频繁，请稍后再试。"
        elif self.status == 401:
            reason = "机器人访问凭证已失效，请检查 QQ 官方机器人连接状态。"
        elif self.status == 403:
            reason = "平台拒绝访问，请检查机器人接口授权及群管理员权限。"
        elif self.status == 404:
            reason = "接口或目标不存在，请检查目标 ID 及平台功能开放情况。"
        elif self.status >= 500:
            reason = "QQ 平台暂时无法处理请求，请稍后再试。"
        elif self.status == 0:
            reason = "无法连接 QQ 平台，请检查网络后重试。"
        else:
            reason = "QQ 平台未完成该操作，请检查参数及机器人权限。"
        details = []
        if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code):
            details.append(f"错误码 {code}")
        if self.trace_id and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.trace_id):
            details.append(f"TraceID {self.trace_id}")
        return reason + ("（" + "；".join(details) + "）" if details else "")


class QQTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None: ...


def _text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name}必须是{'可为空的' if empty else '非空'}字符串。")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name}不能包含控制字符。")
    return value


def _segment(value: Any, name: str) -> str:
    value = _text(value, name)
    if value in {".", ".."}:
        raise ValueError(f"{name}无效。")
    return quote(value, safe="")


def _choice(value: Any, options: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in options:
        raise ValueError(f"{name}必须为 {' / '.join(sorted(options))}。")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name}必须是布尔值。")
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name}必须是 {minimum} 到 {maximum} 之间的整数。")
    return value


def _items(values: Any, name: str, maximum: int) -> list[Any]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{name}必须是列表。")
    if not 1 <= len(values) <= maximum:
        raise ValueError(f"{name}每次必须包含 1 到 {maximum} 项。")
    return list(values)


def _ids(values: Any, name: str, maximum: int) -> list[str]:
    result = [_text(value, name) for value in _items(values, name, maximum)]
    if len(result) != len(set(result)):
        raise ValueError(f"{name}不能包含重复项。")
    return result


def _timestamp(value: Any, name: str) -> datetime:
    value = _text(value, name)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise ValueError(f"{name}必须是带时区的 RFC3339 时间，例如 2026-11-11T18:00:00+08:00。")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name}不是有效时间。") from exc


def _group_targets(group_openids: Any, group_ids: Any) -> dict[str, Any]:
    if (group_openids is None) == (group_ids is None):
        raise ValueError("group_openids 与 group_ids 必须且只能提供一个。")
    if group_openids is not None:
        return {"group_openids": _ids(group_openids, "group_openids", 100)}
    numbers = [
        _integer(value, "group_ids", 1, 2**64 - 1) for value in _items(group_ids, "group_ids", 100)
    ]
    if len(numbers) != len(set(numbers)):
        raise ValueError("group_ids不能包含重复项。")
    return {"group_ids": numbers}


def _remark(value: Any) -> str:
    value = _text(value, "remark", empty=True)
    if len(value) > 255:
        raise ValueError("策略备注最多 255 个字符。")
    return value


class QQGroupAPI:
    """The 17 documented group-management endpoints plus group messaging.

    Authentication, bounded retries and HTTP error decoding belong to the
    injected transport.  The caller must authorize every operation, including
    button callbacks; platform button permissions alone are not authorization.
    """

    def __init__(self, transport: QQTransport) -> None:
        self.transport = transport

    @staticmethod
    def _group(group_openid: str) -> str:
        return "/v2/groups/" + _segment(group_openid, "group_openid")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self.transport.request(method, path, json=json, params=params)
        return response if response is not None else {}

    async def get_group_info(self, group_openid: str) -> dict[str, Any]:
        return await self._request("GET", self._group(group_openid) + "/info")

    async def get_bot_state(self, group_openid: str) -> dict[str, Any]:
        return await self._request("GET", self._group(group_openid) + "/bot_state")

    async def list_members(self, group_openid: str, *, cursor: str = "") -> dict[str, Any]:
        return await self._request(
            "GET",
            self._group(group_openid) + "/members",
            params={"cursor": _text(cursor, "cursor", empty=True)},
        )

    async def get_member(self, group_openid: str, member_openid: str) -> dict[str, Any]:
        path = self._group(group_openid) + "/members/" + _segment(member_openid, "member_openid")
        return await self._request("GET", path)

    async def remove_members(
        self, group_openid: str, member_openids: Sequence[str], *, add_to_blacklist: bool = False
    ) -> dict[str, Any]:
        payload = {
            "member_openids": _ids(member_openids, "member_openids", 20),
            "add_to_member_blacklist": _bool(add_to_blacklist, "add_to_blacklist"),
        }
        return await self._request(
            "POST", self._group(group_openid) + "/batch_remove_members", json=payload
        )

    async def list_blacklist(
        self, group_openid: str, *, cursor: str = "", limit: int = 20
    ) -> dict[str, Any]:
        params = {
            "cursor": _text(cursor, "cursor", empty=True),
            "limit": _integer(limit, "limit", 1, 100),
        }
        return await self._request(
            "GET", self._group(group_openid) + "/member_blacklist", params=params
        )

    async def update_blacklist(
        self, group_openid: str, op: str, member_openids: Sequence[str]
    ) -> dict[str, Any]:
        payload = {
            "op": _choice(op, {"add", "del"}, "op"),
            "member_openids": _ids(member_openids, "member_openids", 20),
        }
        return await self._request(
            "POST", self._group(group_openid) + "/member_blacklist", json=payload
        )

    async def get_mute_state(self, group_openid: str) -> dict[str, Any]:
        return await self._request("GET", self._group(group_openid) + "/restrict_chat_setting")

    async def set_member_mutes(
        self, group_openid: str, members: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        normalized = []
        seen = set()
        now = datetime.now(UTC)
        for member in _items(members, "members", 20):
            if not isinstance(member, Mapping) or set(member) - {
                "op",
                "member_openid",
                "mute_expire_at",
            }:
                raise ValueError("禁言项只支持 op、member_openid、mute_expire_at。")
            op = _choice(member.get("op"), {"add", "update", "del"}, "op")
            member_openid = _text(member.get("member_openid"), "member_openid")
            if member_openid in seen:
                raise ValueError("同一次禁言操作不能重复指定成员。")
            seen.add(member_openid)
            item = {"op": op, "member_openid": member_openid}
            expires = member.get("mute_expire_at", "")
            if op == "del":
                if expires != "":
                    raise ValueError("解除禁言时 mute_expire_at 必须为空或省略。")
                item["mute_expire_at"] = ""
            else:
                duration = _timestamp(expires, "mute_expire_at") - now
                if duration <= timedelta(0) or duration > timedelta(days=30):
                    raise ValueError("禁言到期时间必须晚于当前时间且不能超过 30 天。")
                item["mute_expire_at"] = expires
            normalized.append(item)
        return await self._request(
            "POST",
            self._group(group_openid) + "/restrict_chat_setting",
            json={"members": normalized},
        )

    async def list_join_requests(
        self, group_openid: str, *, cursor: str = "", limit: int = 20
    ) -> dict[str, Any]:
        params = {
            "cursor": _text(cursor, "cursor", empty=True),
            "limit": _integer(limit, "limit", 1, 50),
        }
        return await self._request(
            "GET", self._group(group_openid) + "/join_request_list", params=params
        )

    async def approve_join_request(
        self,
        group_openid: str,
        member_openid: str,
        op: str,
        *,
        join_request_id: str,
        reject_reason: str = "",
        add_to_blacklist: bool = False,
    ) -> dict[str, Any]:
        op = _choice(op, {"approve", "decline"}, "op")
        _text(reject_reason, "reject_reason", empty=True)
        _bool(add_to_blacklist, "add_to_blacklist")
        if op == "approve" and (reject_reason or add_to_blacklist):
            raise ValueError("通过申请时不能指定拒绝理由或加入黑名单。")
        payload: dict[str, Any] = {
            "op": op,
            "join_request_id": _text(join_request_id, "join_request_id"),
        }
        if op == "decline":
            payload.update(reject_reason=reject_reason, add_to_member_blacklist=add_to_blacklist)
        path = (
            self._group(group_openid)
            + "/approval_join_request/"
            + _segment(member_openid, "member_openid")
        )
        return await self._request("POST", path, json=payload)

    async def list_strategies(self, *, cursor: str = "", limit: int = 20) -> dict[str, Any]:
        params = {
            "cursor": _text(cursor, "cursor", empty=True),
            "limit": _integer(limit, "limit", 1, 50),
        }
        return await self._request("GET", "/v2/groups/join_approval_strategy", params=params)

    async def create_strategy(
        self,
        *,
        group_openids: Sequence[str] | None = None,
        group_ids: Sequence[int] | None = None,
        is_enable: str = "on",
        expire_at: str | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        payload = _group_targets(group_openids, group_ids)
        payload["is_enable"] = _choice(is_enable, {"on", "off"}, "is_enable")
        if expire_at is not None:
            _timestamp(expire_at, "expire_at")
            payload["expire_at"] = expire_at
        if remark is not None:
            payload["remark"] = _remark(remark)
        return await self._request("POST", "/v2/groups/join_approval_strategy", json=payload)

    async def update_strategy(
        self,
        strategy_id: str,
        *,
        is_enable: str | None = None,
        expire_at: str | None = None,
        group_action: Mapping[str, Any] | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if is_enable is not None:
            payload["is_enable"] = _choice(is_enable, {"on", "off"}, "is_enable")
        if expire_at is not None:
            _timestamp(expire_at, "expire_at")
            payload["expire_at"] = expire_at
        if remark is not None:
            payload["remark"] = _remark(remark)
        if group_action is not None:
            if not isinstance(group_action, Mapping) or set(group_action) - {
                "op",
                "group_openids",
                "group_ids",
            }:
                raise ValueError("group_action 只支持 op、group_openids、group_ids。")
            payload["group_action"] = {
                "op": _choice(group_action.get("op"), {"add", "del"}, "group_action.op"),
                **_group_targets(group_action.get("group_openids"), group_action.get("group_ids")),
            }
        if not payload:
            raise ValueError("至少提供一项策略修改内容。")
        path = "/v2/groups/join_approval_strategy/" + _segment(strategy_id, "strategy_id")
        return await self._request("PATCH", path, json=payload)

    async def delete_strategy(self, strategy_id: str) -> dict[str, Any]:
        path = "/v2/groups/join_approval_strategy/" + _segment(strategy_id, "strategy_id")
        return await self._request("DELETE", path)

    async def execute_strategy(self, strategy_id: str) -> dict[str, Any]:
        path = (
            "/v2/groups/join_approval_strategy/" + _segment(strategy_id, "strategy_id") + "/execute"
        )
        return await self._request("POST", path, json={})

    async def update_strategy_whitelist(
        self, strategy_id: str, op: str, whitelist_users: Sequence[str]
    ) -> dict[str, Any]:
        users = _ids(whitelist_users, "whitelist_users", 10000)
        if any(
            not re.fullmatch(r"[1-9][0-9]{0,19}", user) or int(user) > 2**64 - 1 for user in users
        ):
            raise ValueError("白名单 QQ 号码必须是非零、无前导零的 uint64 数字字符串。")
        payload = {"op": _choice(op, {"add", "del"}, "op"), "whitelist_users": users}
        path = (
            "/v2/groups/join_approval_strategy/"
            + _segment(strategy_id, "strategy_id")
            + "/whitelist_users"
        )
        return await self._request("POST", path, json=payload)

    async def send_message(self, group_openid: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Send documented group payloads, including native Markdown/keyboards.

        ``<qqbot-at-everyone />`` in text or Markdown mentions all members.
        QQ enforces administrator status and one mention per group per day.
        Button rendering and native Markdown/template authorization are handled
        by the caller; nested objects are preserved without introducing fields.
        """
        allowed = {
            "msg_type",
            "content",
            "markdown",
            "keyboard",
            "msg_id",
            "event_id",
            "msg_seq",
            "media",
            "message_reference",
            "card",
        }
        if not isinstance(payload, Mapping) or set(payload) - allowed:
            raise ValueError("消息含有当前群消息接口未公开的字段。")
        message = deepcopy(dict(payload))
        kind = message.get("msg_type", 0)
        if isinstance(kind, bool) or not isinstance(kind, int) or kind not in {0, 2, 7, 8}:
            raise ValueError("群消息 msg_type 必须为 0、2、7 或 8。")
        if message.get("msg_id") and message.get("event_id"):
            raise ValueError("msg_id 与 event_id 只能提供一个。")
        for identifier in ("msg_id", "event_id"):
            if identifier in message:
                _text(message[identifier], identifier)
        if "msg_seq" in message:
            _integer(message["msg_seq"], "msg_seq", 1, 2**31 - 1)
        if "content" in message and not isinstance(message["content"], str):
            raise ValueError("消息 content 必须是字符串。")
        if kind == 0 and not message.get("content", "").strip():
            raise ValueError("文本消息 content 不能为空。")
        for field, expected in (("markdown", 2), ("media", 7), ("card", 8)):
            if kind == expected and (
                not isinstance(message.get(field), Mapping) or not message[field]
            ):
                raise ValueError(f"消息类型 {kind} 必须提供 {field} 对象。")
            if field in message and kind != expected:
                raise ValueError(f"{field} 与 msg_type 不匹配。")
        if kind == 2 and message.get("content"):
            raise ValueError("Markdown 消息不能同时提供非空 content。")
        if "keyboard" in message:
            keyboard = message["keyboard"]
            if not isinstance(keyboard, Mapping) or bool(keyboard.get("id")) == bool(
                keyboard.get("content")
            ):
                raise ValueError("keyboard 必须且只能提供 id 或 content。")
        return await self._request("POST", self._group(group_openid) + "/messages", json=message)

    async def delete_message(self, group_openid: str, message_id: str) -> dict[str, Any]:
        """Recall within two minutes; QQ checks sender and administrator rights."""
        path = self._group(group_openid) + "/messages/" + _segment(message_id, "message_id")
        return await self._request("DELETE", path)
