"""Bounded request diagnostics and execution state, without request contents."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class RequestState:
    mutation_started: bool = False


# A new state is installed for each command. Transports are shared across
# commands, so request progress must never be stored on a transport instance.
current_request_state: ContextVar[RequestState | None] = ContextVar(
    "qq_admin_request_state", default=None
)

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
ROUTE_TEMPLATES = frozenset(
    {
        "/v2/groups/{group_openid}/info",
        "/v2/groups/{group_openid}/bot_state",
        "/v2/groups/{group_openid}/members",
        "/v2/groups/{group_openid}/members/{member_openid}",
        "/v2/groups/{group_openid}/batch_remove_members",
        "/v2/groups/{group_openid}/member_blacklist",
        "/v2/groups/{group_openid}/restrict_chat_setting",
        "/v2/groups/{group_openid}/join_request_list",
        "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
        "/v2/groups/join_approval_strategy",
        "/v2/groups/join_approval_strategy/{strategy_id}",
        "/v2/groups/join_approval_strategy/{strategy_id}/execute",
        "/v2/groups/join_approval_strategy/{strategy_id}/whitelist_users",
        "/v2/groups/{group_openid}/messages",
        "/v2/groups/{group_openid}/messages/{message_id}",
    }
)
_ROUTE_PATTERNS = tuple(
    (
        re.compile(
            "/".join(
                r"[^/?#\\\r\n]+" if part.startswith("{") else re.escape(part)
                for part in template.split("/")
            )
        ),
        template,
    )
    for template in sorted(ROUTE_TEMPLATES)
)
DIAGNOSTIC_REASONS = frozenset(
    {
        "json_decode",
        "text_decode",
        "root_type",
        "field_type",
        "field_value",
        "pagination",
        "verification",
    }
)
DIAGNOSTIC_FIELDS = frozenset(
    {
        "err_code",
        "code",
        "members",
        "members[].member_openid",
        "next_cursor",
        "member_openid",
        "member_role",
        "list",
        "join_request_id",
        "users",
        "strategies",
        "strategy_id",
        "group_openids",
        "global_rule",
        "remove_members_result",
        "add_to_member_blacklist_fail_openids",
        "fail_openids",
    }
)
DIAGNOSTIC_VALUES = frozenset(
    {
        "list",
        "object",
        "string",
        "nonempty_string",
        "boolean",
        "integer",
        "integer_or_string_or_null",
        "nonnegative_integer",
        "missing",
        "null",
        "number",
        "array",
        "match",
        "mismatch",
        "complete",
        "cursor_loop",
        "page_limit",
    }
)
RESPONSE_SHAPE_FIELDS = (
    "members",
    "next_cursor",
    "data",
    "list",
    "users",
    "strategies",
    "code",
    "err_code",
    "retcode",
    "ret",
    "status",
    "message",
    "trace_id",
    "total",
    "count",
    "member_openid",
    "member_role",
    "bot",
    "group_openids",
    "strategy_id",
    "is_enable",
    "expire_at",
    "global_rule",
    "remove_members_result",
    "add_to_member_blacklist_fail_openids",
    "fail_openids",
)
MAX_RESPONSE_SHAPE_LENGTH = 700


def response_shape_parts(response: Mapping[str, Any] | None) -> tuple[str, ...] | None:
    """Snapshot only fixed field/type labels, never retain response values.

    A string cursor is classified as empty or nonempty without retaining it.
    Only the root data object is entered; nested business data is never read.
    """
    if not isinstance(response, Mapping):
        return None
    parts: list[str] = []

    def append_fields(value: Mapping[str, Any], prefix: str = "") -> None:
        known = 0
        for key in RESPONSE_SHAPE_FIELDS:
            if key not in value:
                continue
            known += 1
            item = value[key]
            kind = "object" if isinstance(item, Mapping) else json_type(item) or "unknown"
            if key == "next_cursor" and isinstance(item, str):
                kind = f"string({'nonempty' if item else 'empty'})"
            elif key == "data" and isinstance(item, Mapping) and not item:
                kind = "object{}"
            parts.append(f"{prefix}{key}:{kind}")
        unknown = max(0, len(value) - known)
        if unknown:
            parts.append(f"{prefix}unknown_keys:{unknown}")

    append_fields(response)
    data = response.get("data")
    if isinstance(data, Mapping):
        append_fields(data, "data.")
    return tuple(parts)


def format_response_shape(parts: tuple[str, ...] | None) -> str:
    """Render normalized parts, truncating only between complete fields."""
    if parts is None:
        return ""
    included: list[str] = []
    size = len("object{}")
    for index, part in enumerate(parts):
        extra = len(part) + bool(included)
        reserve = len(",...") if index + 1 < len(parts) else 0
        if size + extra + reserve > MAX_RESPONSE_SHAPE_LENGTH:
            included.append("...")
            break
        included.append(part)
        size += extra
    return "object{" + ",".join(included) + "}"


def allowed(value: Any, options: frozenset[str], *, default: str = "") -> str:
    """Accept a fixed diagnostic label, never a field value or arbitrary key."""
    return value if isinstance(value, str) and value in options else default


def safe_status(value: Any) -> int:
    return value if type(value) is int and 100 <= value <= 599 else 0


def safe_trace_id(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else ""


def safe_route(value: Any) -> str:
    if value == "":
        return ""
    return allowed(value, ROUTE_TEMPLATES, default="unknown")


def route_template(path: str) -> str:
    """Map only documented routes; never return a literal resource identifier."""
    if not isinstance(path, str):
        return "unknown"
    for pattern, template in _ROUTE_PATTERNS:
        if pattern.fullmatch(path):
            return template
    return "unknown"


def json_type(value: Any) -> str:
    """Describe a parsed JSON value without exposing its contents."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return ""
