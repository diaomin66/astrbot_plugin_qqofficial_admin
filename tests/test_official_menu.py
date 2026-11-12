"""Stateful tests for current-group QQ native panel installation and ownership."""

from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace

from qq_admin.api import QQAPIError, QQResponse
from qq_admin.menu import group_command_items
from qq_admin.official_menu import OfficialMenu


def context(*, group_id="group-a", platform_id="bot-a"):
    return SimpleNamespace(platform_id=platform_id, group_id=group_id, user_id="admin-a")


class StatefulPanelAPI:
    """An in-memory QQ API with realistic list and top-level detail fields."""

    def __init__(self, *, page_size=20):
        self.panels = {}
        self.calls = []
        self.page_size = page_size
        self.pages = None
        self.detail_overrides = {}
        self.create_response = None
        self.update_response = None
        self.apply_creates = True
        self.apply_updates = True
        self.after_mutation = None
        self.read_error_after_mutation = None
        self.next_id = 1

    def seed(self, panel_id, *, group_id="group-a", remark="unrelated-plugin", items=None):
        detail = {
            "panel_id": panel_id,
            "scope": "group",
            "target_type": "specific",
            "group_openids": [group_id],
            "version": 1,
            "panel": {
                "remark": remark,
                "items": deepcopy(items if items is not None else group_command_items()),
            },
        }
        self.panels[panel_id] = detail
        return detail

    @staticmethod
    def record(detail):
        # The list response does not include target group_openids. The service
        # must fetch and validate detail before replacing or deleting a panel.
        return {
            "panel_id": detail["panel_id"],
            "panel": deepcopy(detail["panel"]),
            "version": detail["version"],
        }

    async def list_panels(self, *, cursor="", limit=20):
        self.calls.append(("list", cursor, limit))
        await asyncio.sleep(0)
        if self.read_error_after_mutation is not None and self.mutations():
            raise self.read_error_after_mutation
        if self.pages is not None:
            return deepcopy(self.pages[cursor])
        start = int(cursor.removeprefix("offset:")) if cursor else 0
        records = [self.record(detail) for detail in self.panels.values()]
        end = min(len(records), start + min(limit, self.page_size))
        is_end = end >= len(records)
        return {
            "records": records[start:end],
            "next_cursor": "" if is_end else f"offset:{end}",
            "is_end": is_end,
        }

    async def get_panel(self, panel_id):
        self.calls.append(("get", panel_id))
        await asyncio.sleep(0)
        if self.read_error_after_mutation is not None and self.mutations():
            raise self.read_error_after_mutation
        return deepcopy(self.detail_overrides.get(panel_id, self.panels[panel_id]))

    async def create_panel(self, group_openids, panel):
        self.calls.append(("create", deepcopy(group_openids), deepcopy(panel)))
        await asyncio.sleep(0)
        panel_id = f"created-{self.next_id}"
        self.next_id += 1
        if self.apply_creates:
            self.panels[panel_id] = {
                "panel_id": panel_id,
                "scope": "group",
                "target_type": "specific",
                "group_openids": list(group_openids),
                "version": 1,
                "panel": deepcopy(panel),
            }
        if self.after_mutation is not None:
            self.after_mutation()
        if isinstance(self.create_response, BaseException):
            raise self.create_response
        return (
            deepcopy(self.create_response)
            if self.create_response is not None
            else {"panel_id": panel_id}
        )

    async def update_panel(self, panel_id, panel):
        self.calls.append(("update", panel_id, deepcopy(panel)))
        await asyncio.sleep(0)
        if self.apply_updates:
            self.panels[panel_id]["panel"] = deepcopy(panel)
            self.panels[panel_id]["version"] += 1
        if self.after_mutation is not None:
            self.after_mutation()
        if isinstance(self.update_response, BaseException):
            raise self.update_response
        return (
            deepcopy(self.update_response)
            if self.update_response is not None
            else {"version": self.panels[panel_id]["version"]}
        )

    async def delete_panel(self, panel_id):
        self.calls.append(("delete", panel_id))
        await asyncio.sleep(0)
        del self.panels[panel_id]
        return {}

    def mutations(self):
        return [call for call in self.calls if call[0] in {"create", "update", "delete"}]


class OfficialMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = context()
        self.menu = OfficialMenu()
        self.api = StatefulPanelAPI()

    def seed_owned(self, *, panel_id="owned", group_id="group-a", items=None):
        return self.api.seed(
            panel_id,
            group_id=group_id,
            remark=OfficialMenu._marker(group_id),
            items=items,
        )

    async def test_first_install_creates_twenty_commands_for_only_current_group(self):
        self.api.seed("other-plugin", group_id="group-a")
        self.seed_owned(panel_id="other-group", group_id="group-b")
        unrelated = deepcopy(self.api.panels)
        result = await self.menu.install(self.ctx, self.api)
        self.assertEqual(result["状态"], "已创建")
        self.assertEqual(result["指令数"], 20)
        created = self.api.panels[result["panel_id"]]
        self.assertEqual(created["group_openids"], ["group-a"])
        self.assertEqual(created["scope"], "group")
        self.assertEqual(created["target_type"], "specific")
        self.assertEqual(created["panel"]["items"], group_command_items())
        self.assertEqual(created["panel"]["remark"], OfficialMenu._marker("group-a"))
        self.assertEqual([call[0] for call in self.api.mutations()], ["create"])
        for panel_id, expected in unrelated.items():
            self.assertEqual(self.api.panels[panel_id], expected)

    async def test_same_application_in_two_adapter_instances_shares_install_lock(self):
        self.api.transport = SimpleNamespace(application_id="shared-qq-bot")
        contexts = (context(platform_id="websocket"), context(platform_id="webhook"))
        results = await asyncio.gather(*(self.menu.install(ctx, self.api) for ctx in contexts))
        self.assertEqual(results[0]["panel_id"], results[1]["panel_id"])
        self.assertEqual([call[0] for call in self.api.mutations()], ["create"])

    async def test_update_without_valid_version_reads_back_once_and_confirms_items(self):
        for response in (
            {},
            {"version": None},
            {"version": True},
            {"version": "2"},
            {"version": -1},
        ):
            api = StatefulPanelAPI()
            api.seed(
                "owned",
                remark=OfficialMenu._marker("group-a"),
                items=[{"type": "command", "name": "/legacy"}],
            )
            api.update_response = response
            with self.subTest(response=response):
                result = await self.menu.install(self.ctx, api)
                self.assertEqual(result["状态"], "已更新")
                self.assertIn("已通过查询核实", result["说明"])
                self.assertEqual([call[0] for call in api.calls], ["list", "get", "update", "get"])
                self.assertEqual([call[0] for call in api.mutations()], ["update"])

    async def test_reinstall_and_recreated_service_find_existing_panel_without_new_writes(self):
        initial = await self.menu.install(self.ctx, self.api)
        again = await self.menu.install(self.ctx, self.api)
        restarted = await OfficialMenu().install(self.ctx, self.api)
        self.assertEqual(again["状态"], "已是最新")
        self.assertEqual(restarted["状态"], "已是最新")
        self.assertEqual(initial["panel_id"], again["panel_id"])
        self.assertEqual(initial["panel_id"], restarted["panel_id"])
        self.assertEqual([call[0] for call in self.api.mutations()], ["create"])

    async def test_concurrent_installs_in_one_group_create_only_one_panel(self):
        results = await asyncio.gather(*(self.menu.install(self.ctx, self.api) for _ in range(16)))
        self.assertEqual(len({result["panel_id"] for result in results}), 1)
        self.assertEqual(sum(result["状态"] == "已创建" for result in results), 1)
        self.assertEqual([call[0] for call in self.api.mutations()], ["create"])
        self.assertEqual(len(self.api.panels), 1)

    async def test_old_owned_menu_is_updated_without_changing_targets_or_other_panels(self):
        self.seed_owned(items=[{"type": "command", "name": "/legacy"}])
        self.api.seed("foreign", group_id="group-b")
        foreign = deepcopy(self.api.panels["foreign"])
        result = await self.menu.install(self.ctx, self.api)
        self.assertEqual(result["状态"], "已更新")
        self.assertEqual(result["panel_id"], "owned")
        self.assertEqual(self.api.panels["owned"]["group_openids"], ["group-a"])
        self.assertEqual(self.api.panels["owned"]["panel"]["items"], group_command_items())
        self.assertEqual(self.api.panels["foreign"], foreign)
        self.assertEqual([call[0] for call in self.api.mutations()], ["update"])
        self.assertLess(self.api.calls.index(("get", "owned")), len(self.api.calls) - 1)

    async def test_paginated_search_finds_owned_panel_on_later_page(self):
        self.api.page_size = 1
        self.api.seed("foreign")
        self.seed_owned(panel_id="other-group", group_id="group-b")
        self.seed_owned()
        result = await self.menu.install(self.ctx, self.api)
        self.assertEqual(result["panel_id"], "owned")
        self.assertEqual(result["状态"], "已是最新")
        self.assertEqual(
            [call[1] for call in self.api.calls if call[0] == "list"],
            ["", "offset:1", "offset:2"],
        )
        self.assertEqual([call for call in self.api.calls if call[0] == "get"], [("get", "owned")])
        self.assertEqual(self.api.mutations(), [])

    async def test_status_reads_only_current_group_plugin_panel(self):
        self.api.seed("foreign")
        self.seed_owned(panel_id="other-group", group_id="group-b")
        self.seed_owned()
        result = await self.menu.status(self.ctx, self.api)
        self.assertEqual(result["panel_id"], "owned")
        self.assertEqual(result["commands"], group_command_items())
        self.assertEqual(result["version"], 1)
        self.assertEqual([call for call in self.api.calls if call[0] == "get"], [("get", "owned")])
        self.assertEqual(self.api.mutations(), [])

    async def test_status_does_not_substitute_another_groups_plugin_panel(self):
        self.seed_owned(panel_id="other-group", group_id="group-b")
        result = await self.menu.status(self.ctx, self.api)
        self.assertNotIn("panel_id", result)
        self.assertFalse(any(call[0] == "get" for call in self.api.calls))
        self.assertEqual(self.api.mutations(), [])

    async def test_remove_deletes_only_owned_single_group_panel_and_is_repeatable(self):
        self.api.seed("foreign")
        self.seed_owned(panel_id="other-group", group_id="group-b")
        self.seed_owned()
        expected_remaining = {
            key: deepcopy(value) for key, value in self.api.panels.items() if key != "owned"
        }
        result = await self.menu.remove(self.ctx, self.api)
        self.assertEqual(result["panel_id"], "owned")
        self.assertEqual(self.api.panels, expected_remaining)
        self.assertEqual(self.api.mutations(), [("delete", "owned")])
        repeated = await self.menu.remove(self.ctx, self.api)
        self.assertNotIn("panel_id", repeated)
        self.assertEqual(self.api.mutations(), [("delete", "owned")])

    async def test_detail_scope_target_type_marker_and_id_must_match_before_any_mutation(self):
        valid = self.seed_owned(items=[{"type": "command", "name": "/legacy"}])
        variants = [
            {**valid, "scope": "c2c"},
            {**valid, "target_type": "all"},
            {**valid, "panel_id": "another-id"},
            {**valid, "panel": {**valid["panel"], "remark": "changed-owner"}},
            {**valid, "panel": None},
        ]
        for detail in variants:
            self.api.detail_overrides["owned"] = deepcopy(detail)
            for operation in (self.menu.install, self.menu.remove, self.menu.status):
                with self.subTest(detail=detail, operation=operation.__name__):
                    with self.assertRaises(QQAPIError):
                        await operation(self.ctx, self.api)
                    self.assertEqual(self.api.mutations(), [])

    async def test_multiple_or_changed_group_targets_are_never_updated_or_deleted(self):
        valid = self.seed_owned(items=[{"type": "command", "name": "/legacy"}])
        for targets in (["group-a", "group-b"], ["group-b"], [], None):
            self.api.detail_overrides["owned"] = {**deepcopy(valid), "group_openids": targets}
            for operation in (self.menu.install, self.menu.remove):
                with self.subTest(targets=targets, operation=operation.__name__):
                    with self.assertRaises(QQAPIError) as caught:
                        await operation(self.ctx, self.api)
                    self.assertEqual(caught.exception.field, "group_openids")
                    self.assertEqual(self.api.mutations(), [])

    async def test_duplicate_plugin_markers_on_different_pages_refuse_all_changes(self):
        self.api.page_size = 1
        self.seed_owned(panel_id="duplicate-one")
        self.seed_owned(panel_id="duplicate-two")
        for operation in (self.menu.install, self.menu.remove, self.menu.status):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                await operation(self.ctx, self.api)
        self.assertEqual(self.api.mutations(), [])
        self.assertFalse(any(call[0] == "get" for call in self.api.calls))

    async def test_incomplete_pagination_cannot_be_treated_as_a_complete_search(self):
        cases = [
            {"records": [], "is_end": False, "next_cursor": ""},
            {"records": [], "is_end": False},
            {"records": []},
            {"records": [], "is_end": "false", "next_cursor": ""},
            {"records": [], "is_end": 1, "next_cursor": ""},
            {"records": [], "is_end": None, "next_cursor": ""},
            {"records": [], "is_end": True, "next_cursor": None},
            {"records": [], "is_end": True, "next_cursor": 0},
            {"records": [], "is_end": True, "next_cursor": "unexpected-next"},
        ]
        for response in cases:
            self.api.pages = {"": response}
            with self.subTest(response=response), self.assertRaises(QQAPIError):
                await self.menu.install(self.ctx, self.api)
        self.assertEqual(self.api.mutations(), [])

    async def test_each_explicit_completion_signal_can_finish_empty_search(self):
        for response in (
            {"records": [], "next_cursor": ""},
            {"records": [], "is_end": True},
        ):
            api = StatefulPanelAPI()
            api.pages = {"": response}
            with self.subTest(response=response):
                result = await self.menu.install(self.ctx, api)
                self.assertEqual(result["状态"], "已创建")
                self.assertEqual([call[0] for call in api.calls], ["list", "create"])

    async def test_mixed_pagination_fields_continue_and_find_later_owned_panel(self):
        owned = self.seed_owned()
        for final in ({"is_end": True}, {"next_cursor": ""}):
            self.api.calls.clear()
            self.api.pages = {
                "": {"records": [], "next_cursor": "second"},
                "second": {"records": [], "next_cursor": "last", "is_end": False},
                "last": {"records": [self.api.record(owned)], **final},
            }
            with self.subTest(final=final):
                result = await self.menu.install(self.ctx, self.api)
                self.assertEqual(result["状态"], "已是最新")
                self.assertEqual(
                    [call[1] for call in self.api.calls if call[0] == "list"],
                    ["", "second", "last"],
                )
                self.assertEqual(self.api.mutations(), [])

    async def test_cursor_loop_and_excessive_pages_do_not_create_a_panel(self):
        self.api.pages = {
            "": {"records": [], "is_end": False, "next_cursor": "loop"},
            "loop": {"records": [], "is_end": False, "next_cursor": "loop"},
        }
        with self.assertRaises(QQAPIError):
            await self.menu.install(self.ctx, self.api)
        self.assertEqual(self.api.mutations(), [])
        self.api.pages = {
            "" if number == 0 else f"cursor-{number}": {
                "records": [],
                "is_end": False,
                "next_cursor": f"cursor-{number + 1}",
            }
            for number in range(22)
        }
        with self.assertRaises(QQAPIError):
            await self.menu.install(self.ctx, self.api)
        self.assertEqual(self.api.mutations(), [])

    async def test_invalid_list_shape_or_owned_record_without_id_does_not_create(self):
        for response in (
            {"records": None, "is_end": True, "next_cursor": ""},
            {"records": ["invalid"], "is_end": True, "next_cursor": ""},
            {"records": [{"panel_id": "incomplete"}], "is_end": True, "next_cursor": ""},
            {
                "records": [{"panel": {"remark": OfficialMenu._marker("group-a")}}],
                "is_end": True,
                "next_cursor": "",
            },
        ):
            self.api.pages = {"": response}
            with self.subTest(response=response), self.assertRaises(QQAPIError):
                await self.menu.install(self.ctx, self.api)
        self.assertEqual(self.api.mutations(), [])

    async def test_create_without_panel_id_reads_all_pages_and_confirms_one_write(
        self,
    ):
        for response in ({}, {"panel_id": ""}, {"panel_id": " "}, {"panel_id": 3}):
            api = StatefulPanelAPI(page_size=1)
            api.seed("foreign")
            api.create_response = response
            with self.subTest(response=response):
                result = await self.menu.install(self.ctx, api)
                self.assertEqual(result["状态"], "已创建")
                self.assertIn("已通过查询核实", result["说明"])
                self.assertEqual(
                    [call[0] for call in api.calls], ["list", "create", "list", "list", "get"]
                )
                self.assertEqual(result["panel_id"], "created-1")
                repeated = await self.menu.install(self.ctx, api)
                self.assertEqual(repeated["状态"], "已是最新")
                self.assertEqual([call[0] for call in api.mutations()], ["create"])

    async def test_transport_invalid_response_can_be_verified_without_repeating_write(self):
        for operation, method, route in (
            ("create", "POST", "/v2/panels"),
            ("update", "PUT", "/v2/panels/{panel_id}"),
        ):
            api = StatefulPanelAPI()
            if operation == "update":
                api.seed("owned", remark=OfficialMenu._marker("group-a"), items=[])
            original = QQAPIError(
                201 if operation == "create" else 200,
                "INVALID_RESPONSE",
                "unparseable response",
                trace_id="original-write-trace",
                method=method,
                route=route,
                request_sent=True,
            )
            setattr(api, operation + "_response", original)
            with self.subTest(operation=operation):
                result = await self.menu.install(self.ctx, api)
                self.assertEqual(result["状态"], "已创建" if operation == "create" else "已更新")
                self.assertIn("已通过查询核实", result["说明"])
                self.assertEqual([call[0] for call in api.mutations()], [operation])

    async def test_unapplied_create_or_update_stays_unknown_with_write_diagnostics(self):
        for operation in ("create", "update"):
            api = StatefulPanelAPI()
            if operation == "update":
                api.seed("owned", remark=OfficialMenu._marker("group-a"), items=[])
            setattr(api, "apply_" + operation + "s", False)
            setattr(api, operation + "_response", {})
            with self.subTest(operation=operation), self.assertRaises(QQAPIError) as caught:
                await self.menu.install(self.ctx, api)
            error = caught.exception
            self.assertEqual(error.code, "INVALID_RESPONSE")
            self.assertEqual(error.status, 0)  # Plain dicts carry no HTTP status.
            self.assertEqual(error.method, "POST" if operation == "create" else "PUT")
            self.assertEqual(error.field, "panel_id" if operation == "create" else "version")
            self.assertEqual(error.actual, "missing")
            self.assertTrue(error.request_sent)
            self.assertEqual([call[0] for call in api.mutations()], [operation])

    async def test_readback_failure_preserves_original_write_error_and_metadata(self):
        for operation in ("create", "update"):
            for read_error in (
                QQAPIError(0, "TIMEOUT", "read timed out", method="GET"),
                ValueError("ambiguous panel ownership"),
            ):
                api = StatefulPanelAPI()
                if operation == "update":
                    api.seed("owned", remark=OfficialMenu._marker("group-a"), items=[])
                original = QQAPIError(
                    201 if operation == "create" else 200,
                    "INVALID_RESPONSE",
                    "invalid write acknowledgement",
                    trace_id="write-trace",
                    method="POST" if operation == "create" else "PUT",
                    route="/v2/panels" if operation == "create" else "/v2/panels/{panel_id}",
                    request_sent=True,
                )
                setattr(api, operation + "_response", original)
                api.read_error_after_mutation = read_error
                with self.subTest(operation=operation, read_error=type(read_error).__name__):
                    with self.assertRaises(QQAPIError) as caught:
                        await self.menu.install(self.ctx, api)
                    self.assertIs(caught.exception, original)
                    self.assertEqual(caught.exception.trace_id, "write-trace")
                    self.assertEqual([call[0] for call in api.mutations()], [operation])

    async def test_parsed_write_metadata_survives_failed_readback(self):
        for operation, method, route, status, field in (
            ("create", "POST", "/v2/panels", 201, "panel_id"),
            ("update", "PUT", "/v2/panels/{panel_id}", 200, "version"),
        ):
            api = StatefulPanelAPI()
            if operation == "update":
                api.seed("owned", remark=OfficialMenu._marker("group-a"), items=[])
            setattr(
                api,
                operation + "_response",
                QQResponse({}, status=status, trace_id="write-trace", method=method, route=route),
            )
            api.read_error_after_mutation = QQAPIError(
                503, "UPSTREAM", "read failed", trace_id="different-read-trace", method="GET"
            )
            with self.subTest(operation=operation), self.assertRaises(QQAPIError) as caught:
                await self.menu.install(self.ctx, api)
            error = caught.exception
            self.assertEqual(
                (error.status, error.trace_id, error.method, error.route),
                (status, "write-trace", method, route),
            )
            self.assertEqual(
                (error.reason, error.field, error.actual), ("field_type", field, "missing")
            )
            self.assertEqual([call[0] for call in api.mutations()], [operation])

    async def test_changed_ownership_or_content_after_write_never_confirms_success(self):
        changes = (
            ("panel_id", "another-panel"),
            ("scope", "c2c"),
            ("target_type", "all"),
            ("group_openids", ["group-a", "group-b"]),
            ("group_openids", ["group-b"]),
            ("panel", {"remark": "new-owner", "items": group_command_items()}),
            ("panel", {"remark": OfficialMenu._marker("group-a"), "items": []}),
        )
        for operation in ("create", "update"):
            for field, value in changes:
                api = StatefulPanelAPI()
                panel_id = "created-1" if operation == "create" else "owned"
                if operation == "update":
                    api.seed(panel_id, remark=OfficialMenu._marker("group-a"), items=[])
                setattr(api, operation + "_response", {})

                def change_detail(api=api, panel_id=panel_id, field=field, value=value):
                    # Keep list identity intact while detail reveals a changed
                    # association, ownership or content after the write.
                    api.detail_overrides[panel_id] = {
                        **deepcopy(api.panels[panel_id]),
                        field: deepcopy(value),
                    }

                api.after_mutation = change_detail
                with self.subTest(operation=operation, field=field, value=value):
                    with self.assertRaises(QQAPIError) as caught:
                        await self.menu.install(self.ctx, api)
                    self.assertEqual(
                        caught.exception.method, "POST" if operation == "create" else "PUT"
                    )
                    self.assertEqual(
                        caught.exception.field, "panel_id" if operation == "create" else "version"
                    )
                    self.assertEqual([call[0] for call in api.mutations()], [operation])

    async def test_duplicate_owned_panels_after_create_are_not_verified_as_success(self):
        self.api.create_response = {}
        self.api.after_mutation = lambda: self.seed_owned(panel_id="concurrent-panel")
        with self.assertRaises(QQAPIError) as caught:
            await self.menu.install(self.ctx, self.api)
        self.assertEqual(caught.exception.method, "POST")
        self.assertEqual([call[0] for call in self.api.mutations()], ["create"])
        self.assertFalse(any(call[0] == "get" for call in self.api.calls))

    async def test_timeout_or_server_error_does_not_trigger_readback_or_another_write(self):
        for operation in ("create", "update"):
            for status, code in ((0, "TIMEOUT"), (503, "UPSTREAM")):
                api = StatefulPanelAPI()
                if operation == "update":
                    api.seed("owned", remark=OfficialMenu._marker("group-a"), items=[])
                original = QQAPIError(status, code, "request failed")
                setattr(api, operation + "_response", original)
                with self.subTest(operation=operation, code=code):
                    with self.assertRaises(QQAPIError) as caught:
                        await self.menu.install(self.ctx, api)
                    self.assertIs(caught.exception, original)
                    self.assertEqual(api.calls[-1][0], operation)
                    self.assertEqual([call[0] for call in api.mutations()], [operation])

    async def test_readback_cancellation_propagates(self):
        for operation in ("create", "update"):
            api = StatefulPanelAPI()
            if operation == "update":
                api.seed("owned", remark=OfficialMenu._marker("group-a"), items=[])
            setattr(api, operation + "_response", {})
            api.read_error_after_mutation = asyncio.CancelledError()
            with self.subTest(operation=operation), self.assertRaises(asyncio.CancelledError):
                await self.menu.install(self.ctx, api)
            self.assertEqual([call[0] for call in api.mutations()], [operation])

    async def test_local_list_validation_uses_safe_field_types_and_real_response_metadata(self):
        self.api.pages = {
            "": QQResponse(
                {"records": "private-upstream-value", "is_end": True},
                status=200,
                trace_id="list-trace",
                method="GET",
                route="/v2/panels",
            )
        }
        with self.assertRaises(QQAPIError) as caught:
            await self.menu.install(self.ctx, self.api)
        error = caught.exception
        self.assertEqual(
            (error.status, error.trace_id, error.method, error.route),
            (200, "list-trace", "GET", "/v2/panels"),
        )
        self.assertEqual(
            (error.reason, error.field, error.expected, error.actual),
            ("field_type", "records", "list", "string"),
        )
        self.assertNotIn("private-upstream-value", str(error))
        self.assertEqual(self.api.mutations(), [])


if __name__ == "__main__":
    unittest.main()
