"""Member command normalization against actual QQ payload and component shapes."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from qq_admin.commands import DIRECT_COMMANDS, normalize_command


def event_for(text, *, mentions=(), content=None, components=(), raw_data=None):
    raw = SimpleNamespace(content=text if content is None else content, mentions=mentions)
    if raw_data is not None:
        raw.raw_data = raw_data
    return SimpleNamespace(
        get_message_str=lambda: text,
        get_self_id=lambda: "bot-app-id",
        get_messages=lambda: components,
        message_obj=SimpleNamespace(raw_message=raw),
    )


def component(kind, **fields):
    value = type(kind, (), {})()
    for key, field in fields.items():
        setattr(value, key, field)
    return value


class CommandTests(unittest.TestCase):
    def test_direct_and_legacy_commands_normalize_to_same_command(self):
        for prefix in ("/", "", "/群管 ", "群管 ", "/qqadmin ", "qqadmin "):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    normalize_command(event_for(prefix + "禁言 <@member-1> 10m")),
                    "禁言 member-1 10m",
                )
        self.assertEqual(normalize_command(event_for("/群管")), "面板")
        self.assertTrue({"禁言", "解禁", "身份", "我的ID", "确认", "取消"} <= DIRECT_COMMANDS)

    def test_native_markup_and_legacy_mentions_use_group_identity(self):
        for markup in ('<qqbot-at-user id="target-user" />', "<@target-user>", "<@!target-user>"):
            with self.subTest(markup=markup):
                event = event_for(
                    "/禁言 " + markup + " 30分钟",
                    mentions=[{"id": "target-user", "member_openid": "target-member"}],
                )
                self.assertEqual(normalize_command(event), "禁言 target-member 30分钟")

    def test_default_duration_and_multiple_mentions_fold_to_comma_list(self):
        event = event_for(
            '/禁言 <qqbot-at-user id="first" /><qqbot-at-user id="second" />',
            mentions=[
                {"id": "first", "member_openid": "member-first"},
                {"id": "second", "member_openid": "member-second"},
            ],
        )
        self.assertEqual(normalize_command(event), "禁言 member-first,member-second")
        self.assertEqual(normalize_command(event_for("/禁言 <@member-1>")), "禁言 member-1")

    def test_multiple_members_and_explicit_ids_are_deduplicated(self):
        event = event_for(
            "/禁言 member-1,<@alias-1> <@member-2> member-3 1h",
            mentions=[{"id": "alias-1", "member_openid": "member-1"}],
        )
        self.assertEqual(normalize_command(event), "禁言 member-1,member-2,member-3 1h")

    def test_unicode_whitespace_and_comma_separators_are_consumed(self):
        for space in ("\v", "\u2003", "\u3000", "\u00a0"):
            with self.subTest(space=repr(space)):
                event = event_for(f"/禁言 <@member-1>,{space}，{space}<@member-2>")
                self.assertEqual(normalize_command(event), "禁言 member-1,member-2")

    def test_invalid_duration_is_not_promoted_to_another_member(self):
        for value in ("typo", "10min", "0", "-1h"):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_command(event_for(f"/禁言 <@member-1> {value}")),
                    f"禁言 member-1 {value}",
                )
        self.assertEqual(
            normalize_command(event_for("/禁言 typo", mentions=[{"id": "member-1"}])),
            "禁言 member-1 typo",
        )

    def test_duration_between_targets_is_rejected(self):
        for duration in ("10m", "600", "30分钟", "10min", "-10", "1.5h", "10x"):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, "所有 @成员 之后"):
                    normalize_command(event_for(f"/禁言 <@member-1> {duration} <@member-2>"))
        self.assertEqual(normalize_command(event_for("/禁言 <@10m>")), "禁言 10m")

    def test_explicit_mention_positions_do_not_expand_from_other_metadata(self):
        for command in ("禁言", "解禁", "成员详情", "移出"):
            with self.subTest(command=command):
                event = event_for(
                    f"/{command} <@alias-1>",
                    mentions=[
                        {"id": "alias-1", "member_openid": "member-1"},
                        {"id": "alias-2", "member_openid": "member-2"},
                    ],
                )
                self.assertEqual(normalize_command(event), f"{command} member-1")

    def test_numeric_group_openid_is_not_a_default_duration(self):
        member_id = "12345678901234567890123456789012"
        for records in ([], [{"id": member_id}]):
            with self.subTest(mentions=records):
                self.assertEqual(
                    normalize_command(event_for(f"/禁言 {member_id}", mentions=records)),
                    f"禁言 {member_id}",
                )
        self.assertEqual(normalize_command(event_for(f"/禁言 <@{member_id}>")), f"禁言 {member_id}")

    def test_bot_wake_mention_is_removed_even_when_it_differs_from_app_id(self):
        event = event_for(
            '<qqbot-at-user id="bot-user" /> /禁言 <@target> 10m',
            mentions=[
                {"id": "bot-user", "member_openid": "bot-member", "is_you": True},
                {"id": "target", "member_openid": "member-1"},
            ],
        )
        self.assertEqual(normalize_command(event), "禁言 member-1 10m")

    def test_bot_in_target_list_is_excluded_and_bot_only_target_is_rejected(self):
        records = [{"id": "bot-user", "member_openid": "bot-member", "bot": True}]
        self.assertEqual(
            normalize_command(event_for("/禁言 <@bot-user> <@member-1> 10m", mentions=records)),
            "禁言 member-1 10m",
        )
        with self.assertRaises(ValueError):
            normalize_command(event_for("/禁言 <@bot-user> 10m", mentions=records))

    def test_component_positions_restore_at_removed_from_message_string(self):
        event = event_for(
            "/禁言 10m",
            content="",
            components=[
                component("At", qq="bot-app-id"),
                component("Plain", text="/禁言 "),
                component("At", qq="alias-1"),
                component("At", qq="member-2"),
                component("Plain", text=" 10m"),
            ],
            mentions=[{"id": "alias-1", "member_openid": "member-1"}],
        )
        self.assertEqual(normalize_command(event), "禁言 member-1,member-2 10m")

    def test_metadata_only_mentions_support_default_and_explicit_duration(self):
        for body, expected in (("", "member-1"), ("10m", "member-1 10m")):
            event = event_for(
                "/禁言 " + body,
                mentions=[{"id": "alias-1", "member_openid": "member-1"}],
            )
            self.assertEqual(normalize_command(event), "禁言 " + expected)

    def test_raw_data_recovers_native_mention_after_sdk_omission(self):
        event = event_for(
            "/禁言 @张三 10m",
            content="",
            raw_data={
                "content": '/禁言 <qqbot-at-user id="alias-1" /> 10m',
                "mentions": [{"id": "alias-1", "member_openid": "member-1"}],
            },
        )
        self.assertEqual(normalize_command(event), "禁言 member-1 10m")

    def test_typed_nickname_is_never_guessed_even_with_other_actual_mentions(self):
        for text in ("/禁言 @张三 10m", "/禁言 @name 10m", "/解禁 @", "/移出 @成员 拉黑"):
            for records in ([], [{"id": "different-member", "username": "张三"}]):
                with self.subTest(text=text, mentions=records):
                    with self.assertRaisesRegex(ValueError, "选择成员"):
                        normalize_command(event_for(text, mentions=records))

    def test_explicit_ids_without_mentions_keep_existing_grammar(self):
        for text in (
            "禁言 member-1,member-2 10m",
            "禁言 member-1",
            "解禁 member-1,member-2",
            "拒绝 member-1 request-1 请联系 @管理员",
            "成员详情 member-1",
        ):
            self.assertEqual(normalize_command(event_for("/" + text)), text)

    def test_member_operations_keep_flags_and_duration(self):
        for command in ("解禁", "拉黑", "取消拉黑", "成员详情"):
            with self.subTest(command=command):
                self.assertEqual(
                    normalize_command(event_for(f"/{command} <@member-1>")), f"{command} member-1"
                )
        self.assertEqual(
            normalize_command(event_for("/移出 <@member-1> 拉黑")), "移出 member-1 拉黑"
        )
        self.assertEqual(
            normalize_command(event_for("/更新禁言 <@member-1> 7d")), "更新禁言 member-1 7d"
        )

    def test_rejection_changes_target_only_and_preserves_reason_mentions(self):
        event = event_for(
            "/拒绝 <@alias-1> request-1 --拉黑 请联系 <@other-member>  查看理由",
            mentions=[{"id": "alias-1", "member_openid": "member-1"}],
        )
        self.assertEqual(
            normalize_command(event),
            "拒绝 member-1 request-1 --拉黑 请联系 <@other-member>  查看理由",
        )
        self.assertEqual(
            normalize_command(event_for("/同意 <@member-1> request-1")), "同意 member-1 request-1"
        )

    def test_component_target_before_request_does_not_consume_rejection_reason(self):
        event = event_for(
            "/拒绝 request-1 请联系 @管理员",
            content="",
            components=[
                component("Plain", text="/拒绝 "),
                component("At", qq="member-1"),
                component("Plain", text=" request-1 请联系 @管理员"),
            ],
        )
        self.assertEqual(normalize_command(event), "拒绝 member-1  request-1 请联系 @管理员")

    def test_notification_and_json_payloads_are_not_rewritten(self):
        for text in (
            "通知 请 @成员  参加活动\n下一行",
            '策略创建 {"remark": "通知 @昵称", "group_openids": ["member-1"]}',
            '策略修改 strategy-1 {"remark":"<@member-1> 保留  空格"}',
            "全体通知 开会",
            "策略白名单 strategy-1 add 123456,789012",
        ):
            self.assertEqual(normalize_command(event_for("/" + text)), text)


if __name__ == "__main__":
    unittest.main()
