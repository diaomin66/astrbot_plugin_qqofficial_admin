"""Authorization and persistent confirmation replay regression tests."""

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
        with self.assertRaises(PermissionDenied) as caught:
            await access.require(context(), api)
        self.assertIn("11253", str(caught.exception))
        self.assertNotIn("secret upstream", str(caught.exception))
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

    async def test_confirmation_token_is_bound_to_user_group_and_platform(self):
        pending = await self.store.prepare(self.ctx, "mute", {"members": ["target"]}, "禁言 target")
        for changes in (
            {"platform_id": "bot-b"},
            {"group_id": "group-b"},
            {"user_id": "member-b"},
        ):
            other = context(**changes)
            for operation in (self.store.peek, self.store.claim, self.store.cancel):
                with (
                    self.subTest(changes=changes, operation=operation.__name__),
                    self.assertRaises(ValueError),
                ):
                    await operation(other, pending.token)
        claimed, _ = await self.store.claim(self.ctx, pending.token)
        self.assertEqual(claimed.params, {"members": ["target"]})

    async def test_expiry_boundary_denies_peek_claim_and_cancel(self):
        with patch("qq_admin.store.time.time", return_value=1000.0):
            pending = await self.store.prepare(self.ctx, "mute", {}, "禁言", ttl=30)
        self.assertEqual(pending.expires_at, 1030.0)
        with patch("qq_admin.store.time.time", return_value=1030.0):
            for operation in (self.store.peek, self.store.claim, self.store.cancel):
                with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                    await operation(self.ctx, pending.token)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 0)

    async def test_many_simultaneous_claims_have_one_winner_and_one_audit(self):
        pending = await self.store.prepare(self.ctx, "kick", {"members": ["target"]}, "移出 target")
        results = await asyncio.gather(
            *(self.store.claim(self.ctx, pending.token) for _ in range(24)),
            return_exceptions=True,
        )
        winners = [result for result in results if not isinstance(result, BaseException)]
        self.assertEqual(len(winners), 1)
        self.assertTrue(
            all(
                isinstance(result, ValueError)
                for result in results
                if isinstance(result, BaseException)
            )
        )
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 1)
        with self.assertRaises(ValueError):
            await self.store.peek(self.ctx, pending.token)

    async def test_two_connections_still_cannot_claim_same_token_twice(self):
        second = self.open_store()
        pending = await self.store.prepare(self.ctx, "kick", {}, "移出成员")
        results = await asyncio.gather(
            *(store.claim(self.ctx, pending.token) for store in [self.store, second] * 8),
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

    async def test_pending_and_consumption_survive_store_restart(self):
        pending = await self.store.prepare(self.ctx, "mute", {"members": ["target"]}, "禁言 target")
        await self.close_store(self.store)
        reopened = self.open_store()
        self.assertEqual(
            (await reopened.peek(self.ctx, pending.token)).params, {"members": ["target"]}
        )
        _, audit_id = await reopened.claim(self.ctx, pending.token)
        await reopened.finish(audit_id, "success", "已执行")
        await self.close_store(reopened)
        reopened = self.open_store()
        with self.assertRaises(ValueError):
            await reopened.claim(self.ctx, pending.token)
        self.assertEqual((await reopened.recent_audit(self.ctx))[0]["status"], "success")

    async def test_cancelled_confirmation_cannot_later_be_claimed(self):
        pending = await self.store.prepare(self.ctx, "kick", {}, "移出成员")
        await self.store.cancel(self.ctx, pending.token)
        for operation in (self.store.peek, self.store.claim, self.store.cancel):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                await operation(self.ctx, pending.token)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 0)

    async def test_notice_body_is_returned_for_execution_but_not_retained(self):
        text = "CONFIDENTIAL_NOTICE_BODY_只在执行时需要"
        pending = await self.store.prepare(self.ctx, "notice", {"text": text}, "通知：" + text)
        claimed, audit_id = await self.store.claim(self.ctx, pending.token)
        self.assertEqual(claimed.params["text"], text)
        await self.store.finish(audit_id, "success", "通知已发送")
        audit = self.rows("SELECT params,detail FROM audit WHERE id=?", (audit_id,))[0]
        self.assertNotIn(text, " ".join(audit))
        self.assertNotEqual(json.loads(audit[0])["text"], text)
        self.assertEqual(
            self.rows("SELECT params,summary FROM pending WHERE token=?", (pending.token,)), []
        )

    async def test_cancel_deletes_pending_notice_body_immediately(self):
        text = "CANCELLED_PRIVATE_NOTICE"
        pending = await self.store.prepare(self.ctx, "notice", {"text": text}, "通知：" + text)
        await self.store.cancel(self.ctx, pending.token)
        self.assertEqual(
            self.rows("SELECT params,summary FROM pending WHERE token=?", (pending.token,)), []
        )

    async def test_restart_removes_expired_pending_without_another_command(self):
        with patch("qq_admin.store.time.time", return_value=1000.0):
            pending = await self.store.prepare(
                self.ctx, "notice", {"text": "expired notice"}, "过期通知", ttl=30
            )
        await self.close_store(self.store)
        with patch("qq_admin.store.time.time", return_value=1031.0):
            self.open_store()
        self.assertEqual(self.rows("SELECT token FROM pending WHERE token=?", (pending.token,)), [])

    async def test_restart_removes_audit_past_retention(self):
        with patch("qq_admin.store.time.time", return_value=1000.0):
            pending = await self.store.prepare(self.ctx, "mute", {}, "禁言")
            await self.store.claim(self.ctx, pending.token)
        await self.close_store(self.store)
        with patch("qq_admin.store.time.time", return_value=1000.0 + 31 * 86400):
            self.open_store()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit")[0][0], 0)

    async def test_audit_is_isolated_by_bot_and_group(self):
        pending = await self.store.prepare(self.ctx, "mute", {}, "禁言")
        await self.store.claim(self.ctx, pending.token)
        self.assertEqual(len(await self.store.recent_audit(self.ctx)), 1)
        self.assertEqual(await self.store.recent_audit(context(platform_id="bot-b")), [])
        self.assertEqual(await self.store.recent_audit(context(group_id="group-b")), [])


if __name__ == "__main__":
    unittest.main()
