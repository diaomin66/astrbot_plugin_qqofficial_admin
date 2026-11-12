"""Contract and mutation-boundary tests against the published QQ API."""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from qq_admin.api import QQAPIError, QQGroupAPI, QQResponse
from qq_admin.diagnostics import RequestState, current_request_state, route_template


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

    async def test_compatible_transport_marks_only_writes_after_parameter_validation(self):
        state = RequestState()
        token = current_request_state.set(state)
        try:
            await self.api.get_group_info("g")
            self.assertFalse(state.mutation_started)
            with self.assertRaises(ValueError):
                await self.api.remove_members("g", [])
            self.assertFalse(state.mutation_started)
            await self.api.remove_members("g", ["m"])
            self.assertTrue(state.mutation_started)
            await self.api.get_group_info("g")
            self.assertTrue(state.mutation_started)
        finally:
            current_request_state.reset(token)

    async def test_transport_with_precise_tracking_owns_the_send_boundary(self):
        self.transport.tracks_mutations = True
        state = RequestState()
        token = current_request_state.set(state)
        try:
            await self.api.remove_members("g", ["m"])
            self.assertFalse(state.mutation_started)
        finally:
            current_request_state.reset(token)

    async def test_concurrent_commands_have_independent_mutation_states(self):
        write_started, read_started = asyncio.Event(), asyncio.Event()
        observed = {}

        async def request(method, path, **kwargs):
            if method == "POST":
                write_started.set()
                await read_started.wait()
            else:
                await write_started.wait()
                read_started.set()
            await asyncio.sleep(0)
            observed[method] = current_request_state.get().mutation_started
            return {}

        self.transport.request = request

        async def execute(write):
            state = RequestState()
            token = current_request_state.set(state)
            try:
                if write:
                    await self.api.remove_members("g", ["m"])
                else:
                    await self.api.get_group_info("g")
                return state
            finally:
                current_request_state.reset(token)

        parent_state = current_request_state.get()
        write, read = await asyncio.gather(execute(True), execute(False))
        self.assertIsNot(write, read)
        self.assertEqual(observed, {"POST": True, "GET": False})
        self.assertIs(current_request_state.get(), parent_state)

    async def test_compatible_errors_get_request_context_without_claiming_a_send(self):
        error = QQAPIError(0, "AUTH_UNAVAILABLE", "private credential", request_sent=False)
        self.transport.request = AsyncMock(side_effect=error)
        with self.assertRaises(QQAPIError) as caught:
            await self.api.remove_members("private-group", ["private-member"])
        error = caught.exception
        self.assertEqual(error.method, "POST")
        self.assertEqual(error.route, "/v2/groups/{group_openid}/batch_remove_members")
        self.assertFalse(error.request_sent)
        self.assertEqual(str(error), error.user_message())
        self.assertNotIn("private", repr(error))

    async def test_response_metadata_is_preserved_and_legacy_empty_results_are_wrapped(self):
        response = QQResponse(
            {"panel_id": "p"},
            status=201,
            trace_id="trace-create",
            method="POST",
            route="/v2/panels",
        )
        self.transport.response = response
        self.assertIs(await self.api._request("POST", "/v2/panels"), response)
        self.transport.response = None
        empty = await self.api.delete_panel("p")
        self.assertIsInstance(empty, QQResponse)
        self.assertEqual(empty, {})
        self.assertEqual(empty.status, 0)
        self.assertEqual(empty.method, "DELETE")
        self.assertEqual(empty.route, "/v2/panels/{panel_id}")

    async def test_compatible_transport_non_object_response_has_safe_type_diagnostics(self):
        self.transport.response = ["private response text"]
        with self.assertRaises(QQAPIError) as caught:
            await self.api.get_group_info("private-group")
        error = caught.exception
        self.assertEqual(error.reason, "root_type")
        self.assertEqual(error.expected, "object")
        self.assertEqual(error.actual, "array")
        self.assertTrue(error.request_sent)
        self.assertNotIn("private", error.user_message())

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


