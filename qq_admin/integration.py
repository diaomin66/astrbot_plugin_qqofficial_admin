"""AstrBot integration and authenticated transport for QQ's current API host.

The adapter owns credentials and refreshes its botpy token. This module borrows
that token without changing botpy globals or the adapter's HTTP session.
"""

from __future__ import annotations

import asyncio
import json as json_module
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from .api import QQAPIError

API_BASE = "https://api.bot.qq.com"
QQ_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})


@dataclass(frozen=True)
class EventContext:
    platform_id: str
    group_id: str
    user_id: str
    is_superuser: bool
    message_id: str = ""
    event_id: str = ""


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def extract_event_context(event: Any) -> EventContext | None:
    """Accept actual QQ group events, excluding guild channels and private chat.

    Both guild messages and QQ group messages have AstrBot's GROUP_MESSAGE type;
    the raw group OpenID is therefore required instead of relying on that type.
    """
    if event.get_platform_name() not in QQ_PLATFORMS:
        return None
    raw = _field(event.message_obj, "raw_message")
    group_id = str(_field(raw, "group_openid", "") or "")
    scene = _field(raw, "scene")
    if not group_id or scene not in (None, "group"):
        return None
    platform_id = str(event.get_platform_id() or "")
    is_interaction = scene == "group"
    user_id = str(
        (
            _field(raw, "group_member_openid")
            if is_interaction
            else _field(_field(raw, "author"), "member_openid")
        )
        or ""
    )
    if (
        not platform_id
        or not user_id
        or user_id != str(event.get_sender_id() or "")
        or group_id != str(event.get_group_id() or "")
    ):
        return None

    # A button's interaction ID is not a message ID. The event ID is the outer
    # INTERACTION_CREATE envelope ID, retained as Interaction.event_id by botpy.
    message_id = "" if is_interaction else str(_field(raw, "id", "") or "")
    event_id = str(_field(raw, "event_id", "") or "") if is_interaction else ""
    return EventContext(
        platform_id=platform_id,
        group_id=group_id,
        user_id=user_id,
        is_superuser=bool(event.is_admin()),
        message_id=message_id,
        event_id=event_id,
    )


def encode_callback(
    action_id: str,
    data: Any = None,
    *,
    expires_at: float | None = None,
) -> str:
    """Encode native callback data for ``@filter.button_action(action_id)``.

    The handler receives the original JSON through
    ``event.get_button_interaction().data``. AstrBot acknowledges the QQ
    interaction and validates callback expiry before dispatching the handler.
    """
    from astrbot.core.platform.button_interaction import encode_button_callback

    return encode_button_callback(action_id, data, expires_at=expires_at)


class BotpyTransport:
    """Use an adapter's refreshing token with an isolated HTTPS session.

    Writes are never retried: a network timeout does not prove the operation was
    rejected. The caller must report an uncertain result instead of duplicating
    moderation or message operations.
    """

    def __init__(self, bot: Any, timeout: float = 20) -> None:
        self.bot = bot
        self.timeout = float(timeout)
        if not 0 < self.timeout <= 120:
            raise ValueError("HTTP timeout must be between 0 and 120 seconds")
        self._session: aiohttp.ClientSession | None = None
        self._token_lock = asyncio.Lock()
        self._message_lock = asyncio.Lock()
        self._sequences: OrderedDict[tuple[str, str], tuple[int, float]] = OrderedDict()
        self._closed = False

    async def _headers(self) -> dict[str, str]:
        api = getattr(self.bot, "api", None)
        http = getattr(api, "_http", None) or getattr(self.bot, "http", None)
        token = getattr(http, "_token", None)
        if token is None:
            raise QQAPIError(0, "AUTH_UNAVAILABLE", "QQ 官方机器人尚未完成登录")
        try:
            async with self._token_lock:
                await token.check_token()
                authorization = token.get_string()
                app_id = str(token.app_id or "")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise QQAPIError(0, "AUTH_UNAVAILABLE", "QQ 官方机器人令牌刷新失败") from None
        if not isinstance(authorization, str) or not authorization.strip() or not app_id:
            raise QQAPIError(0, "AUTH_UNAVAILABLE", "QQ 官方机器人令牌不可用")
        return {"Authorization": authorization, "X-Union-Appid": app_id}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported QQ API request method")
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(character in path for character in ("?", "#", "\\", "\r", "\n"))
        ):
            raise ValueError("QQ API path must be an absolute path without a host or query")
        if self._closed:
            raise QQAPIError(0, "TRANSPORT_CLOSED", "插件正在关闭，请稍后重试")
        headers = await self._headers()
        if self._closed:
            raise QQAPIError(0, "TRANSPORT_CLOSED", "插件正在关闭，请稍后重试")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=False,
            )
        kwargs: dict[str, Any] = {"headers": headers, "allow_redirects": False}
        if json is not None:
            kwargs["json"] = dict(json)
        if params is not None:
            kwargs["params"] = dict(params)
        try:
            async with self._session.request(method, API_BASE + path, **kwargs) as response:
                text = await response.text()
                trace_id = response.headers.get("X-Tps-Trace-Id", "")
                try:
                    result = json_module.loads(text) if text.strip() else None
                except (ValueError, TypeError):
                    result = None
                    if 200 <= response.status < 300:
                        raise QQAPIError(
                            response.status,
                            "INVALID_RESPONSE",
                            "QQ 接口返回了无法解析的响应",
                            trace_id=trace_id,
                        ) from None
                code = (
                    result.get("err_code", result.get("code", 0)) if isinstance(result, dict) else 0
                )
                if not 200 <= response.status < 300 or code not in (0, "0", None):
                    error = result if isinstance(result, dict) else {}
                    message = str(error.get("err_msg") or error.get("message") or "QQ 接口请求失败")
                    # Never reflect a credential even if an upstream error
                    # unexpectedly contains the request's Authorization value.
                    for secret in (headers["Authorization"], headers["Authorization"].split()[-1]):
                        message = message.replace(secret, "[redacted]")
                    raise QQAPIError(
                        response.status,
                        error.get("err_code", error.get("code", response.status)),
                        message[:500],
                        trace_id=trace_id,
                    )
                if result is not None and not isinstance(result, dict):
                    raise QQAPIError(
                        response.status,
                        "INVALID_RESPONSE",
                        "QQ 接口返回的数据结构不符合预期",
                        trace_id=trace_id,
                    )
                return result
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise QQAPIError(
                0, "TIMEOUT", "QQ 接口请求超时；操作结果尚未确认，请先查询状态"
            ) from None
        except aiohttp.ClientError:
            raise QQAPIError(
                0, "NETWORK_ERROR", "QQ 接口连接失败；操作结果尚未确认，请先查询状态"
            ) from None

    async def next_message_sequence(self, group_id: str, reply_id: str) -> int:
        """Keep msg_seq unique for replies sharing the same msg_id/event_id."""
        async with self._message_lock:
            now = time.monotonic()
            key = (group_id, reply_id)
            previous = self._sequences.pop(key, None)
            if previous is None or now - previous[1] > 300:
                # AstrBot 4.29.4 chooses 1..10000 for its own outgoing messages.
                # A larger random starting point avoids that range and makes
                # plugin reloads unlikely to reuse a recent reply's sequence.
                value = secrets.randbelow(1 << 29) + 10000
            else:
                value = previous[0]
            sequence = value + 1
            self._sequences[key] = (sequence, now)
            while len(self._sequences) > 2048:
                self._sequences.popitem(last=False)
            return sequence

    async def close(self) -> None:
        self._closed = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._sequences.clear()


