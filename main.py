"""AstrBot QQ 官方群管理插件入口。"""

import asyncio
import time
from functools import partial
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .qq_admin.api import QQAPIError, QQGroupAPI
from .qq_admin.commands import DIRECT_COMMANDS, normalize_command
from .qq_admin.integration import (
    BotpyTransport,
    encode_callback,
    extract_event_context,
    reply_age_seconds,
    send_payload,
)
from .qq_admin.manager import WRITE_COMMANDS, AdminManager, split_head
from .qq_admin.panels import Panel, build_payload
from .qq_admin.security import AccessControl
from .qq_admin.store import LocalStore

if not hasattr(filter, "button_action"):
    raise RuntimeError("QQ 官方群管需要 AstrBot >= 4.29.4，请先升级以启用原生按钮回调。")

# These codes establish that the rich message was rejected. A timeout or 5xx
# might have happened after acceptance, so it must never trigger a second send.
RICH_MESSAGE_REJECTIONS = {
    "304036",
    "304037",
    "305007",
    "40034011",
    "40034029",
    "40034124",
    "40034127",
}


def _plain_panel(panel: Panel) -> str:
    text = f"{panel.title}\n\n{panel.body}"
    if panel.buttons:
        text += "\n\n可用命令：\n" + "\n".join(
            f"{button.label}：/{button.command}" for button in panel.buttons
        )
    # QQ's XML-like mention tags are executable even in plain messages.
    return text.replace("<", "＜").replace(">", "＞")


