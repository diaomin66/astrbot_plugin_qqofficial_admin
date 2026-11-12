"""Contract and mutation-boundary tests against the published QQ API."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from qq_admin.api import QQAPIError, QQGroupAPI


class RecordingTransport:
    def __init__(self):
        self.calls = []
        self.response = {"ok": True}

    async def request(self, method, path, *, json=None, params=None):
        self.calls.append((method, path, json, params))
        return self.response


def after(**kwargs):
    return (datetime.now(UTC) + timedelta(**kwargs)).isoformat()


class APIContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport = RecordingTransport()
        self.api = QQGroupAPI(self.transport)

    async def test_all_seventeen_management_endpoints(self):
        """The official group API must never silently use guild endpoints."""
        expires = after(minutes=30)
        operations = [
            (lambda: self.api.get_group_info("g"), "GET", "/v2/groups/g/info"),
            (lambda: self.api.get_bot_state("g"), "GET", "/v2/groups/g/bot_state"),
            (lambda: self.api.list_members("g"), "GET", "/v2/groups/g/members"),
            (lambda: self.api.get_member("g", "m"), "GET", "/v2/groups/g/members/m"),
            (
                lambda: self.api.remove_members("g", ["m"]),
                "POST",
                "/v2/groups/g/batch_remove_members",
            ),
            (lambda: self.api.list_blacklist("g"), "GET", "/v2/groups/g/member_blacklist"),
            (
                lambda: self.api.update_blacklist("g", "add", ["m"]),
                "POST",
                "/v2/groups/g/member_blacklist",
            ),
            (lambda: self.api.get_mute_state("g"), "GET", "/v2/groups/g/restrict_chat_setting"),
            (
                lambda: self.api.set_member_mutes(
                    "g", [{"op": "add", "member_openid": "m", "mute_expire_at": expires}]
                ),
                "POST",
                "/v2/groups/g/restrict_chat_setting",
            ),
            (lambda: self.api.list_join_requests("g"), "GET", "/v2/groups/g/join_request_list"),
            (
                lambda: self.api.approve_join_request(
                    "g", "m", "approve", join_request_id="request"
                ),
                "POST",
                "/v2/groups/g/approval_join_request/m",
            ),
            (lambda: self.api.list_strategies(), "GET", "/v2/groups/join_approval_strategy"),
            (
                lambda: self.api.create_strategy(group_openids=["g"]),
                "POST",
                "/v2/groups/join_approval_strategy",
            ),
            (
                lambda: self.api.update_strategy("s", is_enable="off"),
                "PATCH",
                "/v2/groups/join_approval_strategy/s",
            ),
            (
                lambda: self.api.delete_strategy("s"),
                "DELETE",
                "/v2/groups/join_approval_strategy/s",
            ),
            (
                lambda: self.api.execute_strategy("s"),
                "POST",
                "/v2/groups/join_approval_strategy/s/execute",
            ),
            (
                lambda: self.api.update_strategy_whitelist("s", "add", ["123456789"]),
                "POST",
                "/v2/groups/join_approval_strategy/s/whitelist_users",
            ),
        ]
        for call, method, path in operations:
            with self.subTest(path=path, method=method):
                self.assertEqual(await call(), {"ok": True})
                self.assertEqual(self.transport.calls[-1][:2], (method, path))
        self.assertEqual(len(self.transport.calls), 17)

    async def test_pagination_preserves_cursor_and_uses_query_parameters(self):
        self.transport.response = {"members": [], "next_cursor": "opaque+/="}
        result = await self.api.list_members("g", cursor="opaque+/=")
        self.assertEqual(result["next_cursor"], "opaque+/=")
        self.assertEqual(self.transport.calls[-1][2:], (None, {"cursor": "opaque+/="}))
        await self.api.list_blacklist("g", cursor="next", limit=100)
        self.assertEqual(self.transport.calls[-1][3], {"cursor": "next", "limit": 100})
        await self.api.list_join_requests("g", limit=50)
        self.assertEqual(self.transport.calls[-1][3]["limit"], 50)
        await self.api.list_strategies(limit=50)
        self.assertEqual(self.transport.calls[-1][3]["limit"], 50)
        for call in (
            lambda: self.api.list_blacklist("g", limit=101),
            lambda: self.api.list_blacklist("g", limit=True),
            lambda: self.api.list_join_requests("g", limit=51),
            lambda: self.api.list_strategies(limit=0),
        ):
            with self.assertRaises(ValueError):
                await call()
        self.assertEqual(len(self.transport.calls), 4)

    async def test_member_mutation_cap_and_explicit_blacklist_result(self):
        self.transport.response = {
            "remove_members_result": "success",
            "add_to_member_blacklist_fail_openids": ["m1"],
        }
        result = await self.api.remove_members("g", ["m1", "m2"], add_to_blacklist=True)
        self.assertEqual(result["add_to_member_blacklist_fail_openids"], ["m1"])
        self.assertEqual(
            self.transport.calls[-1][2],
            {"member_openids": ["m1", "m2"], "add_to_member_blacklist": True},
        )
        await self.api.update_blacklist("g", "del", [f"m{i}" for i in range(20)])
        for call in (
            lambda: self.api.remove_members("g", []),
            lambda: self.api.remove_members("g", ["m", "m"]),
            lambda: self.api.remove_members("g", "member"),
            lambda: self.api.remove_members("g", [f"m{i}" for i in range(21)]),
            lambda: self.api.remove_members("g", ["m"], add_to_blacklist="false"),
            lambda: self.api.update_blacklist("g", "remove", ["m"]),
        ):
            with self.assertRaises(ValueError):
                await call()
        self.assertEqual(len(self.transport.calls), 2)

    async def test_member_mute_add_update_delete_and_thirty_day_limit(self):
        expires = after(days=29, hours=23)
        records = [
            {"op": "add", "member_openid": "m1", "mute_expire_at": expires},
            {"op": "update", "member_openid": "m2", "mute_expire_at": expires},
            {"op": "del", "member_openid": "m3"},
        ]
        await self.api.set_member_mutes("g", records)
        self.assertEqual(
            self.transport.calls[-1][2]["members"][2],
            {"op": "del", "member_openid": "m3", "mute_expire_at": ""},
        )
        self.assertNotIn("mute_expire_at", records[2], "The caller's input must not be mutated")
        for expires in (
            after(days=31),
            after(minutes=-1),
            "2026-11-11T12:00:00",
            "2026-99-11T12:00:00+08:00",
            "",
        ):
            with self.subTest(expires=expires), self.assertRaises(ValueError):
                await self.api.set_member_mutes(
                    "g", [{"op": "add", "member_openid": "m", "mute_expire_at": expires}]
                )
        for records in (
            [],
            [{"op": "del", "member_openid": "m", "global_rule": {"mode": "always"}}],
            [{"op": "del", "member_openid": "m", "mute_expire_at": after(hours=1)}],
            [{"op": "del", "member_openid": "m"}] * 21,
            [{"op": "del", "member_openid": "m"}] * 2,
        ):
            with self.assertRaises(ValueError):
                await self.api.set_member_mutes("g", records)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_approval_requires_original_request_id_and_valid_decision(self):
        await self.api.approve_join_request("g", "m", "approve", join_request_id="opaque_request")
        self.assertEqual(
            self.transport.calls[-1][2], {"op": "approve", "join_request_id": "opaque_request"}
        )
        await self.api.approve_join_request(
            "g",
            "m",
            "decline",
            join_request_id="other",
            reject_reason="验证信息不完整",
            add_to_blacklist=True,
        )
        self.assertEqual(
            self.transport.calls[-1][2],
            {
                "op": "decline",
                "join_request_id": "other",
                "reject_reason": "验证信息不完整",
                "add_to_member_blacklist": True,
            },
        )
        for kwargs in (
            {"op": "approve", "join_request_id": ""},
            {"op": "accept", "join_request_id": "request"},
            {"op": "approve", "join_request_id": "request", "add_to_blacklist": True},
            {"op": "approve", "join_request_id": "request", "reject_reason": "reason"},
        ):
            with self.assertRaises(ValueError):
                await self.api.approve_join_request("g", "m", **kwargs)
        self.assertEqual(len(self.transport.calls), 2)

    async def test_strategy_targets_are_exclusive_and_use_documented_types(self):
        await self.api.create_strategy(group_openids=["g"], remark="示例", is_enable="off")
        self.assertEqual(
            self.transport.calls[-1][2],
            {"group_openids": ["g"], "is_enable": "off", "remark": "示例"},
        )
        await self.api.create_strategy(group_ids=[12345678])
        self.assertEqual(self.transport.calls[-1][2]["group_ids"], [12345678])
        for kwargs in (
            {},
            {"group_openids": ["g"], "group_ids": [12345678]},
            {"group_openids": []},
            {"group_openids": [f"g{i}" for i in range(101)]},
            {"group_ids": ["12345678"]},
            {"group_ids": [True]},
            {"group_ids": [2**64]},
            {"group_openids": ["g"], "is_enable": True},
            {"group_openids": ["g"], "remark": "字" * 256},
            {"group_openids": ["g"], "expire_at": "tomorrow"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.api.create_strategy(**kwargs)
        self.assertEqual(len(self.transport.calls), 2)

    async def test_strategy_update_execute_and_whitelist_limits(self):
        await self.api.update_strategy(
            "s", group_action={"op": "del", "group_openids": ["g"]}, remark=""
        )
        self.assertEqual(
            self.transport.calls[-1][2],
            {"group_action": {"op": "del", "group_openids": ["g"]}, "remark": ""},
        )
        await self.api.execute_strategy("s")
        self.assertEqual(self.transport.calls[-1][2], {})
        await self.api.update_strategy_whitelist("s", "add", [str(10000 + i) for i in range(10000)])
        self.assertEqual(len(self.transport.calls[-1][2]["whitelist_users"]), 10000)
        for call in (
            lambda: self.api.update_strategy("s"),
            lambda: self.api.update_strategy(
                "s", group_action={"op": "add", "group_ids": [1], "unknown": True}
            ),
            lambda: self.api.update_strategy(
                "s", group_action={"op": "add", "group_ids": [1], "group_openids": ["g"]}
            ),
            lambda: self.api.update_strategy_whitelist(
                "s", "add", [str(10000 + i) for i in range(10001)]
            ),
            lambda: self.api.update_strategy_whitelist("s", "add", [12345]),
            lambda: self.api.update_strategy_whitelist("s", "add", ["openid"]),
            lambda: self.api.update_strategy_whitelist("s", "add", ["12345", "12345"]),
            lambda: self.api.update_strategy_whitelist("s", "add", [str(2**64)]),
        ):
            with self.assertRaises(ValueError):
                await call()
        self.assertEqual(len(self.transport.calls), 3)

    async def test_route_segments_are_escaped_and_no_content_delete_flags_are_added(self):
        await self.api.get_member("g/../x", "m?#")
        self.assertEqual(self.transport.calls[-1][1], "/v2/groups/g%2F..%2Fx/members/m%3F%23")
        self.transport.response = None
        self.assertEqual(await self.api.delete_message("g", "message/1"), {})
        self.assertEqual(
            self.transport.calls[-1], ("DELETE", "/v2/groups/g/messages/message%2F1", None, None)
        )
        for group in ("", "..", "bad\nheader"):
            with self.assertRaises(ValueError):
                await self.api.get_group_info(group)

    async def test_group_native_panel_endpoints_and_response_fields(self):
        panel = {
            "items": [
                {
                    "type": "command",
                    "name": "/群管 信息",
                    "desc": "查看本群信息",
                    "only_admin": True,
                },
                {"type": "link", "name": "帮助", "link": "https://example.com/help"},
            ],
            "remark": "qqofficial_admin.command_menu.v1",
        }
        self.transport.response = {"records": [], "next_cursor": "opaque+/=", "is_end": False}
        result = await self.api.list_panels(cursor="current+/=", limit=50)
        self.assertEqual(result["next_cursor"], "opaque+/=")
        self.assertEqual(
            self.transport.calls[-1],
            ("GET", "/v2/panels", None, {"scope": "group", "cursor": "current+/=", "limit": 50}),
        )
        self.transport.response = {"panel_id": "p-owned"}
        self.assertEqual((await self.api.create_panel(["group-a"], panel))["panel_id"], "p-owned")
        self.assertEqual(
            self.transport.calls[-1],
            (
                "POST",
                "/v2/panels",
                {
                    "scope": "group",
                    "target_type": "specific",
                    "group_openids": ["group-a"],
                    "panel": panel,
                },
                None,
            ),
        )
        self.transport.response = {
            "panel_id": "p-owned",
            "scope": "group",
            "target_type": "specific",
            "panel": panel,
            "group_openids": ["group-a"],
            "version": 2,
        }
        self.assertEqual((await self.api.get_panel("p-owned"))["group_openids"], ["group-a"])
        self.assertEqual(self.transport.calls[-1], ("GET", "/v2/panels/p-owned", None, None))
        self.transport.response = {"version": 3}
        self.assertEqual((await self.api.update_panel("p-owned", panel))["version"], 3)
        self.assertEqual(
            self.transport.calls[-1], ("PUT", "/v2/panels/p-owned", {"panel": panel}, None)
        )
        self.transport.response = None
        self.assertEqual(await self.api.update_panel_groups("p-owned", "add", ["group-b"]), {})
        self.assertEqual(
            self.transport.calls[-1],
            ("PUT", "/v2/panels/p-owned/target", {"op": "add", "group_openids": ["group-b"]}, None),
        )
        self.assertEqual(await self.api.delete_panel("p-owned"), {})
        self.assertEqual(self.transport.calls[-1], ("DELETE", "/v2/panels/p-owned", None, None))

    async def test_panel_command_uses_name_and_rejects_undocumented_template_fields(self):
        command = {
            "type": "command",
            "name": "/群管 取消拉黑",
            "desc": "补充成员 OpenID",
            "only_admin": True,
        }
        panel = {"items": [command], "remark": "owner-marker", "version": 1}
        await self.api.create_panel(["g"], panel)
        sent = self.transport.calls[-1][2]
        self.assertEqual(sent["panel"]["items"][0]["name"], "/群管 取消拉黑")
        command["name"] = "changed"
        self.assertEqual(sent["panel"]["items"][0]["name"], "/群管 取消拉黑")
        for item in (
            {"type": "command", "name": "/群管 信息", "data": "separate command"},
            {"type": "command", "name": "/群管 信息", "enter": True},
            {"type": "command", "name": "/群管 信息", "params": ["member"]},
            {"type": "command", "name": "/群管 策略白名单"},
            {"type": "command", "name": "查询", "desc": "字" * 16},
            {"type": "command", "name": "查询", "only_admin": "true"},
            {"type": "command", "name": "查询", "link": "https://example.com"},
            {"type": "link", "name": "帮助", "link": "http://example.com"},
            {"type": "link", "name": "帮助", "link": "https://"},
            {"type": "link", "name": "帮助"},
            {"type": "callback", "name": "操作"},
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                await self.api.update_panel("p", {"items": [item]})
        self.assertEqual(len(self.transport.calls), 1)

    async def test_panel_limits_targets_and_ids_are_checked_before_sending(self):
        panel = {"items": [{"type": "command", "name": "/群管 信息"}]}
        await self.api.create_panel([f"g{i}" for i in range(20)], {"items": panel["items"] * 20})
        await self.api.update_panel_groups("p/../?#", "del", ["g"])
        self.assertEqual(self.transport.calls[-1][1], "/v2/panels/p%2F..%2F%3F%23/target")
        for call in (
            lambda: self.api.list_panels(limit=51),
            lambda: self.api.list_panels(limit=True),
            lambda: self.api.create_panel([], panel),
            lambda: self.api.create_panel(["g", "g"], panel),
            lambda: self.api.create_panel("g", panel),
            lambda: self.api.create_panel([f"g{i}" for i in range(21)], panel),
            lambda: self.api.create_panel(["g"], {"items": []}),
            lambda: self.api.create_panel(["g"], {"items": panel["items"] * 21}),
            lambda: self.api.create_panel(["g"], {**panel, "remark": "x" * 256}),
            lambda: self.api.create_panel(["g"], {**panel, "version": True}),
            lambda: self.api.create_panel(["g"], {**panel, "target_type": "all"}),
            lambda: self.api.update_panel_groups("p", "remove", ["g"]),
            lambda: self.api.update_panel_groups("p", "add", [f"g{i}" for i in range(21)]),
            lambda: self.api.get_panel(".."),
            lambda: self.api.delete_panel("bad\nheader"),
        ):
            with self.assertRaises(ValueError):
                await call()
        self.assertEqual(len(self.transport.calls), 2)

    async def test_markdown_buttons_and_at_everyone_preserve_official_payload(self):
        payload = {
            "msg_type": 2,
            "markdown": {"content": "<qqbot-at-everyone />\n群公告"},
            "keyboard": {
                "content": {
                    "rows": [
                        {
                            "buttons": [
                                {
                                    "id": "safe",
                                    "action": {
                                        "type": 1,
                                        "data": "nonce",
                                        "permission": {"type": 0, "specify_user_ids": ["m"]},
                                    },
                                }
                            ]
                        }
                    ]
                }
            },
            "event_id": "interaction-event",
            "msg_seq": 1,
        }
        await self.api.send_message("g", payload)
        self.assertEqual(self.transport.calls[-1], ("POST", "/v2/groups/g/messages", payload, None))
        payload["markdown"]["content"] = "changed"
        self.assertIn("<qqbot-at-everyone />", self.transport.calls[-1][2]["markdown"]["content"])
        for payload in (
            {"msg_type": 0, "content": ""},
            {"msg_type": 2, "content": "text", "markdown": {"content": "md"}},
            {"msg_type": 2, "markdown": {"content": "md"}, "msg_id": "m", "event_id": "e"},
            {"msg_type": 0, "content": "text", "msg_seq": 0},
            {
                "msg_type": 0,
                "content": "text",
                "keyboard": {"id": "template", "content": {"rows": []}},
            },
            {"msg_type": 0, "content": "text", "stream": True},
            {"msg_type": 0, "content": "text", "markdown": {"content": "md"}},
        ):
            with self.assertRaises(ValueError):
                await self.api.send_message("g", payload)
        self.assertEqual(len(self.transport.calls), 1)

    def test_error_display_explains_permission_without_echoing_secrets(self):
        error = QQAPIError(403, 11253, "Authorization: QQBot highly-secret", "trace_123")
        self.assertIn("白名单", error.user_message())
        self.assertIn("11253", error.user_message())
        self.assertIn("trace_123", error.user_message())
        self.assertNotIn("highly-secret", str(error))
        self.assertNotIn("highly-secret", repr(error))
        self.assertIn("频繁", QQAPIError(429, None, "opaque").user_message())
        self.assertNotIn(
            "Authorization",
            QQAPIError(
                500, "Authorization:secret", "secret", "Authorization:secret"
            ).user_message(),
        )


if __name__ == "__main__":
    unittest.main()