async def send_payload(
    event: Any,
    payload: Mapping[str, Any],
    transport: BotpyTransport,
) -> dict[str, Any] | None:
    """Send an official group message with the right passive-reply identifier.

    Callers supply ``markdown``/``keyboard``/``content`` etc.; routing and reply
    identifiers are always taken from the actual event rather than callback data.
    """
    context = extract_event_context(event)
    if context is None:
        raise ValueError("Only QQ official group events can receive this message")
    if (context.message_id or context.event_id) and reply_age_seconds(event) >= 300:
        raise QQAPIError(0, "REPLY_EXPIRED", "被动回复已超过 5 分钟，请重新发起操作")
    outgoing = dict(payload)
    outgoing.pop("msg_id", None)
    outgoing.pop("event_id", None)
    if context.event_id:
        outgoing["event_id"] = context.event_id
    elif context.message_id:
        outgoing["msg_id"] = context.message_id
    reply_id = context.event_id or context.message_id
    outgoing["msg_seq"] = await transport.next_message_sequence(context.group_id, reply_id)
    group_id = quote(context.group_id, safe="")
    return await transport.request("POST", f"/v2/groups/{group_id}/messages", json=outgoing)


def reply_age_seconds(event: Any) -> float:
    raw = _field(event.message_obj, "raw_message")
    candidates = (
        _field(raw, "timestamp"),
        _field(event.message_obj, "timestamp"),
    )
    for value in candidates:
        try:
            if isinstance(value, (int, float)):
                timestamp = float(value)
            elif isinstance(value, datetime):
                timestamp = value.replace(tzinfo=value.tzinfo or UTC).timestamp()
            elif isinstance(value, str) and value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                timestamp = parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
            else:
                continue
            return max(0.0, time.time() - timestamp)
        except (ValueError, OverflowError, OSError):
            continue
    # AstrBot 4.29.4 always timestamps normalized events. This fallback permits
    # compatible event wrappers that do not preserve source timestamps.
    return 0.0


def extract_mention_ids(event: Any) -> list[str]:
    """Read QQ user mentions from parsed components and the raw QQ markup."""
    mentions: list[str] = []
    self_id = str(event.get_self_id() or "")
    for component in event.get_messages():
        # Avoid importing AstrBot just to inspect components in pure unit tests.
        if type(component).__name__ == "At":
            user_id = str(_field(component, "qq", "") or "")
            if user_id and user_id not in {self_id, "all"} and user_id not in mentions:
                mentions.append(user_id)
    raw = _field(event.message_obj, "raw_message")
    for mention in _field(raw, "mentions", None) or []:
        if _field(mention, "is_you", False) or _field(mention, "bot", False):
            continue
        user_id = str(_field(mention, "member_openid") or _field(mention, "id") or "")
        if user_id and user_id not in {self_id, "all"} and user_id not in mentions:
            mentions.append(user_id)
    content = str(_field(raw, "content", "") or "")
    raw_ids = re.findall(r'<qqbot-at-user\s+id=["\']([^"\']+)["\']\s*/?>', content)
    raw_ids.extend(re.findall(r"<@!?([^>\s]+)>", content))
    for user_id in raw_ids:
        if user_id not in {self_id, "all"} and user_id not in mentions:
            mentions.append(user_id)
    return mentions