class DiagnosticTests(unittest.TestCase):
    def test_error_code_field_type_diagnostics_survive_request_context_enrichment(self):
        for field in ("err_code", "code"):
            with self.subTest(field=field):
                error = QQAPIError(
                    200,
                    "INVALID_RESPONSE",
                    "private response body",
                    reason="field_type",
                    field=field,
                    expected="integer_or_string_or_null",
                    actual="object",
                ).with_request_context(method="GET", route="/v2/panels", request_sent=True)
                self.assertEqual(error.field, field)
                self.assertEqual(error.expected, "integer_or_string_or_null")
                self.assertEqual(error.actual, "object")
                self.assertIn(f"field={field}", error.diagnostic_text())
                self.assertEqual(str(error), error.user_message())
                self.assertNotIn("private", repr(error))

    def test_all_documented_routes_use_fixed_templates(self):
        routes = {
            "/v2/groups/private-group/info": "/v2/groups/{group_openid}/info",
            "/v2/groups/private-group/bot_state": "/v2/groups/{group_openid}/bot_state",
            "/v2/groups/private-group/members": "/v2/groups/{group_openid}/members",
            "/v2/groups/private-group/members/private-member": (
                "/v2/groups/{group_openid}/members/{member_openid}"
            ),
            "/v2/groups/private-group/batch_remove_members": (
                "/v2/groups/{group_openid}/batch_remove_members"
            ),
            "/v2/groups/private-group/member_blacklist": (
                "/v2/groups/{group_openid}/member_blacklist"
            ),
            "/v2/groups/private-group/restrict_chat_setting": (
                "/v2/groups/{group_openid}/restrict_chat_setting"
            ),
            "/v2/groups/private-group/join_request_list": (
                "/v2/groups/{group_openid}/join_request_list"
            ),
            "/v2/groups/private-group/approval_join_request/private-member": (
                "/v2/groups/{group_openid}/approval_join_request/{member_openid}"
            ),
            "/v2/groups/join_approval_strategy": "/v2/groups/join_approval_strategy",
            "/v2/groups/join_approval_strategy/private-strategy": (
                "/v2/groups/join_approval_strategy/{strategy_id}"
            ),
            "/v2/groups/join_approval_strategy/private-strategy/execute": (
                "/v2/groups/join_approval_strategy/{strategy_id}/execute"
            ),
            "/v2/groups/join_approval_strategy/private-strategy/whitelist_users": (
                "/v2/groups/join_approval_strategy/{strategy_id}/whitelist_users"
            ),
            "/v2/panels": "/v2/panels",
            "/v2/panels/private-panel": "/v2/panels/{panel_id}",
            "/v2/panels/private-panel/target": "/v2/panels/{panel_id}/target",
            "/v2/groups/private-group/messages": "/v2/groups/{group_openid}/messages",
            "/v2/groups/private-group/messages/private-message": (
                "/v2/groups/{group_openid}/messages/{message_id}"
            ),
        }
        for path, expected in routes.items():
            with self.subTest(path=path):
                self.assertEqual(route_template(path), expected)
                self.assertNotIn("private", route_template(path))
        for path in (
            "/private-unknown-route",
            "/v2/panels?cursor=private",
            "/v2/panels/private\n",
            "/v2/panels/private/unknown",
            "https://api.bot.qq.com/v2/panels",
        ):
            with self.subTest(path=path):
                self.assertEqual(route_template(path), "unknown")

    def test_field_diagnostics_do_not_echo_response_or_command_values(self):
        error = QQAPIError(
            200,
            "INVALID_RESPONSE",
            "private notification body and credential",
            trace_id="trace_123",
            method="GET",
            route="/v2/panels",
            reason="field_type",
            field="records[].panel",
            expected="object",
            actual="null",
            request_sent=True,
        )
        for expected in (
            "GET /v2/panels",
            "HTTP=200",
            "INVALID_RESPONSE",
            "trace_123",
            "reason=field_type",
            "field=records[].panel",
            "expected=object",
            "actual=null",
            "request_sent=true",
        ):
            self.assertIn(expected, error.diagnostic_text())
        self.assertIn(error.diagnostic_text(), error.user_message())
        self.assertEqual(str(error), error.user_message())
        self.assertNotIn("private", repr(error))
        for code in ("INVALID_RESPONSE", "TIMEOUT", "NETWORK_ERROR"):
            self.assertNotIn("结果尚未确认", QQAPIError(0, code, "ignored").user_message())

    def test_untrusted_diagnostic_labels_and_literal_routes_are_discarded(self):
        error = QQAPIError(
            9999,
            "Authorization: private-token",
            "private-body",
            trace_id="private\ntrace",
            method="POST private-data",
            route="/v2/panels/private-panel",
            reason="private-reason",
            field="records.private-key",
            expected="private-expectation",
            actual="private-value",
            request_sent="private-value",
        )
        self.assertEqual(error.status, 0)
        self.assertEqual(error.route, "unknown")
        self.assertIsNone(error.request_sent)
        self.assertNotIn("private", error.user_message())
        self.assertNotIn("private", error.diagnostic_text())
        self.assertNotIn("private", repr(error))

    def test_response_metadata_does_not_change_business_json(self):
        result = QQResponse(
            {"records": []},
            status=200,
            trace_id="trace-1",
            method="GET",
            route="/v2/panels",
        )
        self.assertEqual(json.loads(json.dumps(result)), {"records": []})
        self.assertEqual(result.status, 200)
        self.assertEqual(result.trace_id, "trace-1")
        result = QQResponse({}, status=True, trace_id="invalid trace", route="/private-id")
        self.assertEqual(result.status, 0)
        self.assertEqual(result.trace_id, "")
        self.assertEqual(result.route, "unknown")


if __name__ == "__main__":
    unittest.main()
