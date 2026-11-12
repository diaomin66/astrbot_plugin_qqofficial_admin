import json
import unittest
from types import SimpleNamespace

from qq_admin.pagination import MAX_SNAPSHOT_CHARACTERS, ResultPager
from qq_admin.panels import MAX_MARKDOWN_LENGTH, ButtonSpec, build_payload, render_plain_panel


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.pager = ResultPager(clock=lambda: self.now)
        self.ctx = SimpleNamespace(
            platform_id="bot-a",
            group_id="group-a",
            user_id="member-a",
            is_superuser=False,
        )

    @staticmethod
    def next_command(panel):
        for button in panel.buttons:
            if button.label == "下一段":
                _, token, index = button.command.split()
                return token, int(index)
        return None

    def collect(self, first):
        panels = [first]
        while (command := self.next_command(panels[-1])) is not None:
            panels.append(self.pager.page(self.ctx, *command))
        return panels

    def test_complete_member_api_page_remains_accessible_without_skipping_records(self):
        data = {
            "members": [
                {"member_openid": f"member-{index:02}", "name": f"成员{index:02}" + "长名字" * 80}
                for index in range(30)
            ],
            "next_cursor": "NEXT_BACKEND_CURSOR",
        }
        first = self.pager.show(
            self.ctx, "群成员", data, [ButtonSpec("接口下一页", "成员 NEXT_BACKEND_CURSOR")]
        )
        panels = self.collect(first)
        self.assertGreater(len(panels), 1)
        restored = "".join(panel.body for panel in panels)
        self.assertEqual(restored, json.dumps(data, ensure_ascii=False, indent=2))
        for index in range(30):
            self.assertIn(f"member-{index:02}", restored)
        for panel in panels:
            self.assertIn(ButtonSpec("接口下一页", "成员 NEXT_BACKEND_CURSOR"), panel.buttons)

    def test_one_very_long_escaped_value_is_lossless_and_every_panel_fits(self):
        text = "<qqbot-at-everyone />&_[]\\*中文😀" * 600
        panels = self.collect(self.pager.show(self.ctx, "特殊字符", text))
        self.assertGreater(len(panels), 2)
        self.assertEqual("".join(panel.body for panel in panels), text)
        for panel in panels:
            payload = build_payload(panel, self.ctx, lambda action, data: "encoded")
            content = payload["markdown"]["content"]
            self.assertLessEqual(len(content), MAX_MARKDOWN_LENGTH)
            self.assertNotIn("<qqbot-", content)

    def test_snapshot_is_bound_to_user_group_and_platform(self):
        first = self.pager.show(self.ctx, "成员", {"value": "x" * 7000})
        token, index = self.next_command(first)
        for name, replacement in (
            ("platform_id", "bot-b"),
            ("group_id", "group-b"),
            ("user_id", "member-b"),
        ):
            other = SimpleNamespace(**{**vars(self.ctx), name: replacement})
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.pager.page(other, token, index)
        self.pager.page(self.ctx, token, index)

    def test_expiry_is_fixed_and_reading_does_not_extend_it(self):
        first = self.pager.show(self.ctx, "成员", "x" * 7000)
        token, index = self.next_command(first)
        self.now = 1299.0
        self.pager.page(self.ctx, token, index)
        self.now = 1300.0
        with self.assertRaisesRegex(ValueError, "过期"):
            self.pager.page(self.ctx, token, index)

    def test_bounded_cache_evicts_oldest_snapshot(self):
        pager = ResultPager(max_snapshots=2, clock=lambda: self.now)
        first = pager.show(self.ctx, "最早", "a" * 7000)
        first_token, first_index = self.next_command(first)
        second = pager.show(self.ctx, "第二", "b" * 7000)
        second_token, second_index = self.next_command(second)
        pager.show(self.ctx, "第三", "c" * 7000)
        with self.assertRaises(ValueError):
            pager.page(self.ctx, first_token, first_index)
        pager.page(self.ctx, second_token, second_index)

    def test_page_index_cannot_be_negative_bool_or_outside_snapshot(self):
        first = self.pager.show(self.ctx, "成员", "x" * 7000)
        token, _ = self.next_command(first)
        for invalid in (-1, True, "1", 10**6):
            with self.subTest(index=invalid), self.assertRaises(ValueError):
                self.pager.page(self.ctx, token, invalid)

    def test_data_mutation_after_show_does_not_change_cached_result(self):
        data = {"value": "x" * 7000}
        first = self.pager.show(self.ctx, "成员", data)
        token, index = self.next_command(first)
        before = self.pager.page(self.ctx, token, index)
        data["value"] = "replaced"
        self.assertEqual(self.pager.page(self.ctx, token, index).body, before.body)

    def test_global_strategy_snapshot_cannot_be_read_after_superuser_revocation(self):
        self.ctx.is_superuser = True
        first = self.pager.show(self.ctx, "全局策略", "x" * 7000, global_operation=True)
        token, index = self.next_command(first)
        self.pager.page(self.ctx, token, index)
        self.ctx.is_superuser = False
        with self.assertRaisesRegex(ValueError, "全局策略"):
            self.pager.page(self.ctx, token, index)
        with self.assertRaisesRegex(ValueError, "全局策略"):
            self.pager.show(self.ctx, "全局策略", {}, global_operation=True)

    def test_ordinary_snapshot_does_not_require_superuser_after_role_change(self):
        self.ctx.is_superuser = True
        first = self.pager.show(self.ctx, "群信息", "x" * 7000)
        token, index = self.next_command(first)
        self.ctx.is_superuser = False
        self.pager.page(self.ctx, token, index)

    def test_explicit_limits_never_silently_drop_text_or_actions(self):
        with self.assertRaisesRegex(ValueError, "过大"):
            self.pager.show(self.ctx, "结果", "x" * (MAX_SNAPSHOT_CHARACTERS + 1))
        with self.assertRaisesRegex(ValueError, "10"):
            self.pager.show(self.ctx, "结果", {}, [ButtonSpec("按钮", "身份")] * 11)

    def test_clear_releases_existing_result_links(self):
        first = self.pager.show(self.ctx, "成员", "x" * 7000)
        token, index = self.next_command(first)
        self.pager.clear()
        with self.assertRaises(ValueError):
            self.pager.page(self.ctx, token, index)

    def test_single_empty_page_and_back_navigation_work(self):
        panel = self.pager.show(self.ctx, "成员", {"members": [], "next_cursor": ""}, buttons=[])
        self.assertEqual(panel.title, "成员")
        self.assertEqual(panel.body, json.dumps({"members": [], "next_cursor": ""}, indent=2))
        self.assertEqual(panel.buttons, [])
        first = self.pager.show(self.ctx, "长结果", "x" * 7000)
        second = self.pager.page(self.ctx, *self.next_command(first))
        previous = next(button for button in second.buttons if button.label == "上一段")
        _, token, index = previous.command.split()
        self.assertEqual(self.pager.page(self.ctx, token, int(index)).body, first.body)

    def test_single_text_result_has_no_pagination_noise_and_keeps_next_api_page(self):
        text = "小明 · 管理员\n成员ID：member-1\n还有下一页"
        next_page = ButtonSpec("下一页", "成员 opaque-cursor")
        panel = self.pager.show(self.ctx, "群成员", text, [next_page])
        self.assertEqual(panel.title, "群成员")
        self.assertEqual(panel.body, text)
        self.assertEqual(panel.buttons, [next_page])
        content = build_payload(panel, self.ctx, lambda *_: "encoded")["markdown"]["content"]
        for removed in ("1/1", "结果分页", "5 分钟", "opaque-cursor"):
            self.assertNotIn(removed, content)
        self.assertEqual(render_plain_panel(panel).count("/成员 opaque-cursor"), 1)

    def test_multiple_segments_only_add_position_and_navigation_buttons(self):
        text = "一条完整记录<&>\n" * 2000
        panels = self.collect(self.pager.show(self.ctx, "成员", text, buttons=[]))
        self.assertGreater(len(panels), 1)
        self.assertEqual("".join(panel.body for panel in panels), text)
        for index, panel in enumerate(panels):
            self.assertEqual(panel.title, f"成员（第 {index + 1}/{len(panels)} 段）")
            for removed in ("结果分页", "5 分钟", "上一段：", "下一段：", "/结果 "):
                self.assertNotIn(removed, panel.body)
            self.assertEqual(
                {button.label for button in panel.buttons},
                ({"上一段"} if index else set())
                | ({"下一段"} if index + 1 < len(panels) else set()),
            )
            plain = render_plain_panel(panel)
            for button in panel.buttons:
                self.assertEqual(plain.count("/" + button.command), 1)


if __name__ == "__main__":
    unittest.main()
