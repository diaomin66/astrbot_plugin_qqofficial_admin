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

import aiohttp

from qq_admin.api import QQAPIError, QQGroupAPI, QQResponse
from qq_admin.diagnostics import RequestState, current_request_state
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
    def __init__(self, status=200, payload=None, *, text=None, headers=None):
        self.status = status
        self.headers = {"X-Tps-Trace-Id": "trace-1"} if headers is None else headers
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

    def test_mentions_prefer_group_openid_and_exclude_all_bot_aliases(self):
        event = make_event()
        raw = event.message_obj.raw_message
        raw.content = "<@user-2> <@member-2> <@bot-user> <@bot-member> <@other-bot>"
        raw.mentions = [
            {"id": "user-2", "member_openid": "member-2", "user_openid": "private-2"},
            {"id": "bot-user", "member_openid": "bot-member", "is_you": True},
            {"id": "other-bot", "bot": True},
        ]
        self.assertEqual(extract_mention_ids(event), ["member-2"])

    def test_richer_raw_data_resolves_incomplete_sdk_mentions_and_components(self):
        event = make_event()
        raw = event.message_obj.raw_message
        raw.mentions = [SimpleNamespace(id="user-2")]
        raw.raw_data = {
            "mentions": [
                {"id": "user-2", "member_openid": "member-2"},
                {"id": "bot-user", "member_openid": "bot-member", "is_you": True},
            ],
            "content": "<@!user-2> <@!bot-user>",
        }
        At = type("At", (), {})
        target, bot = At(), At()
        target.qq, bot.qq = "user-2", "bot-user"
        event.get_messages = lambda: [target, bot]
        self.assertEqual(extract_mention_ids(event), ["member-2"])

    def test_private_chat_and_union_identifiers_are_never_member_fallbacks(self):
        event = make_event()
        event.message_obj.raw_message.mentions = [
            {"user_openid": "private-2", "union_user_account": "account-2"},
            {"id": "documented-user-openid", "username": "nickname"},
        ]
        self.assertEqual(extract_mention_ids(event), ["documented-user-openid"])

    def test_conflicting_mention_identities_cannot_choose_a_target(self):
        event = make_event()
        event.message_obj.raw_message.mentions = [
            {"id": "user-2", "member_openid": "member-2"},
            {"id": "user-2", "member_openid": "member-3"},
        ]
        with self.assertRaisesRegex(ValueError, "身份不一致"):
            extract_mention_ids(event)

    def test_mentions_from_quoted_message_are_not_targets(self):
        event = make_event()
        event.message_obj.raw_message.raw_data = {
            "msg_elements": [{"ref": {"content": "<@quoted-user>"}}],
            "message_reference": {"mentions": [{"id": "quoted-user"}]},
        }
        self.assertEqual(extract_mention_ids(event), [])

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
        self.assertEqual(transport.application_id, "app-1")
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

    async def test_invalid_error_code_types_keep_http_status_and_safe_field_diagnostics(self):
        for field in ("err_code", "code"):
            for value, kind in (
                ({"private-key": "private-value"}, "object"),
                (["private-value"], "array"),
                (True, "boolean"),
                (False, "boolean"),
                (0.0, "number"),
            ):
                for status in (200, 403, 503):
                    with self.subTest(field=field, kind=kind, status=status, value=value):
                        # The other valid field must not skip type validation.
                        payload = {"err_code": 0, "code": 0, field: value}
                        session = FakeSession(FakeResponse(status, payload))
                        transport = BotpyTransport(make_bot())
                        with patch(
                            "qq_admin.integration.aiohttp.ClientSession", return_value=session
                        ):
                            with self.assertRaises(QQAPIError) as caught:
                                await QQGroupAPI(transport).remove_members("private-group", ["m"])
                        error = caught.exception
                        self.assertEqual(
                            error.code, "INVALID_RESPONSE" if status == 200 else status
                        )
                        self.assertEqual(error.status, status)
                        self.assertEqual(error.trace_id, "trace-1")
                        self.assertEqual(error.reason, "field_type")
                        self.assertEqual(error.field, field)
                        self.assertEqual(error.expected, "integer_or_string_or_null")
                        self.assertEqual(error.actual, kind)
                        self.assertTrue(error.request_sent)
                        self.assertEqual(error.method, "POST")
                        self.assertNotIn("private", error.diagnostic_text())
                        self.assertNotIn("private", repr(error))
                        self.assertIsInstance(hash(error.code), int)
                        await transport.close()

    async def test_nonzero_error_in_either_field_cannot_be_masked_by_success(self):
        cases = (
            ({"err_code": 0, "code": 11253}, 11253),
            ({"err_code": None, "code": "11253"}, "11253"),
            ({"err_code": "0", "code": 11253}, 11253),
            ({"err_code": 11253, "code": 0}, 11253),
            ({"err_code": "11253", "code": None}, "11253"),
            ({"err_code": 11253, "code": 11254}, 11253),
        )
        for payload, expected_code in cases:
            with self.subTest(payload=payload):
                session = FakeSession(FakeResponse(200, payload))
                transport = BotpyTransport(make_bot())
                with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
                    with self.assertRaises(QQAPIError) as caught:
                        await transport.request("GET", "/v2/groups/group-1/members")
                self.assertEqual(caught.exception.code, expected_code)
                self.assertTrue(caught.exception.request_sent)
                await transport.close()
        for payload in (
            {"err_code": 0, "code": "0"},
            {"err_code": None, "code": 0},
            {"err_code": "0", "code": None},
        ):
            with self.subTest(success=payload):
                session = FakeSession(FakeResponse(200, payload))
                transport = BotpyTransport(make_bot())
                with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
                    result = await transport.request("GET", "/v2/groups/group-1/members")
                self.assertEqual(result, payload)
                await transport.close()

    async def test_empty_delete_response_is_success(self):
        session = FakeSession(FakeResponse(204, text=""))
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            result = await transport.request("DELETE", "/v2/groups/group-1/messages/msg-1")
        self.assertIsInstance(result, QQResponse)
        self.assertEqual(result, {})
        self.assertEqual(result.status, 204)
        self.assertEqual(result.trace_id, "trace-1")
        self.assertEqual(result.method, "DELETE")
        self.assertEqual(result.route, "/v2/groups/{group_openid}/messages/{message_id}")
        await transport.close()

    async def test_timeout_does_not_retry_mutation(self):
        session = FakeSession(failure=TimeoutError())
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError) as caught:
                await transport.request("POST", "/v2/groups/group-1/messages", json={})
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(caught.exception.request_sent)
        self.assertEqual(caught.exception.status, 0)
        self.assertEqual(caught.exception.method, "POST")
        await transport.close()

    async def test_invalid_success_body_is_reported(self):
        session = FakeSession(FakeResponse(200, text="<html>upstream error</html>"))
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError) as caught:
                await transport.request("GET", "/v2/groups/group-1/info")
        self.assertEqual(caught.exception.reason, "json_decode")
        self.assertEqual(caught.exception.status, 200)
        self.assertEqual(caught.exception.trace_id, "trace-1")
        self.assertEqual(caught.exception.method, "GET")
        self.assertEqual(caught.exception.route, "/v2/groups/{group_openid}/info")
        self.assertTrue(caught.exception.request_sent)
        self.assertNotIn("upstream error", caught.exception.user_message())
        await transport.close()

    async def test_json_root_type_failures_report_only_types(self):
        for payload, kind in (
            (["private-body"], "array"),
            ("private-body", "string"),
            (True, "boolean"),
            (7, "number"),
            (3.5, "number"),
            (None, "null"),
        ):
            with self.subTest(kind=kind, payload=payload):
                session = FakeSession(FakeResponse(payload=payload))
                transport = BotpyTransport(make_bot())
                with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
                    with self.assertRaises(QQAPIError) as caught:
                        await transport.request("GET", "/v2/groups/group-1/members")
                error = caught.exception
                self.assertEqual(error.reason, "root_type")
                self.assertEqual(error.expected, "object")
                self.assertEqual(error.actual, kind)
                self.assertTrue(error.request_sent)
                self.assertNotIn("private", error.user_message())
                self.assertEqual(str(error), error.user_message())
                await transport.close()

    async def test_unreadable_text_has_bounded_diagnostics(self):
        response = FakeResponse(payload={})
        response.text = AsyncMock(
            side_effect=UnicodeDecodeError("utf-8", b"private-data", 0, 1, "private reason")
        )
        session = FakeSession(response)
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError) as caught:
                await transport.request("GET", "/v2/groups/group-1/members")
        self.assertEqual(caught.exception.reason, "text_decode")
        self.assertTrue(caught.exception.request_sent)
        self.assertNotIn("private", caught.exception.diagnostic_text())
        await transport.close()

    async def test_safe_trace_from_body_is_fallback_for_missing_or_invalid_header(self):
        for header, expected in (
            (None, "body-trace"),
            ("invalid\nheader", "body-trace"),
            ("header-trace", "header-trace"),
        ):
            for error_response in (False, True):
                with self.subTest(header=header, error=error_response):
                    payload = {"trace_id": "body-trace"}
                    if error_response:
                        payload.update(err_code=11253, err_msg="private text")
                    headers = {} if header is None else {"X-Tps-Trace-Id": header}
                    session = FakeSession(FakeResponse(payload=payload, headers=headers))
                    transport = BotpyTransport(make_bot())
                    with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
                        if error_response:
                            with self.assertRaises(QQAPIError) as caught:
                                await transport.request("GET", "/v2/groups/group-1/members")
                            result = caught.exception
                        else:
                            result = await transport.request("GET", "/v2/groups/group-1/members")
                    self.assertEqual(result.trace_id, expected)
                    await transport.close()

    async def test_diagnostics_omit_credentials_payload_and_resource_identifiers(self):
        payload = {
            "err_code": "private-test-token",
            "err_msg": "QQBot private-test-token private-notice-body",
            "trace_id": "private-test-token",
            "private-response-key": "private-response-value",
        }
        session = FakeSession(
            FakeResponse(400, payload, headers={"X-Tps-Trace-Id": "private-test-token"})
        )
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError) as caught:
                await transport.request(
                    "POST",
                    "/v2/groups/private-group/messages",
                    json={"content": "private-notice-body"},
                    params={"cursor": "private-cursor"},
                )
        error = caught.exception
        self.assertEqual(error.route, "/v2/groups/{group_openid}/messages")
        self.assertEqual(error.trace_id, "")
        self.assertTrue(error.request_sent)
        self.assertNotIn("private", error.diagnostic_text())
        self.assertNotIn("private", error.user_message())
        self.assertNotIn("private", repr(error))
        await transport.close()

    async def test_unknown_route_does_not_leak_its_path(self):
        session = FakeSession(failure=TimeoutError())
        transport = BotpyTransport(make_bot())
        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(QQAPIError) as caught:
                await transport.request("GET", "/v2/private-unknown-route/private-id")
        self.assertEqual(caught.exception.route, "unknown")
        self.assertNotIn("private", caught.exception.diagnostic_text())
        await transport.close()

    async def test_failures_after_http_start_preserve_stage_and_response_metadata(self):
        for failure in (TimeoutError(), aiohttp.ClientPayloadError("private network details")):
            with self.subTest(failure=type(failure).__name__):
                response = FakeResponse(201, payload={})
                response.text = AsyncMock(side_effect=failure)
                session = FakeSession(response)
                transport = BotpyTransport(make_bot())
                state = RequestState()
                token = current_request_state.set(state)
                try:
                    with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
                        with self.assertRaises(QQAPIError) as caught:
                            await QQGroupAPI(transport).create_strategy(group_openids=["g"])
                    error = caught.exception
                    self.assertTrue(state.mutation_started)
                    self.assertTrue(error.request_sent)
                    self.assertEqual(error.status, 201)
                    self.assertEqual(error.trace_id, "trace-1")
                    self.assertEqual(error.method, "POST")
                    self.assertEqual(error.route, "/v2/groups/join_approval_strategy")
                    self.assertEqual(len(session.calls), 1)
                    self.assertEqual(
                        session.calls[0][2]["json"],
                        {"group_openids": ["g"], "is_enable": "on"},
                    )
                    self.assertNotIn("private", repr(error))
                finally:
                    current_request_state.reset(token)
                    await transport.close()

    async def test_auth_error_is_redacted_and_close_does_not_close_adapter(self):
        bot = make_bot()
        bot.api._http._token.check_token.side_effect = RuntimeError("secret credential")
        transport = BotpyTransport(bot)
        with self.assertRaises(QQAPIError) as raised:
            await transport.request("GET", "/v2/groups/group-1/info")
        self.assertNotIn("secret credential", str(raised.exception))
        self.assertFalse(raised.exception.request_sent)
        self.assertEqual(raised.exception.method, "GET")
        await transport.close()
        self.assertIsNotNone(bot.api._http._token)
        with self.assertRaises(QQAPIError) as raised:
            await transport.request("GET", "/v2/groups/group-1/info")
        self.assertFalse(raised.exception.request_sent)

    async def test_auth_failure_or_shutdown_never_marks_a_mutation(self):
        for phase in ("auth_failure", "closed", "closed_after_auth"):
            with self.subTest(phase=phase):
                bot = make_bot()
                transport = BotpyTransport(bot)
                if phase == "auth_failure":
                    bot.api._http._token.check_token.side_effect = RuntimeError("private token")
                elif phase == "closed":
                    await transport.close()
                else:
                    bot.api._http._token.check_token.side_effect = transport.close
                state = RequestState()
                token = current_request_state.set(state)
                try:
                    with patch("qq_admin.integration.aiohttp.ClientSession") as session:
                        with self.assertRaises(QQAPIError) as caught:
                            await QQGroupAPI(transport).remove_members("g", ["m"])
                    session.assert_not_called()
                    self.assertFalse(state.mutation_started)
                    self.assertFalse(caught.exception.request_sent)
                    self.assertEqual(caught.exception.method, "POST")
                finally:
                    current_request_state.reset(token)
                    await transport.close()

    async def test_cancellation_during_auth_is_distinct_from_cancellation_during_http(self):
        for phase in ("auth", "http"):
            with self.subTest(phase=phase):
                entered = asyncio.Event()
                state = RequestState()

                async def wait_for_cancellation(entered=entered):
                    entered.set()
                    await asyncio.Future()

                bot = make_bot()
                response = FakeResponse(payload={})
                session = FakeSession(response)
                transport = BotpyTransport(bot)
                if phase == "auth":
                    bot.api._http._token.check_token.side_effect = wait_for_cancellation
                else:
                    response.text = wait_for_cancellation

                async def execute(state=state, transport=transport):
                    token = current_request_state.set(state)
                    try:
                        await QQGroupAPI(transport).remove_members("g", ["m"])
                    finally:
                        current_request_state.reset(token)

                with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
                    task = asyncio.create_task(execute())
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                self.assertEqual(state.mutation_started, phase == "http")
                self.assertEqual(len(session.calls), int(phase == "http"))
                await transport.close()

    async def test_shared_transport_keeps_concurrent_response_metadata_and_stages_separate(self):
        write_started, read_started = asyncio.Event(), asyncio.Event()

        class InterleavedResponse(FakeResponse):
            async def text(self):
                if self.status == 201:
                    write_started.set()
                    await read_started.wait()
                else:
                    await write_started.wait()
                    read_started.set()
                await asyncio.sleep(0)
                return self._text

        class InterleavedSession(FakeSession):
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                write = method == "POST"
                return InterleavedResponse(
                    201 if write else 200,
                    {"id": "write" if write else "read"},
                    headers={"X-Tps-Trace-Id": "trace-write" if write else "trace-read"},
                )

        session = InterleavedSession()
        transport = BotpyTransport(make_bot())
        api = QQGroupAPI(transport)

        async def execute(write):
            state = RequestState()
            token = current_request_state.set(state)
            try:
                response = (
                    await api.remove_members("g", ["m"]) if write else await api.get_group_info("g")
                )
                return response, state.mutation_started
            finally:
                current_request_state.reset(token)

        with patch("qq_admin.integration.aiohttp.ClientSession", return_value=session):
            (write, write_started), (read, read_started) = await asyncio.gather(
                execute(True), execute(False)
            )
        self.assertTrue(write_started)
        self.assertFalse(read_started)
        self.assertEqual((write.status, write.trace_id, write.method), (201, "trace-write", "POST"))
        self.assertEqual((read.status, read.trace_id, read.method), (200, "trace-read", "GET"))
        self.assertEqual(write.route, "/v2/groups/{group_openid}/batch_remove_members")
        self.assertEqual(read.route, "/v2/groups/{group_openid}/info")
        await transport.close()


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
