import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from qq_admin.api import QQAPIError, QQGroupAPI
from qq_admin.integration import EventContext
from qq_admin.manager import AdminManager, duration_seconds
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

    async def prepare(self, text, ctx=None):
        panel = await self.manager.handle(ctx or self.ctx, text, self.api)
        self.assertEqual(panel.title, "请确认管理操作")
        return panel.buttons[0].command

    async def test_each_mutation_is_reviewed_then_calls_correct_official_endpoint(self):
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
        for command, method, path in cases:
            with self.subTest(command=command):
                self.transport.calls.clear()
                confirmation = await self.prepare(command)
                self.assertEqual(self.transport.calls, [], "Preparing must not change QQ state")
                result = await self.manager.handle(self.ctx, confirmation, self.api)
                self.assertIn(result.title, {"操作已完成", "策略执行请求已受理"})
                self.assertEqual(len(self.transport.calls), 1)
                self.assertEqual(self.transport.calls[0][:2], (method, path))
        log = await self.store.recent_audit(self.ctx, 20)
        self.assertEqual(len(log), len(cases))
        self.assertTrue(all(row["status"] == "success" for row in log))

    async def test_fresh_role_check_blocks_revoked_admin_at_confirmation(self):
        actor = replace(self.ctx, is_superuser=False)
        confirmation = await self.prepare("移出 target", actor)
        self.transport.role = "member"
        with self.assertRaises(PermissionDenied):
            await self.manager.handle(actor, confirmation, self.api)
        self.assertTrue(all(call[0] == "GET" for call in self.transport.calls))
        self.assertEqual(await self.store.recent_audit(actor), [])

    async def test_global_strategy_never_accepts_group_admin(self):
        actor = replace(self.ctx, is_superuser=False)
        with self.assertRaises(PermissionDenied):
            await self.manager.handle(actor, "策略删除 strategy", self.api)
        self.assertEqual(self.transport.calls, [])

    async def test_repeated_confirmations_execute_once(self):
        confirmation = await self.prepare("移出 target")
        results = await asyncio.gather(
            *(self.manager.handle(self.ctx, confirmation, self.api) for _ in range(12)),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(value, Exception) for value in results), 1)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_partial_blacklist_failure_is_visible_and_audited(self):
        confirmation = await self.prepare("移出 target 拉黑")
        self.transport.result = {
            "remove_members_result": "success",
            "add_to_member_blacklist_fail_openids": ["target"],
        }
        panel = await self.manager.handle(self.ctx, confirmation, self.api)
        self.assertEqual(panel.title, "部分操作未成功")
        self.assertIn("target", panel.body)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "partial")

    async def test_timeout_is_unknown_and_token_cannot_be_retried(self):
        confirmation = await self.prepare("移出 target")
        self.transport.failure = QQAPIError(0, "TIMEOUT", "request timeout")
        panel = await self.manager.handle(self.ctx, confirmation, self.api)
        self.assertEqual(panel.title, "操作结果尚未确认")
        self.assertEqual(len(self.transport.calls), 1)
        with self.assertRaises(ValueError):
            await self.manager.handle(self.ctx, confirmation, self.api)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")

    async def test_member_mute_expiry_is_calculated_from_execution(self):
        confirmation = await self.prepare("禁言 target 10m")
        await self.manager.handle(self.ctx, confirmation, self.api)
        body = self.transport.calls[0][2]
        expires = datetime.fromisoformat(body["members"][0]["mute_expire_at"])
        self.assertTrue(595 < (expires - datetime.now(UTC)).total_seconds() <= 600)
        self.assertEqual(body["members"][0]["op"], "add")

    async def test_mute_without_duration_previews_and_executes_ten_minutes(self):
        panel = await self.manager.handle(self.ctx, "禁言 target", self.api)
        self.assertIn("600 秒", panel.body)
        self.assertIn("/确认 ", panel.body)
        self.assertNotIn("/群管", panel.body)
        self.assertEqual(self.transport.calls, [])
        await self.manager.handle(self.ctx, panel.buttons[0].command, self.api)
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
        confirmation = await self.prepare(f"禁言 {member}")
        await self.manager.handle(self.ctx, confirmation, self.api)
        self.assertEqual(self.transport.calls[0][2]["members"][0]["member_openid"], member)

    async def test_success_http_status_with_invalid_body_is_still_uncertain(self):
        confirmation = await self.prepare("移出 target")
        self.transport.failure = QQAPIError(200, "INVALID_RESPONSE", "invalid JSON")
        panel = await self.manager.handle(self.ctx, confirmation, self.api)
        self.assertEqual(panel.title, "操作结果尚未确认")
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")

    async def test_notice_reply_uses_current_confirmation_event_not_original_message(self):
        confirmation = await self.prepare("全体通知 活动开始")
        click = replace(self.ctx, message_id="", event_id="INTERACTION_CREATE:new")
        await self.manager.handle(click, confirmation, self.api)
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

    async def test_missing_request_id_and_unknown_json_fields_reject_before_confirmation(self):
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
