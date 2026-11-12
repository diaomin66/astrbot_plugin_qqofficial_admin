import unittest

from qq_admin.menu import group_command_items


def qq_text_length(text):
    """Conservatively count non-ASCII characters as two for QQ menu budgets."""
    return sum(1 if ord(character) < 128 else 2 for character in text)


class GroupCommandMenuTests(unittest.TestCase):
    def test_menu_contains_daily_group_commands_with_official_fields(self):
        items = group_command_items()
        self.assertEqual(len(items), 20)
        self.assertEqual(
            {item["name"] for item in items},
            {
                "/禁言",
                "/更新禁言",
                "/解禁",
                "/移出",
                "/成员",
                "/成员详情",
                "/入群",
                "/黑名单",
                "/拉黑",
                "/取消拉黑",
                "/禁言状态",
                "/信息",
                "/状态",
                "/通知",
                "/全体通知",
                "/最近消息",
                "/撤回",
                "/日志",
                "/菜单",
                "/帮助",
            },
        )
        for item in items:
            self.assertEqual(set(item), {"type", "name", "desc", "only_admin"})
            self.assertEqual(item["type"], "command")
            self.assertIsInstance(item["only_admin"], bool)
            self.assertLessEqual(qq_text_length(item["name"]), 14)
            self.assertLessEqual(qq_text_length(item["desc"]), 30)

    def test_only_navigation_items_are_visible_to_everyone(self):
        for item in group_command_items():
            self.assertEqual(item["only_admin"], item["name"] not in {"/菜单", "/帮助"})

    def test_command_names_prefill_no_fake_mentions_or_confirmation_step(self):
        items = group_command_items()
        for item in items:
            self.assertNotIn("@", item["name"])
            self.assertNotIn(" ", item["name"])
            self.assertNotIn("确认", item["desc"])
        mute = next(item for item in items if item["name"] == "/禁言")
        self.assertIn("@成员", mute["desc"])
        self.assertIn("默认10分钟", mute["desc"])

    def test_callers_cannot_modify_future_menu_definitions(self):
        first = group_command_items()
        first[0]["only_admin"] = False
        first[0]["name"] = "/changed"
        first.pop()
        second = group_command_items()
        self.assertEqual(len(second), 20)
        self.assertEqual(second[0]["name"], "/禁言")
        self.assertTrue(second[0]["only_admin"])


if __name__ == "__main__":
    unittest.main()
