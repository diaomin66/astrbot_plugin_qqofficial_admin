import json
import unittest
from types import SimpleNamespace

from qq_admin.panels import (
    MAX_MARKDOWN_LENGTH,
    ButtonSpec,
    Panel,
    build_payload,
    escape_markdown,
    help_panel,
    home_panel,
    result_panel,
)


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.ctx = SimpleNamespace(
            platform_id="qq-production",
            group_id="group-openid",
            user_id="member-openid",
            is_superuser=False,
        )
        self.encoded_calls = []

    def encode(self, action, data):
        self.encoded_calls.append((action, data))
        return json.dumps({"action": action, "data": data}, ensure_ascii=False)

    def buttons(self, payload):
        return [
            button for row in payload["keyboard"]["content"]["rows"] for button in row["buttons"]
        ]

    def test_callback_is_bound_to_panel_owner_group_and_platform(self):
        payload = build_payload(home_panel(), self.ctx, self.encode)
        buttons = self.buttons(payload)
        self.assertEqual(len({button["id"] for button in buttons}), len(buttons))
        for button in buttons:
            self.assertEqual(
                button["action"]["permission"],
                {"type": 0, "specify_user_ids": ["member-openid"]},
            )
        for handler, data in self.encoded_calls:
            self.assertEqual(handler, "qqadmin")
            self.assertEqual(data["platform_id"], "qq-production")
            self.assertEqual(data["group_id"], "group-openid")
            self.assertEqual(data["owner_id"], "member-openid")
        for transport_field in ("msg_id", "event_id", "msg_seq"):
            self.assertNotIn(transport_field, payload)

    def test_group_input_button_never_sends_automatically(self):
        panel = Panel("输入操作", "请填写目标", [ButtonSpec("填写禁言", "禁言 ", "input")])
        button = self.buttons(build_payload(panel, self.ctx, self.encode))[0]
        self.assertEqual(button["action"]["type"], 2)
        self.assertEqual(button["action"]["data"], "/群管 禁言 ")
        self.assertIs(button["action"]["enter"], False)
        self.assertEqual(self.encoded_calls, [])

    def test_untrusted_result_cannot_create_mentions_links_or_command_tags(self):
        text = '<qqbot-at-everyone />\n<qqbot-cmd-enter text="/danger" /> [go](https://x.invalid)'
        content = build_payload(
            result_panel("<qqbot-at-user id='x' />", text), self.ctx, self.encode
        )["markdown"]["content"]
        self.assertNotIn("<qqbot-", content)
        self.assertNotIn("[go](", content)
        self.assertIn("&lt;qqbot", content)
        self.assertIn("\\[go\\]", content)
        self.assertEqual(escape_markdown("&lt;tag&gt;"), "&amp;lt;tag&amp;gt;")

    def test_danger_confirmation_details_are_never_silently_truncated(self):
        panel = Panel(
            "确认移出",
            "成员目标：" + "目标" * 4000,
            [ButtonSpec("确认移出", "确认 abc", danger=True)],
        )
        with self.assertRaisesRegex(ValueError, "确认操作"):
            build_payload(panel, self.ctx, self.encode)
        self.assertEqual(self.encoded_calls, [])

    def test_danger_buttons_have_native_confirmation_and_exclusive_group(self):
        specs = [
            ButtonSpec("确认移出", "确认 abc", danger=True),
            ButtonSpec("确认拉黑", "确认 xyz", danger=True),
        ]
        buttons = self.buttons(
            build_payload(Panel("确认", "请核对目标", specs), self.ctx, self.encode)
        )
        self.assertEqual(buttons[0]["group_id"], buttons[1]["group_id"])
        for button in buttons:
            modal = button["action"]["modal"]
            self.assertLessEqual(len(modal["content"]), 40)
            self.assertLessEqual(len(modal["confirm_text"]), 4)
            self.assertLessEqual(len(modal["cancel_text"]), 4)
            self.assertNotIn("http", modal["content"])

    def test_long_results_keep_next_cursor_and_respect_escaped_size_budget(self):
        data = {"members": [{"name": "<&_*" * 1000}], "next_cursor": "cursor<&_9"}
        panel = result_panel("群成员", data)
        self.assertIn("已省略", panel.body)
        self.assertIn("next_cursor：cursor<&_9", panel.body)
        payload = build_payload(panel, self.ctx, self.encode)
        self.assertLessEqual(len(payload["markdown"]["content"]), MAX_MARKDOWN_LENGTH)
        self.assertNotIn("<", payload["markdown"]["content"])

    def test_full_keyboard_fits_without_losing_actions(self):
        specs = [ButtonSpec(str(index), f"成员 {index}") for index in range(25)]
        payload = build_payload(Panel("测试", "", specs), self.ctx, self.encode)
        rows = payload["keyboard"]["content"]["rows"]
        self.assertLessEqual(len(rows), 5)
        self.assertTrue(all(len(row["buttons"]) <= 5 for row in rows))
        self.assertEqual(len(self.buttons(payload)), 25)
        with self.assertRaisesRegex(ValueError, "25"):
            build_payload(Panel("测试", "", specs + [specs[0]]), self.ctx, self.encode)

    def test_missing_owner_never_creates_a_public_management_button(self):
        self.ctx.user_id = ""
        with self.assertRaisesRegex(ValueError, "操作者"):
            build_payload(home_panel(), self.ctx, self.encode)

    def test_help_and_empty_page_are_renderable(self):
        build_payload(help_panel(), self.ctx, self.encode)
        panel = result_panel("群成员", {"members": [], "next_cursor": ""}, buttons=[])
        self.assertIn("最后一页", panel.body)
        self.assertNotIn("keyboard", build_payload(panel, self.ctx, self.encode))


if __name__ == "__main__":
    unittest.main()
