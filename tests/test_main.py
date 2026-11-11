"""Exercise the plugin entry point with the AstrBot 4.29.4 public contracts.

The framework import surface is stubbed; management responses and outgoing
sends are mocked. Actual group validation, button identity checks, panel
rendering, and send/fallback decisions run as real code. No installed AstrBot
version, external account, or network is required.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import sys
import tempfile
import time
import types
import unittest
from enum import IntFlag
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qq_admin.api import QQAPIError
from qq_admin.panels import ButtonSpec, Panel

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_qqadmin_entrypoint_contract"


class StubStar:
    def __init__(self, context, config=None):
        self.context = context
        self.logger = logging.getLogger("qqadmin.entrypoint.test")


class StubEventType(IntFlag):
    GROUP_MESSAGE = 1
    PRIVATE_MESSAGE = 2
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE


def _filter_factory(kind):
    def register(*args, **kwargs):
        def decorate(handler):
            records = list(getattr(handler, "_contract_filters", []))
            records.append((kind, args, kwargs))
            handler._contract_filters = records
            return handler

        return decorate

    return register


def _framework_modules(data_dir: Path) -> dict[str, types.ModuleType]:
    astrbot = types.ModuleType("astrbot")
    astrbot.__version__ = "4.29.4"
    astrbot.logger = logging.getLogger("qqadmin.entrypoint.test")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = astrbot.logger
    events = types.ModuleType("astrbot.api.event")
    events.AstrMessageEvent = object
    filters = types.ModuleType("astrbot.api.event.filter")
    for kind in ("command", "button_action", "event_message_type", "on_astrbot_loaded"):
        setattr(filters, kind, _filter_factory(kind))
    filters.EventMessageType = StubEventType
    events.filter = filters
    stars = types.ModuleType("astrbot.api.star")
    stars.Star = StubStar
    stars.Context = object
    stars.StarTools = SimpleNamespace(get_data_dir=lambda *_args, **_kwargs: data_dir)
    stars.register = lambda *_args, **_kwargs: lambda cls: cls
    astrbot.api = api
    api.event = events
    api.star = stars
    return {module.__name__: module for module in (astrbot, api, events, filters, stars)}


class FakeEvent:
    def __init__(
        self,
        text="群管 信息",
        *,
        platform="qq_official",
        group="group-1",
        user="member-1",
        interaction=None,
    ):
        self.message_str = text
        self.platform = platform
        self.group = group
        self.user = user
        self.interaction = interaction
        self.stopped = False
        self.extras = {}
        self.bot = SimpleNamespace(api=SimpleNamespace(_http=SimpleNamespace(_token=None)))
        raw = SimpleNamespace(
            group_openid=group,
            id="message-1",
            author=SimpleNamespace(member_openid=user),
            timestamp=time.time(),
            content=text,
        )
        if interaction is not None:
            raw.scene = "group"
            raw.id = "interaction-1"
            raw.event_id = "outer-event-1"
            raw.group_member_openid = user
        self.message_obj = SimpleNamespace(
            raw_message=raw,
            timestamp=time.time(),
            message_id=raw.id,
            group_id=group,
        )
        self.send = AsyncMock()

    def get_message_str(self):
        return self.message_str

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return "adapter-1"

    def get_sender_id(self):
        return self.user

    def get_group_id(self):
        return self.group

    def get_self_id(self):
        return "bot-1"

    def get_messages(self):
        return []

    def is_admin(self):
        return True

    def is_button_interaction(self):
        return self.interaction is not None

    def get_button_interaction(self):
        return self.interaction

    def stop_event(self):
        self.stopped = True

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


def button_data(**changes):
    data = {
        "command": "信息",
        "platform_id": "adapter-1",
        "group_id": "group-1",
        "owner_id": "member-1",
    }
    data.update(changes)
    return data


class EntrypointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qqadmin-entrypoint-")
        self.addCleanup(self.directory.cleanup)
        modules = _framework_modules(Path(self.directory.name))
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(ROOT)]
        modules[PACKAGE_NAME] = package
        # Keep exception classes and pure plugin modules identical to those used
        # elsewhere in this test process while importing main as a real package.
        modules[PACKAGE_NAME + ".qq_admin"] = importlib.import_module("qq_admin")
        for name in ("api", "integration", "manager", "panels", "security", "store"):
            modules[f"{PACKAGE_NAME}.qq_admin.{name}"] = importlib.import_module(f"qq_admin.{name}")
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        spec = importlib.util.spec_from_file_location(PACKAGE_NAME + ".main", ROOT / "main.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        plugin_type = next(
            value
            for value in vars(self.module).values()
            if inspect.isclass(value) and value is not StubStar and issubclass(value, StubStar)
        )
        self.config = {
            "enabled": True,
            "use_markdown": True,
            "remember_messages": True,
            "allowed_groups": [],
        }
        self.plugin = plugin_type(SimpleNamespace(), self.config)
        if hasattr(self.plugin, "initialize"):
            await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)
        self.plugin._manager.handle = AsyncMock(
            return_value=Panel("测试面板", "操作结果", [ButtonSpec("返回面板", "面板")])
        )
        self.sender = AsyncMock(return_value={"id": "sent-1"})
        self.send_patch = patch.object(self.module, "send_payload", self.sender)
        self.send_patch.start()
        self.addCleanup(self.send_patch.stop)
        self.encode_patch = patch.object(
            self.module,
            "encode_callback",
            lambda action_id, data=None, **kwargs: "astrbot:" + "a" * 32,
        )
        self.encode_patch.start()
        self.addCleanup(self.encode_patch.stop)

    def handler(self, kind):
        for _name, method in inspect.getmembers(self.plugin, predicate=inspect.ismethod):
            if any(item[0] == kind for item in getattr(method, "_contract_filters", [])):
                return method
        self.fail(f"Missing AstrBot {kind} handler")

    async def test_command_alias_and_native_callback_are_registered_at_high_priority(self):
        command = self.handler("command")
        record = next(item for item in command._contract_filters if item[0] == "command")
        self.assertEqual(record[1], ("群管",))
        self.assertIn("qqadmin", record[2]["alias"])
        self.assertEqual(record[2]["priority"], 100)
        callback = self.handler("button_action")
        record = next(item for item in callback._contract_filters if item[0] == "button_action")
        self.assertEqual(record[1], ("qqadmin",))
        self.assertEqual(record[2]["priority"], 100)

    async def test_commands_stop_the_event_and_share_manager_with_callback_route(self):
        for platform in ("qq_official", "qq_official_webhook"):
            event = FakeEvent(platform=platform)
            await self.handler("command")(event)
            self.assertTrue(event.stopped)
            args = self.plugin._manager.handle.await_args.args
            self.assertEqual(args[0].group_id, "group-1")
            self.assertEqual(args[1], "信息")
        self.assertEqual(self.sender.await_count, 2)

    async def test_command_and_callback_ignore_non_qq_groups_and_guild_channels(self):
        for kind in ("command", "button_action"):
            interaction = SimpleNamespace(action_id="qqadmin", data=button_data())
            candidates = [
                FakeEvent(platform="aiocqhttp", interaction=interaction),
                FakeEvent(group="", interaction=interaction),
                FakeEvent(interaction=interaction),
            ]
            candidates[-1].message_obj.raw_message.scene = "guild"
            for event in candidates:
                await self.handler(kind)(event)
                self.assertFalse(event.stopped)
        self.plugin._manager.handle.assert_not_awaited()
        self.sender.assert_not_awaited()

    async def test_disabled_plugin_does_not_consume_events_or_send(self):
        self.config["enabled"] = False
        for kind in ("command", "button_action", "event_message_type"):
            event = FakeEvent(interaction=SimpleNamespace(action_id="qqadmin", data=button_data()))
            await self.handler(kind)(event)
            self.assertFalse(event.stopped)
        self.plugin._manager.handle.assert_not_awaited()
        self.sender.assert_not_awaited()

    async def test_callback_rejects_mismatched_platform_group_and_operator(self):
        for key in ("platform_id", "group_id", "owner_id"):
            event = FakeEvent(
                interaction=SimpleNamespace(action_id="qqadmin", data=button_data(**{key: "other"}))
            )
            await self.handler("button_action")(event)
            self.assertTrue(event.stopped)
        self.plugin._manager.handle.assert_not_awaited()

    async def test_valid_callback_dispatches_original_command_once(self):
        event = FakeEvent(
            interaction=SimpleNamespace(action_id="qqadmin", data=button_data(command="成员"))
        )
        await self.handler("button_action")(event)
        self.plugin._manager.handle.assert_awaited_once()
        self.assertEqual(self.plugin._manager.handle.await_args.args[1], "成员")
        self.assertTrue(event.stopped)
        self.sender.assert_awaited_once()

    async def test_explicit_markdown_rejection_falls_back_to_plain_text_once(self):
        self.sender.side_effect = [
            QQAPIError(403, 304037, "button permission denied"),
            {"id": "sent-fallback"},
        ]
        await self.handler("command")(FakeEvent())
        self.assertEqual(self.sender.await_count, 2)
        first, second = self.sender.await_args_list
        self.assertEqual(first.args[1]["msg_type"], 2)
        self.assertEqual(second.args[1]["msg_type"], 0)
        self.assertNotIn("keyboard", second.args[1])
        self.assertIn("/群管", second.args[1]["content"])
        self.plugin._manager.handle.assert_awaited_once()

    async def test_uncertain_network_failure_never_resends_or_reexecutes(self):
        for code in ("TIMEOUT", "NETWORK_ERROR"):
            self.sender.reset_mock(side_effect=True)
            self.plugin._manager.handle.reset_mock()
            self.sender.side_effect = QQAPIError(0, code, "uncertain result")
            await self.handler("command")(FakeEvent())
            self.sender.assert_awaited_once()
            self.plugin._manager.handle.assert_awaited_once()

    async def test_server_failure_cannot_trigger_fallback_even_with_rich_rejection_code(self):
        self.sender.side_effect = QQAPIError(503, 304037, "upstream result unknown")
        await self.handler("command")(FakeEvent())
        self.sender.assert_awaited_once()
        self.plugin._manager.handle.assert_awaited_once()

    async def test_plain_mode_sends_no_markdown_or_keyboard(self):
        self.config["use_markdown"] = False
        await self.handler("command")(FakeEvent())
        payload = self.sender.await_args.args[1]
        self.assertEqual(payload["msg_type"], 0)
        self.assertNotIn("markdown", payload)
        self.assertNotIn("keyboard", payload)

    async def test_transports_are_reused_and_replaced_when_adapter_reconnects(self):
        event = FakeEvent()
        first = await self.plugin._transport(event)
        self.assertIs(await self.plugin._transport(event), first)
        event.bot = SimpleNamespace(api=SimpleNamespace(_http=SimpleNamespace(_token=None)))
        replacement = await self.plugin._transport(event)
        self.assertIsNot(first, replacement)
        self.assertTrue(first._closed)
        self.assertFalse(replacement._closed)

    async def test_unload_cancels_maintenance_and_closes_owned_resources(self):
        transport = await self.plugin._transport(FakeEvent())
        maintenance = self.plugin._maintenance_task
        await self.plugin.terminate()
        self.assertTrue(transport._closed)
        self.assertTrue(maintenance.cancelled())
        self.assertIsNone(self.plugin._store)
        self.assertFalse(self.plugin._transports)
        event = FakeEvent()
        await self.handler("command")(event)
        self.assertFalse(event.stopped)

    async def test_inflight_handler_cannot_reopen_transport_after_unload(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def delayed_result(*args):
            entered.set()
            await finish.wait()
            return Panel("完成", "已处理")

        self.plugin._manager.handle.side_effect = delayed_result
        pending = asyncio.create_task(self.handler("command")(FakeEvent()))
        terminating = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            terminating = asyncio.create_task(self.plugin.terminate())
            await asyncio.sleep(0)
            self.assertTrue(self.plugin._closing)
        finally:
            finish.set()
            await asyncio.wait_for(pending, timeout=2)
            if terminating is not None:
                await asyncio.wait_for(terminating, timeout=2)
        self.sender.assert_not_awaited()
        self.assertFalse(self.plugin._transports)

    async def test_old_framework_has_an_explicit_upgrade_error(self):
        filters = sys.modules["astrbot.api.event.filter"]
        native_button_action = filters.button_action
        del filters.button_action
        try:
            spec = importlib.util.spec_from_file_location(
                PACKAGE_NAME + ".old_runtime", ROOT / "main.py"
            )
            module = importlib.util.module_from_spec(spec)
            with self.assertRaisesRegex(RuntimeError, "4.29.4"):
                spec.loader.exec_module(module)
        finally:
            filters.button_action = native_button_action


if __name__ == "__main__":
    unittest.main()
