"""Install QQ's native group command panel without changing unrelated panels."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from .api import QQAPIError, QQGroupAPI
from .menu import group_command_items


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
    def _invalid() -> QQAPIError:
        return QQAPIError(200, "INVALID_RESPONSE", "官方指令面板响应不完整，未继续修改。")

    async def _find(self, ctx: Any, api: QQGroupAPI) -> dict | None:
        marker = self._marker(ctx.group_id)
        cursor = ""
        cursors: set[str] = set()
        found: set[str] = set()
        for _ in range(21):
            data = await api.list_panels(cursor=cursor)
            records = data.get("records")
            if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                raise self._invalid()
            for row in records:
                panel = row.get("panel")
                panel_id = row.get("panel_id")
                if not isinstance(panel, dict) or not isinstance(panel_id, str) or not panel_id:
                    raise self._invalid()
                if panel.get("remark") != marker:
                    continue
                found.add(panel_id)
            next_cursor = data.get("next_cursor", "")
            is_end = data.get("is_end")
            if not isinstance(next_cursor, str) or not isinstance(is_end, bool):
                raise self._invalid()
            if is_end:
                if next_cursor:
                    raise self._invalid()
                break
            if not next_cursor or next_cursor in cursors:
                raise self._invalid()
            cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise self._invalid()
        if len(found) > 1:
            raise ValueError(
                "当前群存在多个同标记的官方指令面板，请先在开放平台检查，未修改任何面板。"
            )
        if not found:
            return None
        panel_id = next(iter(found))
        detail = await api.get_panel(panel_id)
        panel = detail.get("panel")
        if (
            detail.get("panel_id") != panel_id
            or detail.get("scope") != "group"
            or detail.get("target_type") != "specific"
            or not isinstance(panel, dict)
            or panel.get("remark") != marker
        ):
            raise self._invalid()
        if detail.get("group_openids") != [ctx.group_id]:
            raise ValueError("此指令面板的关联群已被修改，未更新或删除。请在开放平台核对后重试。")
        return detail

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
            if detail is None:
                result = await api.create_panel([ctx.group_id], panel)
                panel_id = result.get("panel_id")
                if not isinstance(panel_id, str) or not panel_id:
                    raise self._invalid()
                status = "已创建"
            else:
                panel_id = detail["panel_id"]
                if detail["panel"].get("items") == panel["items"]:
                    status = "已是最新"
                else:
                    result = await api.update_panel(panel_id, panel)
                    version = result.get("version")
                    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                        raise self._invalid()
                    status = "已更新"
            return {
                "状态": status,
                "panel_id": panel_id,
                "指令数": len(panel["items"]),
                "说明": "在 QQ 中打开本机器人的群指令面板，选择指令、补齐 @成员等参数后发送即执行。客户端刷新显示可能有延迟。",
            }

    async def remove(self, ctx: Any, api: QQGroupAPI) -> dict:
        async with self._lock(ctx, api):
            detail = await self._find(ctx, api)
            if detail is None:
                return {"状态": "本群未设置插件的官方指令菜单"}
            await api.delete_panel(detail["panel_id"])
            return {"状态": "已移除本群的插件指令菜单", "panel_id": detail["panel_id"]}
