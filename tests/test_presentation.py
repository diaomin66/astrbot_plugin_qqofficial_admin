"""User-facing query formatting preserves operational facts without JSON dumps."""

from __future__ import annotations

import unittest
from copy import deepcopy

from qq_admin.presentation import format_read_result


class ReadPresentationTests(unittest.TestCase):
    def test_group_information_uses_documented_fields_and_omits_internal_identifiers(self):
        data = {
            "group_name": "读书分享会",
            "group_member_num": 256,
            "group_finger_memo": "每周共读一本好书",
            "group_class_text": "文化",
            "group_tags": ["阅读", "文学"],
            "group_openid": "private-group",
            "trace_id": "private-trace",
            "private-field": {"credential": "private-token"},
        }
        original = deepcopy(data)
        shown = format_read_result("信息", data)
        self.assertIn("群名：读书分享会", shown)
        self.assertIn("人数：256", shown)
        self.assertIn("简介：每周共读一本好书", shown)
        self.assertIn("分类：文化", shown)
        self.assertIn("标签：阅读、文学", shown)
        self.assertNotIn("private", shown)
        self.assertNotIn("group_", shown)
        self.assertNotIn("{", shown)
        self.assertEqual(data, original)

    def test_bot_state_distinguishes_false_from_missing_and_translates_receive_modes(self):
        shown = format_read_result(
            "状态",
            {
                "member_role": "admin",
                "allow_proactive_msg": False,
                "recv_msg_setting": "only_mention",
                "joined_at": "2026-11-12T01:02:03Z",
                "member_openid": "private-bot-id",
            },
        )
        self.assertIn("群内身份：管理员", shown)
        self.assertIn("主动消息：不允许", shown)
        self.assertIn("仅 @机器人 的消息", shown)
        self.assertIn("2026-11-12 09:02:03（北京时间）", shown)
        self.assertNotIn("private", shown)
        missing = format_read_result("状态", {"recv_msg_setting": "unknown-mode"})
        self.assertIn("未返回主动消息设置", missing)
        self.assertNotIn("主动消息：不允许", missing)
        self.assertIn("未返回可识别的消息接收设置", missing)
        self.assertNotIn("unknown-mode", missing)

    def test_members_retain_every_record_and_full_member_ids_but_bound_nicknames(self):
        rows = [
            {
                "username": "长" * 80,
                "member_openid": f"member-{index:03d}",
                "member_role": "owner" if index == 0 else "member",
                "bot": index == 1,
                "union_openid": "private-union",
            }
            for index in range(30)
        ]
        shown = format_read_result("成员", {"members": rows, "next_cursor": "private-cursor"})
        for index in range(30):
            self.assertIn(f"成员ID：member-{index:03d}", shown)
        self.assertIn("长" * 29 + "…", shown)
        self.assertNotIn("长" * 30, shown)
        self.assertIn("群主", shown)
        self.assertIn("机器人", shown)
        self.assertIn("还有下一页", shown)
        self.assertNotIn("private", shown)

    def test_empty_and_missing_lists_are_not_confused(self):
        for command, key, empty in (
            ("成员", "members", "暂无成员"),
            ("黑名单", "users", "暂无黑名单成员"),
            ("入群", "list", "暂无待处理入群申请"),
            ("策略列表", "strategies", "暂无生效中的审批策略"),
            ("日志", "audit", "暂无操作记录"),
            ("最近消息", "messages", "最近两分钟暂无可见消息"),
        ):
            with self.subTest(command=command):
                self.assertIn(empty, format_read_result(command, {key: []}))
                for malformed in ({}, {key: None}, {key: {}}, {key: "private-value"}):
                    shown = format_read_result(command, malformed)
                    self.assertIn("未返回", shown)
                    self.assertNotIn("暂无", shown)
                    self.assertNotIn("private", shown)

    def test_member_detail_and_blacklist_use_joined_at_and_banned_at(self):
        member = {
            "username": "成员甲",
            "member_openid": "member-a",
            "member_role": "member",
            "bot": False,
            "joined_at": "2026-11-01T09:00:00+08:00",
            "banned_at": "2026-11-12T12:00:00+08:00",
            "union_openid": "private-union",
        }
        detail = format_read_result("成员详情", member)
        self.assertIn("成员甲（普通成员）", detail)
        self.assertIn("成员ID：member-a", detail)
        self.assertIn("账号类型：用户", detail)
        self.assertIn("2026-11-01 09:00:00", detail)
        self.assertNotIn("2026-11-12", detail)
        blacklist = format_read_result("黑名单", {"users": [member]})
        self.assertIn("拉黑时间：2026-11-12 12:00:00", blacklist)
        self.assertNotIn("2026-11-01", blacklist)
        self.assertNotIn("private", blacklist + detail)

    def test_mute_state_preserves_global_rules_and_every_muted_member(self):
        data = {
            "global_rule": {
                "mode": "schedule",
                "schedule_rules": [
                    {
                        "task_id": "private-task",
                        "start_at": "2026-12-01T22:00:00+08:00",
                        "end_at": "2026-12-02T07:00:00+08:00",
                        "enabled": True,
                    },
                ],
                "recurring_rules": [
                    {
                        "weekdays": [1, 3, 7],
                        "start_time": "22:00",
                        "end_time": "07:00",
                        "enabled": False,
                    },
                ],
            },
            "members": [
                {
                    "username": "成员甲",
                    "member_openid": "mute-a",
                    "mute_expire_at": "2026-12-01T23:00:00+08:00",
                },
                {
                    "username": "成员乙",
                    "member_openid": "mute-b",
                    "mute_expire_at": "2026-12-02T23:00:00+08:00",
                },
            ],
        }
        shown = format_read_result("禁言状态", data)
        for expected in (
            "全员禁言：按计划",
            "2026-12-01 22:00:00",
            "2026-12-02 07:00:00",
            "周一、周三、周日",
            "22:00 至 07:00",
            "停用",
            "启用",
            "mute-a",
            "mute-b",
        ):
            self.assertIn(expected, shown)
        self.assertNotIn("private", shown)
        missing = format_read_result("禁言状态", {})
        self.assertIn("未返回全员禁言设置", missing)
        self.assertIn("未返回成员禁言列表", missing)
        self.assertNotIn("关闭", missing)
        self.assertNotIn("暂无", missing)

    def test_join_requests_keep_complete_verification_risk_and_approval_targets(self):
        long_answer = "完整审核回答" * 1000
        request = {
            "username": "申请者",
            "member_openid": "applicant-openid",
            "join_request_id": "request-full-" + "x" * 180,
            "apply_at": "2026-11-12T12:30:00+08:00",
            "apply_source": "invited",
            "invited_by": "inviter-openid",
            "bot": False,
            "risk_tips": "存在可疑行为\n请核实身份",
            "verify_info": {
                "method": "admin_review_qa",
                "review_qa_list": [
                    {"question": "入群目的？", "answer": long_answer},
                    {"question": "同意群规？", "answer": "同意"},
                ],
            },
            "union_openid": "private-union",
            "trace_id": "private-trace",
        }
        shown = format_read_result("入群", {"list": [request]})
        for expected in (
            request["join_request_id"],
            "applicant-openid",
            "inviter-openid",
            "成员邀请",
            "存在可疑行为\n请核实身份",
            "入群目的？",
            long_answer,
            "同意群规？",
            "回答：同意",
        ):
            self.assertIn(expected, shown)
        self.assertNotIn("private", shown)
        self.assertNotIn("review_qa_list", shown)

    def test_join_verification_message_and_automatic_approval_are_visible(self):
        row = {
            "username": "申请者",
            "member_openid": "member-a",
            "join_request_id": "request-a",
            "verify_info": {"method": "verify_message", "verify_message": "希望交流学习"},
            "auto_approved": {"strategy_id": "strategy-a"},
            "risk_tips": "",
        }
        shown = format_read_result("入群", {"list": [row]})
        self.assertIn("验证消息：希望交流学习", shown)
        self.assertIn("状态：已自动通过", shown)
        self.assertIn("审批策略：strategy-a", shown)
        self.assertNotIn("风险提示：无", shown)
        row["verify_info"] = {
            "method": "unknown-method",
            "verify_message": "必须保留的验证内容",
            "review_qa_list": [{"question": "问题", "answer": "回答内容"}],
        }
        shown = format_read_result("入群", {"list": [row]})
        self.assertIn("未返回可识别的入群验证方式", shown)
        self.assertIn("必须保留的验证内容", shown)
        self.assertIn("回答内容", shown)
        self.assertNotIn("unknown-method", shown)

    def test_strategies_keep_scope_state_expiry_and_estimated_whitelist_count(self):
        rows = [
            {
                "strategy_id": "strategy-a",
                "group_openids": [f"group-{index:03d}" for index in range(100)],
                "is_enable": "on",
                "expire_at": "2027-08-05T15:30:16+08:00",
                "whitelist_user_count": 12,
                "remark": "全部关联群使用此策略",
                "updated_at": "2026-11-12T12:00:00+08:00",
                "secret": "private-value",
            },
            {
                "strategy_id": "strategy-b",
                "group_ids": [12345, "26****763"],
                "is_enable": "off",
                "expire_at": "2027-08-06T15:30:16+08:00",
                "whitelist_user_count": 0,
            },
        ]
        shown = format_read_result("策略列表", {"strategies": rows})
        self.assertIn("关联的全部群", shown)
        self.assertIn("strategy-a", shown)
        self.assertIn("strategy-b", shown)
        self.assertIn("状态：启用", shown)
        self.assertIn("状态：关闭", shown)
        self.assertIn("2027-08-05 15:30:16", shown)
        self.assertIn("2027-08-06 15:30:16", shown)
        self.assertIn("白名单：约 12 人", shown)
        self.assertIn("白名单：约 0 人", shown)
        for index in range(100):
            self.assertIn(f"group-{index:03d}", shown)
        self.assertIn("12345、26****763", shown)
        self.assertNotIn("private", shown)
        incomplete = format_read_result("策略列表", {"strategies": [{"strategy_id": "s"}]})
        self.assertIn("未返回关联群范围", incomplete)
        self.assertIn("未返回策略到期时间", incomplete)
        self.assertIn("未返回启用状态", incomplete)
        self.assertNotIn("关联群：无", incomplete)

    def test_explicit_log_query_preserves_failure_diagnostics_and_omits_success_noise(self):
        detail = "INVALID_RESPONSE\nfield=records actual=missing TraceID trace-a"
        shown = format_read_result(
            "日志",
            {
                "audit": [
                    {
                        "id": 2,
                        "action": "禁言",
                        "status": "unknown",
                        "created": 0,
                        "detail": detail,
                    },
                    {
                        "id": 1,
                        "action": "解禁",
                        "status": "success",
                        "created": 0,
                        "detail": "unnecessary-success-detail",
                    },
                ]
            },
        )
        self.assertIn("#2 禁言 · 结果未知", shown)
        self.assertIn("#1 解禁 · 成功", shown)
        self.assertIn("1970-01-01 08:00:00", shown)
        self.assertIn(detail, shown)
        self.assertNotIn("unnecessary-success-detail", shown)
        self.assertNotIn('"status"', shown)

    def test_recent_messages_keep_withdrawal_ids_authors_and_times_without_contents(self):
        shown = format_read_result(
            "最近消息",
            {
                "messages": [
                    {
                        "message_id": "message-a",
                        "member_openid": "author-a",
                        "received_at": 0,
                        "content": "private-message",
                    },
                    {"message_id": "message-b", "member_openid": "author-b", "received_at": 1},
                ]
            },
        )
        for value in ("message-a", "message-b", "author-a", "author-b", "1970-01-01 08:00:00"):
            self.assertIn(value, shown)
        self.assertNotIn("private", shown)

    def test_malformed_rows_are_retained_as_placeholders_and_never_dumped(self):
        for command, key in (
            ("成员", "members"),
            ("黑名单", "users"),
            ("入群", "list"),
            ("策略列表", "strategies"),
            ("日志", "audit"),
            ("最近消息", "messages"),
        ):
            with self.subTest(command=command):
                shown = format_read_result(command, {key: [{}, "private-value", None]})
                self.assertIn("未返回", shown)
                self.assertIn("3", shown)
                self.assertNotIn("private", shown)
                self.assertNotIn("暂无", shown)
        self.assertEqual(format_read_result("信息", None), "未返回有效查询结果。")
        with self.assertRaisesRegex(ValueError, "不支持"):
            format_read_result("unknown", {"secret": "private"})


if __name__ == "__main__":
    unittest.main()
