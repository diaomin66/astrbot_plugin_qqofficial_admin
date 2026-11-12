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
from qq_admin.panels import MemberMention
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
        receipts = {
            "移出": "已移出1名成员并拉黑",
            "拉黑": "已拉黑1名成员",
            "取消拉黑": "已取消拉黑1名成员",
            "禁言": "已禁言 @target 10分钟",
            "更新禁言": "已更新禁言 @target 60分钟",
            "解禁": "已解禁 @target",
            "同意": "已同意入群申请",
            "拒绝": "已拒绝入群申请并拉黑",
            "策略创建": "已创建策略，可用 /策略列表 查看",
            "策略修改": "已修改策略",
            "策略删除": "已删除策略",
            "策略执行": "策略执行已提交，预计约10分钟完成",
            "策略白名单": "已更新策略白名单",
            "撤回": "已撤回消息",
            "通知": "通知已发送",
            "全体通知": "全体通知已发送",
        }
        for index, (command, method, path) in enumerate(cases):
            with self.subTest(command=command):
                self.transport.calls.clear()
                self.transport.result = (
                    {"remove_members_result": "success"} if command.startswith("移出 ") else {}
                )
                ctx = replace(self.ctx, message_id=f"message-{index}")
                result = await self.manager.handle(ctx, command, self.api)
                self.assertEqual(result.title, "")
                self.assertEqual(result.buttons, [])
                self.assertEqual(result.body, receipts[command.split()[0]])
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
            self.assertEqual(panel.body, "确认指令已停用，请直接发送管理命令。")
            self.assertEqual(panel.title, "")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(await self.store.recent_audit(self.ctx), [])

    async def test_partial_blacklist_failure_is_visible_and_audited(self):
        self.transport.result = {
            "remove_members_result": "success",
            "add_to_member_blacklist_fail_openids": ["target"],
        }
        panel = await self.manager.handle(self.ctx, "移出 target 拉黑", self.api)
        self.assertEqual(panel.title, "")
        self.assertEqual(panel.body, "已移出1名成员，其中1名拉黑失败")
        audit = (await self.store.recent_audit(self.ctx))[0]
        self.assertEqual(audit["status"], "partial")
        self.assertIn("拉黑失败：target", audit["detail"])

    async def test_removal_requires_explicit_success_and_retains_deduplication(self):
        for index, response in enumerate(
            (
                {},
                {"detail": "unrelated field"},
                {"remove_members_result": None},
                {"remove_members_result": "failure"},
                {"remove_members_result": []},
            )
        ):
            with self.subTest(response=response):
                self.transport.calls.clear()
                self.transport.result = response
                ctx = replace(self.ctx, message_id=f"incomplete-removal-{index}")
                panel = await self.manager.handle(ctx, "移出 target", self.api)
                self.assertEqual(panel.body, "结果未确认，请先查询状态。")
                self.assertEqual((await self.store.recent_audit(ctx))[0]["status"], "unknown")
                with self.assertRaises(ValueError):
                    await self.manager.handle(ctx, "移出 target", self.api)
                self.assertEqual(len(self.transport.calls), 1)

    async def test_full_batch_failed_blacklist_ids_survive_audit_without_verbose_receipt(self):
        members = [f"member{index:02d}" + "x" * 120 for index in range(20)]
        self.transport.result = {
            "remove_members_result": "success",
            "add_to_member_blacklist_fail_openids": members,
        }
        panel = await self.manager.handle(self.ctx, "移出 " + ",".join(members) + " 拉黑", self.api)
        self.assertEqual(panel.body, "已移出20名成员，其中20名拉黑失败")
        audit = (await self.store.recent_audit(self.ctx))[0]
        self.assertEqual(audit["status"], "partial")
        for member in members:
            self.assertIn(member, audit["detail"])

    async def test_long_write_result_shows_only_success_count_and_failed_members(self):
        self.transport.result = {"detail": "large reply " * 700, "fail_openids": ["failed-member"]}
        panel = await self.manager.handle(self.ctx, "拉黑 target,failed-member", self.api)
        self.assertEqual(panel.body, "已拉黑1名成员；失败成员：failed-member")
        self.assertEqual(panel.buttons, [])
        self.assertNotIn("large reply", panel.body)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "partial")

    async def test_oversize_success_result_does_not_claim_write_was_not_executed(self):
        self.transport.result = {
            "remove_members_result": "success",
            "detail": "x" * (MAX_SNAPSHOT_CHARACTERS + 1),
        }
        panel = await self.manager.handle(self.ctx, "移出 target", self.api)
        self.assertEqual(panel.title, "")
        self.assertEqual(panel.body, "已移出1名成员")
        self.assertEqual(panel.buttons, [])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "success")

    async def test_timeout_is_unknown_and_request_cannot_be_retried(self):
        self.transport.failure = QQAPIError(0, "TIMEOUT", "request timeout")
        panel = await self.manager.handle(self.ctx, "移出 target", self.api)
        self.assertEqual(panel.title, "")
        self.assertEqual(panel.body, "结果未确认，请先查询状态。")
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
        self.assertEqual(panel.body, "已禁言 @target 10分钟")
        self.assertEqual(panel.title, "")
        self.assertEqual(panel.buttons, [])
        self.assertIn(MemberMention("target"), panel.content_parts)
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
        self.assertEqual(panel.body, "结果未确认，请先查询状态。")
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")

    async def test_success_headers_with_unreadable_body_are_still_uncertain(self):
        for code in ("TIMEOUT", "NETWORK_ERROR"):
            with self.subTest(code=code):
                self.transport.calls.clear()
                self.transport.failure = QQAPIError(201, code, "response body incomplete")
                ctx = replace(self.ctx, message_id=f"body-{code}")
                panel = await self.manager.handle(ctx, "移出 target", self.api)
                self.assertEqual(panel.body, "结果未确认，请先查询状态。")
                self.assertEqual(len(self.transport.calls), 1)
                self.assertEqual((await self.store.recent_audit(ctx))[0]["status"], "unknown")

    async def test_malformed_upstream_error_code_cannot_break_audit_failure_handling(self):
        self.transport.failure = QQAPIError(503, {"unexpected": "private-value"}, "opaque")
        panel = await self.manager.handle(self.ctx, "移出 target", self.api)
        self.assertEqual(panel.body, "结果未确认，请先查询状态。")
        self.assertNotIn("private-value", panel.body)
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")

    async def test_batch_mute_receipt_has_each_real_member_and_requested_duration(self):
        panel = await self.manager.handle(self.ctx, "禁言 target,other 30m", self.api)
        self.assertEqual(panel.body, "已禁言 @target @other 30分钟")
        self.assertEqual(
            [part for part in panel.content_parts if isinstance(part, MemberMention)],
            [MemberMention("target"), MemberMention("other")],
        )
        self.assertEqual(panel.buttons, [])
        self.assertEqual(len(self.transport.calls), 1)

    async def test_partial_mute_receipt_separates_succeeded_and_failed_members(self):
        self.transport.result = {"fail_openids": ["other"]}
        panel = await self.manager.handle(self.ctx, "禁言 target,other 10m", self.api)
        self.assertEqual(panel.body, "已禁言 @target 10分钟\n禁言失败 @other")
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "partial")

    async def test_failure_for_an_unrequested_member_never_claims_success(self):
        self.transport.result = {"fail_openids": ["unrequested"]}
        panel = await self.manager.handle(self.ctx, "拉黑 target", self.api)
        self.assertEqual(panel.body, "结果未确认，请先查询状态。")
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "unknown")

    async def test_all_member_failures_are_failed_not_partial_or_success(self):
        self.transport.result = {"fail_openids": ["target", "other"]}
        panel = await self.manager.handle(self.ctx, "禁言 target,other", self.api)
        self.assertEqual(panel.body, "禁言失败 @target @other")
        self.assertEqual((await self.store.recent_audit(self.ctx))[0]["status"], "failed")

    async def test_error_reply_is_short_but_audit_retains_diagnostic(self):
        self.transport.failure = QQAPIError(
            403, 11282, "private reason", "safe-trace", request_sent=True
        )
        panel = await self.manager.handle(self.ctx, "禁言 target", self.api)
        self.assertEqual(panel.title, "")
        self.assertEqual(panel.buttons, [])
        self.assertNotIn("11282", panel.body)
        self.assertNotIn("safe-trace", panel.body)
        self.assertNotIn("private reason", panel.body)
        self.assertNotIn("\n", panel.body)
        audit = (await self.store.recent_audit(self.ctx))[0]
        self.assertEqual(audit["status"], "failed")
        self.assertIn("11282", audit["detail"])
        self.assertIn("safe-trace", audit["detail"])

    async def test_cancellation_before_and_after_write_is_audited_separately(self):
        self.transport.failure = asyncio.CancelledError()
        self.transport.next_message_sequence = AsyncMock(side_effect=asyncio.CancelledError())
        for command, status, methods in (
            ("通知 活动", "failed", []),
            ("移出 target", "unknown", ["POST"]),
        ):
            with self.subTest(command=command):
                self.transport.calls.clear()
                ctx = replace(self.ctx, message_id=f"cancel-{status}")
                with self.assertRaises(asyncio.CancelledError):
                    await self.manager.handle(ctx, command, self.api)
                self.assertEqual((await self.store.recent_audit(ctx))[0]["status"], status)
                self.assertEqual([call[0] for call in self.transport.calls], methods)
                self.assertIsNone(current_request_state.get())

    async def test_internal_failure_before_and_after_write_does_not_expose_exception(self):
        failure = RuntimeError("private credential should never be shown")
        self.transport.failure = failure
        self.transport.next_message_sequence = AsyncMock(side_effect=failure)
        for command, status, message in (
            ("通知 活动", "failed", "处理失败，未执行操作。"),
            ("移出 target", "unknown", "结果未确认，请先查询状态。"),
        ):
            ctx = replace(self.ctx, message_id=f"exception-{status}")
            panel = await self.manager.handle(ctx, command, self.api)
            self.assertEqual(panel.body, message)
            self.assertNotIn("private credential", panel.body)
            self.assertEqual((await self.store.recent_audit(ctx))[0]["status"], status)
            self.assertIsNone(current_request_state.get())

    async def test_concurrent_commands_do_not_inherit_each_others_write_state(self):
        write_started = asyncio.Event()
        finish_write = asyncio.Event()

        async def request(method, path, **kwargs):
            self.transport.calls.append((method, path, kwargs))
            write_started.set()
            await finish_write.wait()
            raise QQAPIError(0, "TIMEOUT", "write timeout")

        async def sequence(*args):
            await write_started.wait()
            raise QQAPIError(0, "TRANSPORT_CLOSED", "private reason")

        self.transport.request = AsyncMock(side_effect=request)
        self.transport.next_message_sequence = AsyncMock(side_effect=sequence)
        write_ctx = replace(self.ctx, message_id="concurrent-write")
        notice_ctx = replace(self.ctx, message_id="concurrent-notice")
        task = asyncio.create_task(self.manager.handle(write_ctx, "移出 target", self.api))
        try:
            notice = await self.manager.handle(notice_ctx, "通知 活动", self.api)
        finally:
            finish_write.set()
            write = await task
        self.assertTrue(notice.body.startswith("未执行："))
        self.assertEqual(write.body, "结果未确认，请先查询状态。")
        audit = {row["action"]: row["status"] for row in await self.store.recent_audit(self.ctx)}
        self.assertEqual(audit, {"通知": "failed", "移出": "unknown"})
        self.assertIsNone(current_request_state.get())

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

    async def test_join_approval_buttons_keep_original_row_numbers_after_invalid_rows(self):
        self.transport.result = {
            "list": [
                None,
                {"member_openid": "first", "join_request_id": "request-1"},
                {"member_openid": "second", "join_request_id": "request-2"},
            ],
        }
        panel = await self.manager.handle(self.ctx, "入群", self.api)
        self.assertIn("申请1：未返回完整申请信息", panel.body)
        buttons = {button.label: button.command for button in panel.buttons}
        self.assertNotIn("同意申请1", buttons)
        self.assertEqual(buttons["同意申请2"], "同意 first request-1")
        self.assertEqual(buttons["同意申请3"], "同意 second request-2")

    async def test_malformed_join_list_has_a_short_query_error_without_approval_buttons(self):
        for data in ({}, {"list": None}, {"list": 1}):
            with self.subTest(data=data):
                self.transport.result = data
                panel = await self.manager.handle(self.ctx, "入群", self.api)
                self.assertEqual(panel.body, "未返回入群申请列表。")
                self.assertEqual(panel.buttons, [])

    async def test_formatted_member_list_retains_every_record_and_next_api_page(self):
        ids = [f"member-{index}-" + "a" * 110 for index in range(30)]
        self.transport.result = {
            "members": [
                {"member_openid": member, "username": "成员", "member_role": "member"}
                for member in ids
            ],
            "next_cursor": "NEXT_CURSOR",
            "internal_detail": "private upstream value",
        }
        panel = await self.manager.handle(self.ctx, "成员", self.api)
        shown = panel.body
        while next_button := next(
            (button for button in panel.buttons if button.label == "下一段"), None
        ):
            panel = await self.manager.handle(self.ctx, next_button.command, self.api)
            shown += panel.body
        for member in ids:
            self.assertIn(member, shown)
        self.assertIn("成员 NEXT_CURSOR", [button.command for button in panel.buttons])
        self.assertNotIn("NEXT_CURSOR", shown)
        self.assertNotIn("internal_detail", shown)
        self.assertNotIn("private upstream value", shown)
        self.assertNotIn("{", shown)
        self.assertEqual(len(self.transport.calls), 1)

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
