"""所有入口共用的服务端鉴权，不信任按钮客户端 permission。"""

from collections.abc import Mapping
from typing import Any

from .api import QQAPIError


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
            raise PermissionDenied("自动审批策略作用于整台机器人，仅 AstrBot 管理员可以操作。")
        identity = f"{ctx.platform_id}|{ctx.group_id}|{ctx.user_id}"
        if identity in self.config.get("trusted_admins", []):
            return
        if self.config.get("allow_group_admins", True):
            try:
                member = await api.get_member(ctx.group_id, ctx.user_id)
            except QQAPIError as exc:
                raise PermissionDenied(
                    "无法验证你的群管理员身份，未执行操作。"
                    "成员信息接口可能尚未获得官方白名单；可由 AstrBot 管理员配置本群的受信任管理员。\n"
                    + exc.user_message()
                ) from exc
            if member.get("member_role") in {"owner", "admin"} and not member.get("bot", False):
                return
        raise PermissionDenied("此功能仅限群主、群管理员、已配置的受信任管理员或 AstrBot 管理员。")

    @staticmethod
    def verify_button(ctx: Any, data: Any) -> str:
        if not isinstance(data, dict):
            raise PermissionDenied("按钮数据无效，请重新发送 /群管。")
        expected = {
            "platform_id": ctx.platform_id,
            "group_id": ctx.group_id,
            "owner_id": ctx.user_id,
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise PermissionDenied("该面板属于其他操作者或群，请发送 /群管 创建自己的面板。")
        command = data.get("command")
        if not isinstance(command, str) or not command or len(command) > 16000:
            raise PermissionDenied("按钮指令无效，请重新发送 /群管。")
        return command
