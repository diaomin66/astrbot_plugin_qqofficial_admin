"""Install QQ's native group command panel without changing unrelated panels."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from .api import QQAPIError, QQGroupAPI
from .menu import group_command_items

_MISSING = object()
_PANELS_ROUTE = "/v2/panels"
_PANEL_ROUTE = "/v2/panels/{panel_id}"


def _value_type(value: Any) -> str:
    """Describe JSON structure without reflecting IDs, cursors or other values."""
    if value is _MISSING:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


class OfficialMenu:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _marker(group_id: str) -> str:
        digest = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:24]
        return "astrbot_qqadmin:group:" + digest

    def _lock(self, ctx: Any, api: QQGroupAPI) -> asyncio.Lock:
        application_id = getattr(getattr(api, "transport", None), "application_id", "")
        key = "app:" + application_id if application_id else "platform:" + ctx.platform_id
        return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _invalid(
        response: dict,
        *,
        method: str,
        route: str,
        reason: str,
        field: str,
        expected: str,
        actual: str,
    ) -> QQAPIError:
        return QQAPIError(
            getattr(response, "status", 0),
            "INVALID_RESPONSE",
            "官方指令面板响应无法验证。",
            trace_id=getattr(response, "trace_id", ""),
            method=getattr(response, "method", "") or method,
            route=getattr(response, "route", "") or route,
            reason=reason,
            field=field,
            expected=expected,
            actual=actual,
            request_sent=True,
        )

    def _validate_detail(self, ctx: Any, detail: dict, panel_id: str) -> None:
        """Require exact ownership and a single current-group target."""
        for field, expected_value in (
            ("panel_id", panel_id),
            ("scope", "group"),
            ("target_type", "specific"),
        ):
            value = detail.get(field, _MISSING)
            if not isinstance(value, str) or value != expected_value:
                raise self._invalid(
                    detail,
                    method="GET",
                    route=_PANEL_ROUTE,
                    reason="field_value" if isinstance(value, str) else "field_type",
                    field=field,
                    expected="nonempty_string" if field == "panel_id" else "string",
                    actual="mismatch" if isinstance(value, str) else _value_type(value),
                )
        panel = detail.get("panel", _MISSING)
        if not isinstance(panel, dict):
            raise self._invalid(
                detail,
                method="GET",
                route=_PANEL_ROUTE,
                reason="field_type",
                field="panel",
                expected="object",
                actual=_value_type(panel),
            )
        remark = panel.get("remark", _MISSING)
        if remark != self._marker(ctx.group_id):
            raise self._invalid(
                detail,
                method="GET",
                route=_PANEL_ROUTE,
                reason="field_value" if isinstance(remark, str) else "field_type",
                field="panel.remark",
                expected="string",
                actual="mismatch" if isinstance(remark, str) else _value_type(remark),
            )
        groups = detail.get("group_openids", _MISSING)
        if groups != [ctx.group_id]:
            raise self._invalid(
                detail,
                method="GET",
                route=_PANEL_ROUTE,
                reason="verification" if isinstance(groups, list) else "field_type",
                field="group_openids",
                expected="list",
                actual="mismatch" if isinstance(groups, list) else _value_type(groups),
            )

    def _verify_items(self, detail: dict, expected_panel: dict) -> None:
        items = detail["panel"].get("items", _MISSING)
        if items != expected_panel["items"]:
            raise self._invalid(
                detail,
                method="GET",
                route=_PANEL_ROUTE,
                reason="verification" if isinstance(items, list) else "field_type",
                field="panel.items",
                expected="list",
                actual="mismatch" if isinstance(items, list) else _value_type(items),
            )

    async def _find(self, ctx: Any, api: QQGroupAPI) -> dict | None:
        marker = self._marker(ctx.group_id)
        cursor = ""
        cursors: set[str] = set()
        found: set[str] = set()
        for _ in range(21):
            data = await api.list_panels(cursor=cursor)
            records = data.get("records", _MISSING)
            if not isinstance(records, list):
                raise self._invalid(
                    data,
                    method="GET",
                    route=_PANELS_ROUTE,
                    reason="field_type",
                    field="records",
                    expected="list",
                    actual=_value_type(records),
                )
            for row in records:
                panel = row.get("panel", _MISSING) if isinstance(row, dict) else _MISSING
                if not isinstance(panel, dict):
                    raise self._invalid(
                        data,
                        method="GET",
                        route=_PANELS_ROUTE,
                        reason="field_type",
                        field="records[].panel",
                        expected="object",
                        actual=_value_type(panel),
                    )
                panel_id = row.get("panel_id", _MISSING)
                if not isinstance(panel_id, str) or not panel_id.strip():
                    raise self._invalid(
                        data,
                        method="GET",
                        route=_PANELS_ROUTE,
                        reason="field_value" if isinstance(panel_id, str) else "field_type",
                        field="records[].panel_id",
                        expected="nonempty_string",
                        actual="mismatch" if isinstance(panel_id, str) else _value_type(panel_id),
                    )
                if panel.get("remark") != marker:
                    continue
                found.add(panel_id)
            next_cursor = data.get("next_cursor", _MISSING)
            is_end = data.get("is_end", _MISSING)
            for field, value, expected, value_class in (
                ("next_cursor", next_cursor, "string", str),
                ("is_end", is_end, "boolean", bool),
            ):
                if value is not _MISSING and not isinstance(value, value_class):
                    raise self._invalid(
                        data,
                        method="GET",
                        route=_PANELS_ROUTE,
                        reason="field_type",
                        field=field,
                        expected=expected,
                        actual=_value_type(value),
                    )
            if next_cursor is not _MISSING and is_end is not _MISSING:
                if is_end != (next_cursor == ""):
                    raise self._invalid(
                        data,
                        method="GET",
                        route=_PANELS_ROUTE,
                        reason="pagination",
                        field="next_cursor",
                        expected="string",
                        actual="mismatch",
                    )
            # Both fields independently document the final-page signal. Never
            # turn an absent cursor into an empty one: {} is not a final page.
            if is_end is True or next_cursor == "":
                break
            if next_cursor is _MISSING or next_cursor in cursors:
                raise self._invalid(
                    data,
                    method="GET",
                    route=_PANELS_ROUTE,
                    reason="pagination",
                    field="next_cursor",
                    expected="string",
                    actual="missing" if next_cursor is _MISSING else "cursor_loop",
                )
            cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise self._invalid(
                data,
                method="GET",
                route=_PANELS_ROUTE,
                reason="pagination",
                field="next_cursor",
                expected="string",
                actual="page_limit",
            )
        if len(found) > 1:
            raise ValueError(
                "当前群存在多个同标记的官方指令面板，请先在开放平台检查，未修改任何面板。"
            )
        if not found:
            return None
        panel_id = next(iter(found))
        detail = await api.get_panel(panel_id)
        self._validate_detail(ctx, detail, panel_id)
        return detail

    async def _create(self, ctx: Any, api: QQGroupAPI, panel: dict) -> tuple[str, bool]:
        try:
            result = await api.create_panel([ctx.group_id], panel)
            panel_id = result.get("panel_id", _MISSING)
            if not isinstance(panel_id, str) or not panel_id.strip():
                raise self._invalid(
                    result,
                    method="POST",
                    route=_PANELS_ROUTE,
                    reason="field_value" if isinstance(panel_id, str) else "field_type",
                    field="panel_id",
                    expected="nonempty_string",
                    actual="mismatch" if isinstance(panel_id, str) else _value_type(panel_id),
                )
            return panel_id, False
        except QQAPIError as write_error:
            if write_error.code != "INVALID_RESPONSE":
                raise
            # A malformed acknowledgement does not justify repeating POST.
            # Search all pages once, then verify ownership, targets and content.
            try:
                detail = await self._find(ctx, api)
                if detail is not None:
                    self._verify_items(detail, panel)
            except (QQAPIError, ValueError):
                raise write_error from None
            if detail is None:
                raise write_error from None
            return detail["panel_id"], True

    async def _update(self, ctx: Any, api: QQGroupAPI, panel_id: str, panel: dict) -> bool:
        try:
            result = await api.update_panel(panel_id, panel)
            version = result.get("version", _MISSING)
            if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                integer = isinstance(version, int) and not isinstance(version, bool)
                raise self._invalid(
                    result,
                    method="PUT",
                    route=_PANEL_ROUTE,
                    reason="field_value" if integer else "field_type",
                    field="version",
                    expected="nonnegative_integer",
                    actual="mismatch" if integer else _value_type(version),
                )
            return False
        except QQAPIError as write_error:
            if write_error.code != "INVALID_RESPONSE":
                raise
            # The ID is already known; one GET can confirm the desired state.
            # Preserve the write error if verification fails or is unavailable.
            try:
                detail = await api.get_panel(panel_id)
                self._validate_detail(ctx, detail, panel_id)
                self._verify_items(detail, panel)
            except (QQAPIError, ValueError):
                raise write_error from None
            return True

    async def status(self, ctx: Any, api: QQGroupAPI) -> dict:
        async with self._lock(ctx, api):
            detail = await self._find(ctx, api)
            if detail is None:
                return {
                    "状态": "本群尚未设置插件的官方指令菜单",
                    "设置方法": "由 AstrBot 管理员发送 /设置菜单",
                }
            return {
                "状态": "已设置 QQ 官方群指令菜单",
                "panel_id": detail["panel_id"],
                "version": detail.get("version"),
                "commands": detail["panel"].get("items", []),
            }

    async def install(self, ctx: Any, api: QQGroupAPI) -> dict:
        async with self._lock(ctx, api):
            detail = await self._find(ctx, api)
            panel = {"items": group_command_items(), "remark": self._marker(ctx.group_id)}
            verified = False
            if detail is None:
                panel_id, verified = await self._create(ctx, api, panel)
                status = "已创建"
            else:
                panel_id = detail["panel_id"]
                if detail["panel"].get("items") == panel["items"]:
                    status = "已是最新"
                else:
                    verified = await self._update(ctx, api, panel_id, panel)
                    status = "已更新"
            return {
                "状态": status,
                "panel_id": panel_id,
                "指令数": len(panel["items"]),
                "说明": ("已通过查询核实菜单内容及当前群归属。" if verified else "")
                + "在 QQ 中打开本机器人的群指令面板，选择指令、补齐 @成员等参数后发送即执行。客户端刷新显示可能有延迟。",
            }

    async def remove(self, ctx: Any, api: QQGroupAPI) -> dict:
        async with self._lock(ctx, api):
            detail = await self._find(ctx, api)
            if detail is None:
                return {"状态": "本群未设置插件的官方指令菜单"}
            await api.delete_panel(detail["panel_id"])
            return {"状态": "已移除本群的插件指令菜单", "panel_id": detail["panel_id"]}
