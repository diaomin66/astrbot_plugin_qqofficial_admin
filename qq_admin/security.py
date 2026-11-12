"""所有入口共用的服务端鉴权，不信任按钮客户端 permission。"""

import logging
import re
from collections.abc import Mapping
from typing import Any

from .api import QQAPIError

logger = logging.getLogger(__name__)


class PermissionDenied(ValueError):
    pass


class AccessControl:
    def __init__(self, config: Mapping[str, Any]):
        self.config = config

    def check_group(self, ctx: Any) -> None:
        allowed = self.config.get("allowed_groups", [])
        if allowed and ctx.group_id not in allowed:
            raise PermissionDenied("本群不在插件允许列表中，请由 AstrBot 管理员配置群 OpenID。")

    async def require(self, ctx: Any, api: Any, *, global_operation: bool = False) -> None:
        self.check_group(ctx)
        if ctx.is_superuser:
            return
        if global_operation:
            raise PermissionDenied("此操作涉及机器人级资源，仅 AstrBot 管理员可以操作。")
        identity = f"{ctx.platform_id}|{ctx.group_id}|{ctx.user_id}"
        if identity in self.config.get("trusted_admins", []):
            return
        if self.config.get("allow_group_admins", True):
            try:
                member = await api.get_member(ctx.group_id, ctx.user_id)
            except QQAPIError as exc:
                logger.warning("QQ administrator verification failed: %s", exc.user_message())
                raise PermissionDenied(
                    "无法验证管理员身份，请联系 AstrBot 管理员配置权限。"
                ) from exc
            if member.get("member_role") in {"owner", "admin"} and not member.get("bot", False):
                return
        raise PermissionDenied("此功能仅限群主、群管理员、已配置的受信任管理员或 AstrBot 管理员。")

    @staticmethod
    def verify_button(ctx: Any, data: Any) -> str:
        if not isinstance(data, dict):
            raise PermissionDenied("按钮数据无效，请重新发送 /菜单。")
        expected = {
            "platform_id": ctx.platform_id,
            "group_id": ctx.group_id,
            "owner_id": ctx.user_id,
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise PermissionDenied("该菜单属于其他操作者或群，请发送 /菜单 创建自己的菜单。")
        command = data.get("command")
        if not isinstance(command, str) or not command or len(command) > 16000:
            raise PermissionDenied("按钮指令无效，请重新发送 /菜单。")
        return command

    @staticmethod
    def button_request_key(data: Any) -> str:
        """A stable per-button key prevents repeated writes; it grants no access."""
        value = data.get("operation_id") if isinstance(data, dict) else None
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) is None:
            raise PermissionDenied("此操作按钮已过期，请重新发送 /菜单 后选择操作。")
        return "button:" + value