@register(
    "astrbot_plugin_qqofficial_admin",
    "QQ Official Admin Contributors",
    "QQ 官方群管理、原生按钮与入群审批",
    "1.2.0",
)
class QQOfficialAdmin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._manager: AdminManager | None = None
        self._store: LocalStore | None = None
        self._transports: dict[str, BotpyTransport] = {}
        self._init_lock = asyncio.Lock()
        self._transport_lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task | None = None
        self._active_handlers: set[asyncio.Task] = set()
        self._closing = False

    async def initialize(self):
        async with self._init_lock:
            if self._manager is not None or self._closing:
                return
            data_dir = StarTools.get_data_dir("astrbot_plugin_qqofficial_admin")
            store = await asyncio.to_thread(
                LocalStore,
                data_dir / "admin.sqlite3",
                int(self.config.get("audit_retention_days", 30)),
            )
            if self._closing:
                await store.close()
                return
            self._store = store
            self._manager = AdminManager(self._store, self.config)
            self._maintenance_task = asyncio.create_task(self._maintenance())

    async def _maintenance(self):
        while True:
            await asyncio.sleep(60)
            try:
                if self._store:
                    await self._store.cleanup()
                if self._manager:
                    self._manager.messages.prune()
                    self._manager.pager.cleanup()
            except Exception:
                logger.error("QQ 官方群管定期数据清理失败，请检查数据目录权限。")

    async def _transport(self, event: AstrMessageEvent) -> BotpyTransport:
        if self._closing:
            raise QQAPIError(0, "TRANSPORT_CLOSED", "插件正在关闭")
        platform_id = str(event.get_platform_id())
        bot = getattr(event, "bot", None)
        if bot is None:
            raise QQAPIError(0, "AUTH_UNAVAILABLE", "缺少 QQ 官方机器人连接")
        async with self._transport_lock:
            if self._closing:
                raise QQAPIError(0, "TRANSPORT_CLOSED", "插件正在关闭")
            transport = self._transports.get(platform_id)
            if transport is not None and transport.bot is not bot:
                await transport.close()
                transport = None
            if transport is None:
                timeout = max(5, min(60, int(self.config.get("request_timeout", 15))))
                transport = BotpyTransport(bot, timeout=timeout)
                self._transports[platform_id] = transport
            return transport

    @filter.command(
        "菜单", alias=(set(DIRECT_COMMANDS) - {"菜单", "help"}) | {"群管", "qqadmin"}, priority=100
    )
    async def admin_command(self, event: AstrMessageEvent):
        """/设置菜单 安装 QQ 官方指令菜单；/禁言 @成员 直接执行；/菜单 打开快捷入口。"""
        if self._closing or not self.config.get("enabled", True):
            return
        ctx = extract_event_context(event)
        if ctx is None:
            return
        event.stop_event()
        try:
            command = normalize_command(event)
        except ValueError as exc:
            await self._deliver(event, Panel("未执行操作", str(exc)))
            return
        await self._dispatch(event, command)

    @filter.button_action("qqadmin", priority=100)
    async def admin_button(self, event: AstrMessageEvent):
        if self._closing or not self.config.get("enabled", True):
            return
        ctx = extract_event_context(event)
        if ctx is None:
            return
        event.stop_event()
        interaction = event.get_button_interaction()
        try:
            data = getattr(interaction, "data", None)
            command = AccessControl.verify_button(ctx, data)
            request_key = (
                AccessControl.button_request_key(data)
                if split_head(command)[0] in WRITE_COMMANDS
                else ""
            )
        except ValueError as exc:
            await self._respond_error(event, str(exc))
            return
        await self._dispatch(event, command, request_key=request_key)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-100)
    async def observe_group_message(self, event: AstrMessageEvent):
        if (
            self._closing
            or not self.config.get("enabled", True)
            or not self.config.get("remember_messages", True)
        ):
            return
        ctx = extract_event_context(event)
        if ctx is None or not ctx.message_id:
            return
        allowed = self.config.get("allowed_groups", [])
        if allowed and ctx.group_id not in allowed:
            return
        await self.initialize()
        if self._manager:
            self._manager.messages.remember(ctx, timestamp=time.time() - reply_age_seconds(event))

    async def _dispatch(self, event: AstrMessageEvent, command: str, *, request_key: str = ""):
        if self._closing:
            return
        task = asyncio.current_task()
        if task:
            self._active_handlers.add(task)
        try:
            await self._run_dispatch(event, command, request_key=request_key)
        finally:
            if task:
                self._active_handlers.discard(task)

    async def _run_dispatch(self, event: AstrMessageEvent, command: str, *, request_key: str = ""):
        await self.initialize()
        ctx = extract_event_context(event)
        if self._closing or ctx is None or self._manager is None:
            return
        try:
            self._manager.access.check_group(ctx)
            if split_head(command)[0] in WRITE_COMMANDS and reply_age_seconds(event) >= 300:
                raise ValueError("这条操作消息已超过 5 分钟，请发送新指令。")
            if self.config.get("remember_messages", True):
                self._manager.messages.remember(
                    ctx, timestamp=time.time() - reply_age_seconds(event)
                )
            transport = await self._transport(event)
            panel = await self._manager.handle(
                ctx, command, QQGroupAPI(transport), request_key=request_key
            )
        except QQAPIError as exc:
            panel = Panel("官方接口暂不可用", exc.user_message())
        except ValueError as exc:
            panel = Panel("未执行操作", str(exc))
        except Exception:
            logger.error("QQ 官方群管处理异常，请检查插件配置与数据目录。")
            panel = Panel(
                "处理失败",
                "未能完成本次请求，请先通过日志或状态查询核实结果，再决定是否发送新指令。",
            )
        await self._deliver(event, panel)

    async def _respond_error(self, event: AstrMessageEvent, message: str):
        await self._deliver(event, Panel("无法使用此按钮", message))

    async def _deliver(self, event: AstrMessageEvent, panel: Panel):
        if self._closing:
            return
        try:
            await self._send_panel(event, panel)
        except QQAPIError as exc:
            logger.warning(f"QQ 官方群管回复失败：{exc.user_message()}")
        except Exception:
            logger.error("QQ 官方群管面板发送失败；已执行操作不会因回复失败而重试。")

    async def _send_panel(self, event: AstrMessageEvent, panel: Panel) -> Any:
        transport = await self._transport(event)
        ctx = extract_event_context(event)
        if ctx is None:
            return None
        if self.config.get("use_markdown", True):
            encoder = partial(encode_callback, expires_at=time.time() + 300)
            payload = build_payload(panel, ctx, encoder)
        else:
            payload = {"msg_type": 0, "content": _plain_panel(panel)}
        try:
            response = await send_payload(event, payload, transport)
        except QQAPIError as exc:
            if (
                payload["msg_type"] != 2
                or str(exc.code) not in RICH_MESSAGE_REJECTIONS
                or exc.status == 0
                or exc.status >= 500
            ):
                raise
            response = await send_payload(
                event, {"msg_type": 0, "content": _plain_panel(panel)}, transport
            )
        if (
            response
            and response.get("id")
            and self._manager
            and self.config.get("remember_messages", True)
        ):
            self._manager.messages.remember(ctx, message_id=response["id"], author="机器人")
        return response

    async def terminate(self):
        self._closing = True
        active = [task for task in self._active_handlers if task is not asyncio.current_task()]
        if active:
            _, pending = await asyncio.wait(active, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
        for transport in self._transports.values():
            await transport.close()
        self._transports.clear()
        if self._store:
            await self._store.close()
            self._store = None
        if self._manager:
            self._manager.pager.clear()
        self._manager = None
