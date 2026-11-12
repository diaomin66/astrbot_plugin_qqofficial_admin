"""Authorization and persistent direct-execution replay regression tests."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qq_admin.api import QQAPIError
from qq_admin.security import AccessControl, PermissionDenied
from qq_admin.store import LocalStore


def context(**changes):
    values = {
        "platform_id": "bot-a",
        "group_id": "group-a",
        "user_id": "member-a",
        "is_superuser": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


class AccessControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_whitelist_error_11253_fails_closed(self):
        api = SimpleNamespace(
            get_member=AsyncMock(
                side_effect=QQAPIError(
                    403,
                    11253,
                    "secret upstream request content",
                )
            )
        )
        access = AccessControl({"allow_group_admins": True})
        with self.assertLogs("qq_admin.security", level="WARNING") as logs:
            with self.assertRaises(PermissionDenied) as caught:
                await access.require(context(), api)
        self.assertEqual(
            str(caught.exception), "无法验证管理员身份，请联系 AstrBot 管理员配置权限。"
        )
        self.assertNotIn("11253", str(caught.exception))
        self.assertNotIn("secret upstream", str(caught.exception))
        self.assertIn("11253", "\n".join(logs.output))
        self.assertNotIn("secret upstream", "\n".join(logs.output))
        api.get_member.assert_awaited_once_with("group-a", "member-a")

    async def test_trusted_admin_matches_all_three_context_identifiers(self):
        access = AccessControl(
            {
                "trusted_admins": ["bot-a|group-a|member-a"],
                "allow_group_admins": False,
            }
        )
        api = SimpleNamespace(get_member=AsyncMock(return_value={"member_role": "member"}))
        await access.require(context(), api)
        for changes in (
            {"platform_id": "bot-b"},
            {"group_id": "group-b"},
            {"user_id": "member-b"},
        ):
            with self.subTest(changes=changes), self.assertRaises(PermissionDenied):
                await access.require(context(**changes), api)
        api.get_member.assert_not_awaited()

    async def test_global_strategy_requires_superuser_even_for_trusted_owner(self):
        access = AccessControl({"trusted_admins": ["bot-a|group-a|member-a"]})
        api = SimpleNamespace(get_member=AsyncMock(return_value={"member_role": "owner"}))
        with self.assertRaises(PermissionDenied):
            await access.require(context(), api, global_operation=True)
        await access.require(context(is_superuser=True), api, global_operation=True)
        api.get_member.assert_not_awaited()

    async def test_allowlist_also_applies_to_superusers(self):
        access = AccessControl({"allowed_groups": ["other-group"]})
        with self.assertRaises(PermissionDenied):
            await access.require(context(is_superuser=True), object(), global_operation=True)

    async def test_group_roles_are_checked_and_bots_never_grant_actor_access(self):
        access = AccessControl({})
        for role in ("owner", "admin"):
            api = SimpleNamespace(
                get_member=AsyncMock(return_value={"member_role": role, "bot": False})
            )
            await access.require(context(), api)
        for member in (
            {"member_role": "member"},
            {"member_role": "unknown"},
            {},
            {"member_role": "owner", "bot": True},
            {"member_role": "admin", "bot": True},
        ):
            api = SimpleNamespace(get_member=AsyncMock(return_value=member))
            with self.subTest(member=member), self.assertRaises(PermissionDenied):
                await access.require(context(), api)

    async def test_role_is_rechecked_when_permission_is_revoked(self):
        api = SimpleNamespace(
            get_member=AsyncMock(
                side_effect=[
                    {"member_role": "admin"},
                    {"member_role": "member"},
                ]
            )
        )
        access = AccessControl({})
        await access.require(context(), api)
        with self.assertRaises(PermissionDenied):
            await access.require(context(), api)
        self.assertEqual(api.get_member.await_count, 2)

    async def test_button_data_cannot_move_between_users_groups_or_platforms(self):
        data = {
            "platform_id": "bot-a",
            "group_id": "group-a",
            "owner_id": "member-a",
            "command": "成员",
        }
        self.assertEqual(AccessControl.verify_button(context(), data), "成员")
        for changes in (
            {"platform_id": "bot-b"},
            {"group_id": "group-b"},
            {"user_id": "member-b"},
        ):
            with self.subTest(changes=changes), self.assertRaises(PermissionDenied):
                AccessControl.verify_button(context(**changes), data)
        for invalid in (
            None,
            [],
            "members",
            {},
            {**data, "command": ""},
            {**data, "command": "x" * 16001},
        ):
            with self.subTest(invalid_type=type(invalid)), self.assertRaises(PermissionDenied):
                AccessControl.verify_button(context(), invalid)

    async def test_button_request_key_is_stable_and_requires_a_valid_operation_identifier(self):
        operation_id = "panel-button_0123456789"
        data = {"operation_id": operation_id}
        self.assertEqual(AccessControl.button_request_key(data), "button:" + operation_id)
        self.assertEqual(AccessControl.button_request_key(dict(data)), "button:" + operation_id)
        for invalid in (
            None,
            [],
            {},
            {"operation_id": ""},
            {"operation_id": "short"},
            {"operation_id": 1234567890123456},
            {"operation_id": "x" * 129},
            {"operation_id": "x" * 16 + "\n"},
            {"operation_id": "with spaces " + "x" * 16},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(PermissionDenied):
                AccessControl.button_request_key(invalid)

    async def test_valid_operation_identifier_never_grants_administrator_access(self):
        data = {
            "platform_id": "bot-a",
            "group_id": "group-a",
            "owner_id": "member-a",
            "command": "禁言 member-b",
            "operation_id": "panel-button_0123456789",
        }
        self.assertTrue(AccessControl.button_request_key(data).startswith("button:"))
        self.assertEqual(AccessControl.verify_button(context(), data), "禁言 member-b")
        api = SimpleNamespace(get_member=AsyncMock(return_value={"member_role": "member"}))
        with self.assertRaises(PermissionDenied):
            await AccessControl({}).require(context(), api)
        api.get_member.assert_awaited_once_with("group-a", "member-a")


class LocalStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "moderation.sqlite3"
        self.stores = []
        self.store = self.open_store()
        self.ctx = context()

    def open_store(self):
        store = LocalStore(self.path)
        self.stores.append(store)
        return store

    async def close_store(self, store):
        await store.close()
        self.stores.remove(store)

    async def asyncTearDown(self):
        for store in self.stores:
            await store.close()
        self.directory.cleanup()

    def rows(self, query, params=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(query, params).fetchall()

    async def test_request_receipts_are_scoped_by_platform_group_and_operator(self):
        original = await self.store.claim_execution(self.ctx, "msg:one", "mute", {})
        claimed = [original]
        for changes in (
            {"platform_id": "bot-b"},
            {"group_id": "group-b"},
            {"user_id": "member-b"},
        ):
            claimed.append(
                await self.store.claim_execution(context(**changes), "msg:one", "mute", {})
            )
        self.assertEqual(len(set(claimed)), 4)
        with self.assertRaises(ValueError):
            await self.store.claim_execution(self.ctx, "msg:one", "mute", {})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 4)

    async def test_empty_or_invalid_request_keys_never_create_receipt_or_audit(self):
        for key in ("", "  ", None, 12, "x" * 2049, "msg:injected\nline"):
            with self.subTest(key_type=type(key)), self.assertRaises(ValueError):
                await self.store.claim_execution(self.ctx, key, "mute", {})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM execution_receipts")[0][0], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 0)

    async def test_many_simultaneous_claims_have_one_winner_and_one_audit(self):
        results = await asyncio.gather(
            *(
                self.store.claim_execution(
                    self.ctx, "button:action-one", "kick", {"members": ["target"]}
                )
                for _ in range(24)
            ),
            return_exceptions=True,
        )
        winners = [result for result in results if not isinstance(result, BaseException)]
        self.assertEqual(len(winners), 1)
        self.assertIsInstance(winners[0], int)
        self.assertTrue(
            all(
                isinstance(result, ValueError)
                for result in results
                if isinstance(result, BaseException)
            )
        )
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM execution_receipts")[0][0], 1)

    async def test_two_connections_still_cannot_claim_same_request_twice(self):
        second = self.open_store()
        results = await asyncio.gather(
            *(
                store.claim_execution(self.ctx, "msg:shared", "kick", {})
                for store in [self.store, second] * 8
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(result, BaseException) for result in results), 1)
        self.assertTrue(
            all(
                isinstance(result, ValueError)
                for result in results
                if isinstance(result, BaseException)
            )
        )
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 1)

    async def test_duplicate_key_cannot_change_action_or_parameters(self):
        audit_id = await self.store.claim_execution(
            self.ctx, "button:fixed", "mute", {"members": ["first"]}
        )
        for action, params in (
            ("mute", {"members": ["second"]}),
            ("kick", {"members": ["first"]}),
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                await self.store.claim_execution(self.ctx, "button:fixed", action, params)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 1)
        row = self.rows("SELECT action,params FROM audit WHERE id=?", (audit_id,))[0]
        self.assertEqual((row[0], json.loads(row[1])), ("mute", {"members": ["first"]}))

    async def test_completed_receipt_and_result_survive_restart(self):
        audit_id = await self.store.claim_execution(self.ctx, "msg:completed", "mute", {})
        await self.store.finish(audit_id, "success", "已执行")
        await self.close_store(self.store)
        reopened = self.open_store()
        with self.assertRaises(ValueError):
            await reopened.claim_execution(self.ctx, "msg:completed", "mute", {})
        self.assertEqual((await reopened.recent_audit(self.ctx))[0]["status"], "success")

    async def test_restart_marks_inflight_execution_unknown_without_releasing_receipt(self):
        audit_id = await self.store.claim_execution(self.ctx, "msg:inflight", "mute", {})
        self.assertEqual(
            self.rows("SELECT status FROM audit WHERE id=?", (audit_id,))[0][0], "executing"
        )
        await self.close_store(self.store)
        reopened = self.open_store()
        self.assertEqual((await reopened.recent_audit(self.ctx))[0]["status"], "unknown")
        with self.assertRaises(ValueError):
            await reopened.claim_execution(self.ctx, "msg:inflight", "mute", {})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 1)

    async def test_failure_and_unknown_results_do_not_allow_same_request_to_retry(self):
        for status in ("failed", "unknown", "partial", "success"):
            key = "msg:" + status
            audit_id = await self.store.claim_execution(self.ctx, key, "mute", {})
            await self.store.finish(audit_id, status, "结果已记录")
            with self.subTest(status=status), self.assertRaises(ValueError):
                await self.store.claim_execution(self.ctx, key, "mute", {})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 4)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM execution_receipts")[0][0], 4)

    async def test_notice_body_never_enters_receipts_or_audit_and_input_is_preserved(self):
        text = "CONFIDENTIAL_NOTICE_BODY_只在执行时需要"
        params = {"text": text, "everyone": True}
        audit_id = await self.store.claim_execution(self.ctx, "msg:notice", "notice", params)
        self.assertEqual(params["text"], text)
        await self.store.finish(audit_id, "success", "通知已发送")
        row = self.rows("SELECT params,detail FROM audit WHERE id=?", (audit_id,))[0]
        self.assertNotIn(text, " ".join(row))
        self.assertEqual(json.loads(row[0])["text"], f"[正文 {len(text)} 字符]")
        receipts = self.rows("SELECT * FROM execution_receipts")
        self.assertNotIn(text, repr(receipts))

    async def test_audit_failure_rolls_back_receipt_in_the_same_transaction(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TRIGGER fail_audit BEFORE INSERT ON audit "
                "BEGIN SELECT RAISE(ABORT, 'simulated audit storage failure'); END"
            )
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.claim_execution(self.ctx, "msg:atomic", "mute", {})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM execution_receipts")[0][0], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 0)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER fail_audit")
            db.commit()
        audit_id = await self.store.claim_execution(self.ctx, "msg:atomic", "mute", {})
        self.assertIsInstance(audit_id, int)

    async def test_receipts_expire_at_twenty_four_hours_without_duplicate_refresh(self):
        with patch("qq_admin.store.time.time", return_value=1000.0):
            await self.store.claim_execution(self.ctx, "msg:window", "mute", {})
        deadline = 1000.0 + 86400
        self.assertEqual(self.rows("SELECT expires FROM execution_receipts")[0][0], deadline)
        with patch("qq_admin.store.time.time", return_value=deadline - 1):
            with self.assertRaises(ValueError):
                await self.store.claim_execution(self.ctx, "msg:window", "mute", {})
            await self.store.cleanup()
        self.assertEqual(self.rows("SELECT expires FROM execution_receipts")[0][0], deadline)
        with patch("qq_admin.store.time.time", return_value=deadline):
            await self.store.cleanup()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM execution_receipts")[0][0], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 1)

    async def test_restart_cleans_expired_receipts_and_audits_at_independent_retention(self):
        with patch("qq_admin.store.time.time", return_value=1000.0):
            await self.store.claim_execution(self.ctx, "msg:old", "mute", {})
        await self.close_store(self.store)
        with patch("qq_admin.store.time.time", return_value=1000.0 + 86400):
            reopened = self.open_store()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM execution_receipts")[0][0], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 1)
        await self.close_store(reopened)
        with patch("qq_admin.store.time.time", return_value=1000.0 + 31 * 86400):
            self.open_store()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 0)

    async def test_periodic_cleanup_removes_old_audits(self):
        with patch("qq_admin.store.time.time", return_value=1000.0):
            await self.store.claim_execution(self.ctx, "msg:old-audit", "mute", {})
        with patch("qq_admin.store.time.time", return_value=1000.0 + 31 * 86400):
            await self.store.cleanup()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM execution_receipts")[0][0], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 0)

    async def test_upgrade_invalidates_legacy_confirmation_tokens_but_keeps_audits(self):
        audit_id = await self.store.claim_execution(self.ctx, "msg:before-upgrade", "mute", {})
        await self.store.finish(audit_id, "success")
        await self.close_store(self.store)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE pending(token TEXT PRIMARY KEY, params TEXT, summary TEXT)")
            db.execute(
                "INSERT INTO pending VALUES('legacy-token', 'private body', 'old confirmation')"
            )
            db.commit()
        reopened = self.open_store()
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='pending'"), [])
        self.assertEqual((await reopened.recent_audit(self.ctx))[0]["id"], audit_id)
        for removed_api in ("prepare", "peek", "cancel", "claim"):
            self.assertFalse(hasattr(reopened, removed_api))

    async def test_audit_is_isolated_by_bot_and_group(self):
        await self.store.claim_execution(self.ctx, "msg:scoped", "mute", {})
        self.assertEqual(len(await self.store.recent_audit(self.ctx)), 1)
        self.assertEqual(await self.store.recent_audit(context(platform_id="bot-b")), [])
        self.assertEqual(await self.store.recent_audit(context(group_id="group-b")), [])


if __name__ == "__main__":
    unittest.main()
