"""Verify transport boundaries and AstrBot routing without network credentials."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qq_admin.api import QQAPIError
from qq_admin.integration import (
    API_BASE,
    BotpyTransport,
    encode_callback,
    extract_event_context,
    extract_mention_ids,
    send_payload,
)


def make_event(*, interaction: bool = False, platform: str = "qq_official"):
    raw = SimpleNamespace(
        group_openid="group-1",
        id="interaction-1" if interaction else "message-1",
        event_id="outer-event-1",
        timestamp=time.time(),
        content="",
        author=SimpleNamespace(member_openid="member-1"),
    )
    if interaction:
        raw.scene = "group"
        raw.group_member_openid = "member-1"
    return SimpleNamespace(
        message_obj=SimpleNamespace(raw_message=raw, timestamp=time.time()),
        get_platform_name=lambda: platform,
        get_platform_id=lambda: "adapter-1",
        get_group_id=lambda: "group-1",
        get_sender_id=lambda: "member-1",
        get_self_id=lambda: "bot-1",
        get_messages=lambda: [],
        is_admin=lambda: False,
    )


def make_bot():
    token = SimpleNamespace(
        check_token=AsyncMock(),
        get_string=lambda: "QQBot private-test-token",
        app_id="app-1",
    )
    return SimpleNamespace(api=SimpleNamespace(_http=SimpleNamespace(_token=token)))


class FakeResponse:
    def __init__(self, status=200, payload=None, *, text=None):
        self.status = status
        self.headers = {"X-Tps-Trace-Id": "trace-1"}
        self._text = text if text is not None else json.dumps(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, response=None, failure=None):
        self.response = response or FakeResponse(payload={"ok": True})
        self.failure = failure
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        return self.response

    async def close(self):
        self.closed = True


class EventContextTests(unittest.TestCase):
    def test_official_group_routes_both_supported_adapters(self):
        for platform in ("qq_official", "qq_official_webhook"):
            context = extract_event_context(make_event(platform=platform))
            self.assertEqual(context.group_id, "group-1")
            self.assertEqual(context.user_id, "member-1")
            self.assertEqual(context.message_id, "message-1")
            self.assertEqual(context.event_id, "")
            self.assertFalse(context.is_superuser)

    def test_guild_private_and_non_official_events_are_excluded(self):
        event = make_event()
        event.message_obj.raw_message = SimpleNamespace(channel_id="channel-1")
        self.assertIsNone(extract_event_context(event))
        event = make_event(interaction=True)
        event.message_obj.raw_message.scene = "guild"
        self.assertIsNone(extract_event_context(event))
        self.assertIsNone(extract_event_context(make_event(platform="aiocqhttp")))

    def test_interaction_uses_envelope_id_not_interaction_id(self):
        context = extract_event_context(make_event(interaction=True))
        self.assertEqual(context.message_id, "")
        self.assertEqual(context.event_id, "outer-event-1")

    def test_normalized_identity_must_match_raw_group_and_operator(self):
        for interaction in (False, True):
            event = make_event(interaction=interaction)
            event.get_sender_id = lambda: "different-member"
            self.assertIsNone(extract_event_context(event))
            event = make_event(interaction=interaction)
            event.get_group_id = lambda: "different-group"
            self.assertIsNone(extract_event_context(event))

    def test_user_mentions_exclude_bot_and_deduplicate(self):
        event = make_event()
        event.message_obj.raw_message.content = (
            '<qqbot-at-user id="member-2" /> <@!member-3> <@bot-1>'
        )
        event.message_obj.raw_message.mentions = [
            SimpleNamespace(id="bot-1", is_you=True),
            SimpleNamespace(id="member-2", is_you=False),
        ]
        self.assertEqual(extract_mention_ids(event), ["member-2", "member-3"])

    def test_callback_passes_json_and_expiry_to_native_encoder(self):
        module = types.ModuleType("astrbot.core.platform.button_interaction")
        observed = []

        def native_encoder(action_id, data, *, expires_at):
            observed.append((action_id, data, expires_at))
            return "astrbot:opaque"

        module.encode_button_callback = native_encoder
        with patch.dict(sys.modules, {module.__name__: module}):
            result = encode_callback("qqadmin", {"command": "info"}, expires_at=123.0)
        self.assertEqual(result, "astrbot:opaque")
        self.assertEqual(observed, [("qqadmin", {"command": "info"}, 123.0)])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_new_fixed_host_refreshes_token_and_never_follows_redirects(self):
        bot = make_bot()
        session = FakeSession()
        transport = BotpyTransport(bot)
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            response = await transport.request("GET", "/v2/groups/group-1/info")
        self.assertEqual(response, {"ok": True})
        bot.api._http._token.check_token.assert_awaited_once()
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, API_BASE + "/v2/groups/group-1/info")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["X-Union-Appid"], "app-1")
        await transport.close()
        self.assertTrue(session.closed)

    async def test_rejects_foreign_hosts_and_fragment_paths_before_authentication(self):
        bot = make_bot()
        transport = BotpyTransport(bot)
        for path in ("https://example.com", "//example.com", "/v2/test?x=1", "/v2/test#x"):
            with self.assertRaises(ValueError):
                await transport.request("GET", path)
        bot.api._http._token.check_token.assert_not_awaited()

    async def test_http_redirect_is_error_and_never_exposes_authorization(self):
        session = FakeSession(FakeResponse(302, {"message": "QQBot private-test-token"}))
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError) as raised:
                await transport.request("POST", "/v2/groups/group-1/messages", json={})
        self.assertNotIn("private-test-token", str(raised.exception))
        self.assertEqual(len(session.calls), 1)
        await transport.close()

    async def test_modern_error_envelope_is_not_treated_as_success(self):
        session = FakeSession(
            FakeResponse(200, {"err_code": 304023, "err_msg": "permission denied"})
        )
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError) as raised:
                await transport.request("GET", "/v2/groups/group-1/info")
        self.assertEqual(raised.exception.code, 304023)
        self.assertEqual(raised.exception.message, "permission denied")
        await transport.close()

    async def test_empty_delete_response_is_success(self):
        session = FakeSession(FakeResponse(204, text=""))
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            result = await transport.request("DELETE", "/v2/groups/group-1/messages/msg-1")
        self.assertIsNone(result)
        await transport.close()

    async def test_timeout_does_not_retry_mutation(self):
        session = FakeSession(failure=TimeoutError())
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError):
                await transport.request("POST", "/v2/groups/group-1/messages", json={})
        self.assertEqual(len(session.calls), 1)
        await transport.close()

    async def test_invalid_success_body_is_reported(self):
        session = FakeSession(FakeResponse(200, text="<html>upstream error</html>"))
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError):
                await transport.request("GET", "/v2/groups/group-1/info")
        await transport.close()

    async def test_auth_error_is_redacted_and_close_does_not_close_adapter(self):
        bot = make_bot()
        bot.api._http._token.check_token.side_effect = RuntimeError("secret credential")
        transport = BotpyTransport(bot)
        with self.assertRaises(QQAPIError) as raised:
            await transport.request("GET", "/v2/groups/group-1/info")
        self.assertNotIn("secret credential", str(raised.exception))
        await transport.close()
        self.assertIsNotNone(bot.api._http._token)
        with self.assertRaises(QQAPIError):
            await transport.request("GET", "/v2/groups/group-1/info")


class MessageRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_replies_allocate_distinct_sequences(self):
        transport = BotpyTransport(make_bot())
        transport.request = AsyncMock(return_value={"id": "sent-1"})
        event = make_event()
        await asyncio.gather(
            *(
                send_payload(event, {"msg_type": 0, "content": "hello"}, transport)
                for _ in range(12)
            )
        )
        payloads = [call.kwargs["json"] for call in transport.request.await_args_list]
        sequences = sorted(payload["msg_seq"] for payload in payloads)
        self.assertGreater(sequences[0], 10000)
        self.assertLess(sequences[-1], 1 << 31)
        self.assertEqual(sequences, list(range(sequences[0], sequences[0] + 12)))
        self.assertTrue(all(payload["msg_id"] == "message-1" for payload in payloads))
        self.assertTrue(all("event_id" not in payload for payload in payloads))

    async def test_callback_payload_cannot_override_event_routing_or_reply_id(self):
        transport = BotpyTransport(make_bot())
        transport.request = AsyncMock(return_value={"id": "sent-1"})
        result = await send_payload(
            make_event(interaction=True),
            {"msg_type": 2, "msg_id": "forged-message", "event_id": "forged-event", "msg_seq": 99},
            transport,
        )
        self.assertEqual(result, {"id": "sent-1"})
        call = transport.request.await_args
        self.assertEqual(call.args, ("POST", "/v2/groups/group-1/messages"))
        self.assertEqual(call.kwargs["json"]["event_id"], "outer-event-1")
        self.assertGreater(call.kwargs["json"]["msg_seq"], 10000)
        self.assertNotIn("msg_id", call.kwargs["json"])

    async def test_expired_passive_reply_is_not_silently_sent_proactively(self):
        event = make_event()
        event.message_obj.raw_message.timestamp = time.time() - 301
        transport = BotpyTransport(make_bot())
        transport.request = AsyncMock()
        with self.assertRaises(QQAPIError):
            await send_payload(event, {"content": "expired"}, transport)
        transport.request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
