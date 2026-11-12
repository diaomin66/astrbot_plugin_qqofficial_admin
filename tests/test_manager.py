import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from qq_admin.api import QQAPIError, QQGroupAPI
from qq_admin.diagnostics import current_request_state
from qq_admin.integration import EventContext
from qq_admin.manager import AdminManager, duration_seconds
from qq_admin.pagination import MAX_SNAPSHOT_CHARACTERS
from qq_admin.security import PermissionDenied
from qq_admin.store import LocalStore


class RecordingTransport:
    def __init__(self):
        self.calls = []
        self.role = "admin"
        self.result = {}
        self.failure = None
        self.seq = 10001

    async def request(self, method, path, *, json=None, params=None):
        self.calls.append((method, path, json, params))
        if method == "GET" and path.endswith("/members/operator"):
            return {"member_role": self.role, "bot": False}
        if self.failure:
            raise self.failure
        return self.result

    async def next_message_sequence(self, group, reply):
        self.seq += 1
        return self.seq


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = LocalStore(Path(self.directory.name) / "test.sqlite3")
        self.manager = AdminManager(self.store, {})
        self.transport = RecordingTransport()
        self.api = QQGroupAPI(self.transport)
        self.ctx = EventContext("bot", "group", "operator", True, message_id="message")

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_each_mutation_directly_calls_correct_official_endpoint(self):
        cases = [
            ("移出 target 拉黑", "POST", "/v2/groups/group/batch_remove_members"),
            ("拉黑 target", "POST", "/v2/groups/group/member_blacklist"),
            ("取消拉黑 target", "POST", "/v2/groups/group/member_blacklist"),
            ("禁言 target 10m", "POST", "/v2/groups/group/restrict_chat_setting"),
            ("更新禁言 target 1h", "POST", "/v2/groups/group/restrict_chat_setting"),
            ("解禁 target", "POST", "/v2/groups/group/restrict_chat_setting"),
            ("同意 applicant request1", "POST", "/v2/groups/group/approval_join_request/applicant"),
            (
                "拒绝 applicant request2 --拉黑 广告",
                "POST",
                "/v2/groups/group/approval_join_request/applicant",
            ),
            (
                '策略创建 {"group_openids":["group"],"remark":"测试"}',
                "POST",
                "/v2/groups/join_approval_strategy",
            ),
            (
                '策略修改 strategy {"is_enable":"off"}',
                "PATCH",
                "/v2/groups/join_approval_strategy/strategy",
            ),
            ("策略删除 strategy", "DELETE", "/v2/groups/join_approval_strategy/strategy"),
            ("策略执行 strategy", "POST", "/v2/groups/join_approval_strategy/strategy/execute"),
            (
                "策略白名单 strategy add 123456,123457",
                "POST",
                "/v2/groups/join_approval_strategy/strategy/whitelist_users",
            ),
            ("撤回 message1", "DELETE", "/v2/groups/group/messages/message1"),
            ("通知 本周活动", "POST", "/v2/groups/group/messages"),
            ("全体通知 本周活动", "POST", "/v2/groups/group/messages"),
        ]
        for index, (command, method, path) in enumerate(cases):
            with self.subTest(command=command):
                self.transport.calls.clear()
                ctx = replace(self.ctx, message_id=f"message-{index}")
                result = await self.manager.handle(ctx, command, self.api)
                self.assertIn(result.title, {"操作已完成", "策略执行请求已受理"})
                self.assertEqual(len(self.transport.calls), 1)
                self.assertEqual(self.transport.calls[0][:2], (method, path))
        log = await self.store.recent_audit(self.ctx, 20)
        self.assertEqual(len(log), len(cases))
        self.assertTrue(all(row["status"] == "success" for row in log))

    async def test_fresh_role_check_blocks_revoked_admin_after_viewing_member(self):
        actor = replace(self.ctx, is_superuser=False)
        await self.manager.handle(actor, "成员详情 target", self.api)
        self.transport.role = "member"
        with self.assertRaises(PermissionDenied):
            await self.manager.handle(actor, "移出 target", self.api)
        self.assertTrue(all(call[0] == "GET" for call in self.transport.calls))
        self.assertEqual(await self.store.recent_audit(actor), [])

    async def test_global_strategy_never_accepts_group_admin(self):
        actor = replace(self.ctx, is_superuser=False)
        with self.assertRaises(PermissionDenied):
            await self.manager.handle(actor, "策略删除 strategy", self.api)
        self.assertEqual(self.transport.calls, [])

    async def test_repeated_message_deliveries_execute_once(self):
        results = await asyncio.gather(
            *(self.manager.handle(self.ctx, "移出 target", self.api) for _ in range(12)),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(value, Exception) for value in results), 1)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_new_messages_can_intentionally_repeat_the_same_command(self):
        for message_id in ("message-1", "message-2"):
            await self.manager.handle(
                replace(self.ctx, message_id=message_id), "禁言 target", self.api
            )
        self.assertEqual(len(self.transport.calls), 2)

    async def test_same_button_on_distinct_click_events_executes_once(self):
        for index in range(2):
            click = replace(self.ctx, message_id="", event_id=f"INTERACTION_CREATE:{index}")
            if index == 0:
                await self.manager.handle(
                    click, "移出 target", self.api, request_key="button:fixed-operation-id"
                )
            else:
                with self.assertRaises(ValueError):
                    await self.manager.handle(
                        click, "移出 target", self.api, request_key="button:fixed-operation-id"
                    )
        self.assertEqual(len(self.transport.calls), 1)

    async def test_missing_event_identity_never_executes_write(self):
        with self.assertRaises(ValueError):
            await self.manager.handle(replace(self.ctx, message_id=""), "移出 target", self.api)
        self.assertEqual(self.transport.calls, [])

    async def test_old_confirmation_commands_do_not_execute_old_actions(self):
        for command in ("确认 old-token", "取消 old-token"):
            panel = await self.manager.handle(self.ctx, command, self.api)
            self.assertEqual(panel.title, "旧指令已停用")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(await self.store.recent_audit(self.ctx), [])

    async def test_native_menu_changes_require_astrbot_admin_and_current_group(self):
        actor = replace(self.ctx, is_superuser=False)
        for command in ("设置菜单", "移除菜单"):
            with self.assertRaises(PermissionDenied):
                await self.manager.handle(actor, command, self.api)
            with self.assertRaises(ValueError):
                await self.manager.handle(self.ctx, command + " another-group", self.api)
        self.assertEqual(self.transport.calls, [])

    async def test_partial_blacklist_failure_is_visible_and_audited(self):
        self.transport.result = {
            "remove_members_result": "success",
            "add_to_member_blacklist_fail_openids": ["target"],
        }
        panel = await self.manager.handle(self.ctx, "移出 target 拉黑", self.api)
        self.assertEqual(panel.title, "部分操作未成功")
        self.assertIn("target", panel.body)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "partial")

    async def test_long_write_result_keeps_failure_details_on_later_pages(self):
        self.transport.result = {"detail": "large reply " * 700, "fail_openids": ["failed-member"]}
        panel = await self.manager.handle(self.ctx, "拉黑 target", self.api)
        shown = panel.body
        while next_button := next(
            (button for button in panel.buttons if button.label == "下一段"), None
        ):
            panel = await self.manager.handle(self.ctx, next_button.command, self.api)
            shown += panel.body
        self.assertIn("failed-member", shown)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "partial")

    async def test_oversize_success_result_does_not_claim_write_was_not_executed(self):
        self.transport.result = {"detail": "x" * (MAX_SNAPSHOT_CHARACTERS + 1)}
        panel = await self.manager.handle(self.ctx, "移出 target", self.api)
        self.assertEqual(panel.title, "操作已完成")
        self.assertIn("无法完整显示", panel.body)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "success")

    async def test_timeout_is_unknown_and_request_cannot_be_retried(self):
        self.transport.failure = QQAPIError(0, "TIMEOUT", "request timeout")
        panel = await self.manager.handle(self.ctx, "移出 target", self.api)
        self.assertEqual(panel.title, "操作结果尚未确认")
        self.assertEqual(len(self.transport.calls), 1)
        with self.assertRaises(ValueError):
            await self.manager.handle(self.ctx, "移出 target", self.api)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")

    async def test_member_mute_expiry_is_calculated_from_execution(self):
        await self.manager.handle(self.ctx, "禁言 target 10m", self.api)
        body = self.transport.calls[0][2]
        expires = datetime.fromisoformat(body["members"][0]["mute_expire_at"])
        self.assertTrue(595 < (expires - datetime.now(UTC)).total_seconds() <= 600)
        self.assertEqual(body["members"][0]["op"], "add")

    async def test_mute_without_duration_immediately_executes_ten_minutes(self):
        panel = await self.manager.handle(self.ctx, "禁言 target", self.api)
        self.assertIn("600 秒", panel.body)
        self.assertEqual(panel.title, "操作已完成")
        self.assertNotIn("/确认", panel.body)
        self.assertNotIn("/群管", panel.body)
        self.assertEqual(len(self.transport.calls), 1)
        body = self.transport.calls[0][2]
        self.assertEqual(body["members"][0]["member_openid"], "target")
        expires = datetime.fromisoformat(body["members"][0]["mute_expire_at"])
        self.assertTrue(595 < (expires - datetime.now(UTC)).total_seconds() <= 600)

    async def test_mute_without_target_or_with_only_duration_is_rejected(self):
        for command in ("禁言", "禁言 10m", "更新禁言 600", "禁言 30分钟"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                await self.manager.handle(self.ctx, command, self.api)
        self.assertEqual(self.transport.calls, [])

    async def test_numeric_member_openid_remains_a_valid_default_mute_target(self):
        member = "12345678901234567890123456789012"
        await self.manager.handle(self.ctx, f"禁言 {member}", self.api)
        self.assertEqual(self.transport.calls[0][2]["members"][0]["member_openid"], member)

    async def test_success_http_status_with_invalid_body_is_still_uncertain(self):
        self.transport.failure = QQAPIError(200, "INVALID_RESPONSE", "invalid JSON")
        panel = await self.manager.handle(self.ctx, "移出 target", self.api)
        self.assertEqual(panel.title, "操作结果尚未确认")
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")

    async def test_success_headers_with_unreadable_body_are_still_uncertain(self):
        for code in ("TIMEOUT", "NETWORK_ERROR"):
            with self.subTest(code=code):
                self.transport.calls.clear()
                self.transport.failure = QQAPIError(201, code, "response body incomplete")
                ctx = replace(self.ctx, message_id=f"body-{code}")
                panel = await self.manager.handle(ctx, "移出 target", self.api)
                self.assertEqual(panel.title, "操作结果尚未确认")
                self.assertEqual(len(self.transport.calls), 1)
                self.assertEqual((await self.store.recent_audit(ctx))[0]["status"], "unknown")

    async def test_malformed_upstream_error_code_cannot_break_audit_failure_handling(self):
        self.transport.failure = QQAPIError(503, {"unexpected": "private-value"}, "opaque")
        panel = await self.manager.handle(self.ctx, "移出 target", self.api)
        self.assertEqual(panel.title, "操作结果尚未确认")
        self.assertNotIn("private-value", panel.body)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")

    async def test_menu_preflight_errors_never_claim_a_modification_was_attempted(self):
        failures = [
            QQAPIError(200, "INVALID_RESPONSE", "unsafe upstream message"),
            QQAPIError(0, "TIMEOUT", "unsafe upstream message"),
            QQAPIError(503, 503, "unsafe upstream message"),
        ]
        for command in ("设置菜单", "移除菜单"):
            for index, failure in enumerate(failures):
                with self.subTest(command=command, failure=failure.code):
                    self.transport.calls.clear()
                    self.transport.failure = failure
                    ctx = replace(self.ctx, message_id=f"preflight-{command}-{index}")
                    panel = await self.manager.handle(ctx, command, self.api)
                    self.assertEqual(panel.title, "未执行操作")
                    self.assertIn("未发送修改请求", panel.body)
                    self.assertNotIn("操作结果尚未确认", panel.body)
                    self.assertNotIn("unsafe upstream message", panel.body)
                    self.assertEqual([call[0] for call in self.transport.calls], ["GET"])
                    audit = (await self.store.recent_audit(ctx))[0]
                    self.assertEqual(audit["status"], "failed")
                    with self.assertRaises(ValueError):
                        await self.manager.handle(ctx, command, self.api)

    async def test_menu_invalid_list_explains_field_without_creating_a_panel(self):
        self.transport.result = {"records": None, "is_end": False, "next_cursor": "more"}
        panel = await self.manager.handle(self.ctx, "设置菜单", self.api)
        self.assertEqual(panel.title, "未执行操作")
        self.assertTrue(panel.prefer_plain_text)
        self.assertIn("/设置菜单", panel.body)
        self.assertIn("GET /v2/panels", panel.body)
        self.assertIn("records", panel.body)
        self.assertIn("null", panel.body)
        self.assertEqual([call[0] for call in self.transport.calls], ["GET"])
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "failed")

    async def test_cancellation_before_and_after_write_is_audited_separately(self):
        self.transport.failure = asyncio.CancelledError()
        for command, status, method in (
            ("设置菜单", "failed", "GET"),
            ("移出 target", "unknown", "POST"),
        ):
            with self.subTest(command=command):
                self.transport.calls.clear()
                ctx = replace(self.ctx, message_id=f"cancel-{method}")
                with self.assertRaises(asyncio.CancelledError):
                    await self.manager.handle(ctx, command, self.api)
                self.assertEqual((await self.store.recent_audit(ctx))[0]["status"], status)
                self.assertEqual([call[0] for call in self.transport.calls], [method])
                self.assertIsNone(current_request_state.get())

    async def test_internal_failure_before_and_after_write_does_not_expose_exception(self):
        for command, status, title in (
            ("设置菜单", "failed", "未执行操作"),
            ("移出 target", "unknown", "操作结果尚未确认"),
        ):
            self.transport.failure = RuntimeError("private credential should never be shown")
            ctx = replace(self.ctx, message_id=f"exception-{status}")
            panel = await self.manager.handle(ctx, command, self.api)
            self.assertEqual(panel.title, title)
            self.assertNotIn("private credential", panel.body)
            self.assertEqual((await self.store.recent_audit(ctx))[0]["status"], status)
            self.assertIsNone(current_request_state.get())

    async def test_concurrent_menu_preflight_does_not_inherit_another_commands_write_state(self):
        write_started = asyncio.Event()
        finish_write = asyncio.Event()

        async def request(method, path, **kwargs):
            self.transport.calls.append((method, path, kwargs))
            if method == "POST":
                write_started.set()
                await finish_write.wait()
                raise QQAPIError(0, "TIMEOUT", "write timeout")
            await write_started.wait()
            return {"records": None, "is_end": False}

        self.transport.request = AsyncMock(side_effect=request)
        write_ctx = replace(self.ctx, message_id="concurrent-write")
        menu_ctx = replace(self.ctx, message_id="concurrent-menu")
        task = asyncio.create_task(self.manager.handle(write_ctx, "移出 target", self.api))
        try:
            menu = await self.manager.handle(menu_ctx, "设置菜单", self.api)
        finally:
            finish_write.set()
            write = await task
        self.assertEqual(menu.title, "未执行操作")
        self.assertEqual(write.title, "操作结果尚未确认")
        audit = {row["action"]: row["status"] for row in await self.store.recent_audit(self.ctx)}
        self.assertEqual(audit, {"设置菜单": "failed", "移出": "unknown"})
        self.assertIsNone(current_request_state.get())

    async def test_menu_failed_readback_keeps_original_write_uncertain(self):
        results = iter(
            [
                {"records": [], "is_end": True, "next_cursor": ""},
                {},
                QQAPIError(0, "TIMEOUT", "readback failed", method="GET", request_sent=True),
            ]
        )

        async def request(method, path, **kwargs):
            self.transport.calls.append((method, path, kwargs))
            result = next(results)
            if isinstance(result, QQAPIError):
                raise result
            return result

        self.transport.request = AsyncMock(side_effect=request)
        panel = await self.manager.handle(self.ctx, "设置菜单", self.api)
        self.assertEqual(panel.title, "操作结果尚未确认")
        self.assertIn("/菜单状态", panel.body)
        self.assertIn("INVALID_RESPONSE", panel.body)
        self.assertIn("POST /v2/panels", panel.body)
        self.assertEqual([call[0] for call in self.transport.calls], ["GET", "POST", "GET"])
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")
        with self.assertRaises(ValueError):
            await self.manager.handle(self.ctx, "设置菜单", self.api)

    async def test_notice_reply_uses_current_button_event(self):
        click = replace(self.ctx, message_id="", event_id="INTERACTION_CREATE:new")
        await self.manager.handle(
            click, "全体通知 活动开始", self.api, request_key="button:notice-button-id"
        )
        body = self.transport.calls[0][2]
        self.assertNotIn("msg_id", body)
        self.assertEqual(body["event_id"], click.event_id)
        self.assertTrue(body["content"].startswith("<qqbot-at-everyone />"))

    async def test_auto_approved_join_requests_never_offer_approval_buttons(self):
        self.transport.result = {
            "list": [
                {
                    "member_openid": "done",
                    "join_request_id": "old",
                    "auto_approved": {"strategy_id": "s"},
                },
                {"member_openid": "waiting", "join_request_id": "new"},
                {
                    "member_openid": "done_empty",
                    "join_request_id": "old_empty",
                    "auto_approved": {},
                },
            ],
            "next_cursor": "next",
        }
        panel = await self.manager.handle(self.ctx, "入群", self.api)
        commands = [button.command for button in panel.buttons]
        self.assertNotIn("同意 done old", commands)
        self.assertNotIn("同意 done_empty old_empty", commands)
        self.assertIn("同意 waiting new", commands)
        self.assertIn("入群 next", commands)

    async def test_missing_request_id_and_unknown_json_fields_reject_before_execution(self):
        for command in [
            "同意 applicant",
            '策略修改 s {"make_admin":true}',
            "禁言 target 31d",
            "通知 <qqbot-at-everyone />",
        ]:
            with self.subTest(command=command), self.assertRaises(ValueError):
                await self.manager.handle(self.ctx, command, self.api)
        self.assertEqual(self.transport.calls, [])

    async def test_recent_messages_keep_no_body_and_are_scope_isolated(self):
        self.manager.messages.remember(self.ctx)
        self.assertEqual(len(self.manager.messages.list(self.ctx)), 1)
        self.assertEqual(self.manager.messages.list(replace(self.ctx, group_id="other")), [])
        self.assertEqual(
            set(self.manager.messages.list(self.ctx)[0]),
            {"message_id", "member_openid", "received_at"},
        )

    def test_duration_boundaries(self):
        self.assertEqual(duration_seconds("30d"), 2592000)
        self.assertEqual(duration_seconds("10分钟"), 600)
        for value in ["0", "-10", "31d", "inf", "1.5h"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                duration_seconds(value)
