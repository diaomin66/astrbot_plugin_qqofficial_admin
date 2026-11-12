"""中文命令与按钮共用鉴权、直接执行、防重复和审计流程。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from .api import QQAPIError, QQGroupAPI
from .diagnostics import RequestState, current_request_state
from .official_menu import OfficialMenu
from .pagination import ResultPager
from .panels import ButtonSpec, Panel, help_panel, home_panel
from .security import AccessControl
from .store import LocalStore

logger = logging.getLogger(__name__)
GLOBAL_COMMANDS = {
    "策略列表",
    "策略创建",
    "策略修改",
    "策略删除",
    "策略执行",
    "策略白名单",
    "设置菜单",
    "移除菜单",
}
WRITE_COMMANDS = {
    "移出",
    "拉黑",
    "取消拉黑",
    "禁言",
    "更新禁言",
    "解禁",
    "同意",
    "拒绝",
    "策略创建",
    "策略修改",
    "策略删除",
    "策略执行",
    "策略白名单",
    "撤回",
    "通知",
    "全体通知",
    "设置菜单",
    "移除菜单",
}
HOME = ButtonSpec("返回菜单", "菜单")
DEFAULT_MUTE_SECONDS = 600


def split_head(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    return (parts[0], parts[1] if len(parts) == 2 else "") if parts else ("", "")


def parse_ids(text: str, maximum: int = 20) -> list[str]:
    text = re.sub(r'<qqbot-at-user\s+id=["\']([^"\']+)["\']\s*/?>', r"\1", text)
    text = re.sub(r"<@!?([^>\s]+)>", r"\1", text)
    ids = [value for value in re.split(r"[,，\s]+", text.strip()) if value]
    if not ids or len(ids) > maximum:
        raise ValueError(f"请提供 1–{maximum} 个成员 OpenID，用英文逗号分隔。")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in ids):
        raise ValueError("请直接 @成员，或使用 /成员 查询后填写成员 OpenID，不能填写 QQ 号或昵称。")
    if len(set(ids)) != len(ids):
        raise ValueError("同一次操作不能包含重复的成员 OpenID。")
    return ids


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(s|m|h|d|秒|分钟|小时|天)?", value.lower())
    if not match:
        raise ValueError("禁言时长示例：600、10m、1h、7d；纯数字单位为秒。")
    seconds = (
        int(match[1])
        * {
            "s": 1,
            "秒": 1,
            "m": 60,
            "分钟": 60,
            "h": 3600,
            "小时": 3600,
            "d": 86400,
            "天": 86400,
        }[match[2] or "s"]
    )
    if seconds > 30 * 86400:
        raise ValueError("官方接口最长允许禁言 30 天。")
    return seconds


def json_object(text: str, allowed: set[str]) -> dict:
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("请提供有效的 JSON 对象，字段名和字符串使用双引号。") from exc
    if not isinstance(value, dict) or not value:
        raise ValueError("参数必须是非空 JSON 对象。")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError("不支持的参数：" + ", ".join(sorted(unknown)))
    return value


def _rows(data: dict, *keys: str) -> list[dict]:
    for key in keys:
        if isinstance(data.get(key), list):
            return [row for row in data[key] if isinstance(row, dict)]
    return []


def _page_button(data: dict, command: str) -> list[ButtonSpec]:
    cursor = data.get("next_cursor")
    return [ButtonSpec("下一页", f"{command} {cursor}")] if cursor else []


class RecentMessages:
    """只在内存保存官方消息 ID、作者和时间。"""

    def __init__(self):
        self._groups: OrderedDict[tuple[str, str], deque[dict]] = OrderedDict()

    def remember(
        self, ctx: Any, *, message_id: str = "", author: str = "", timestamp: float | None = None
    ):
        message_id = message_id or ctx.message_id
        if not message_id:
            return
        key = (ctx.platform_id, ctx.group_id)
        messages = self._groups.pop(key, deque(maxlen=30))
        now = time.time()
        while messages and now - messages[0]["received_at"] >= 120:
            messages.popleft()
        if not any(item["message_id"] == message_id for item in messages):
            messages.append(
                {
                    "message_id": message_id,
                    "member_openid": author or ctx.user_id,
                    "received_at": now if timestamp is None else timestamp,
                }
            )
        self._groups[key] = messages
        while len(self._groups) > 256:
            self._groups.popitem(last=False)

    def list(self, ctx: Any) -> list[dict]:
        rows = self._groups.get((ctx.platform_id, ctx.group_id), [])
        return [dict(item) for item in reversed(rows) if time.time() - item["received_at"] < 120]

    def prune(self) -> None:
        threshold = time.time() - 120
        for key in list(self._groups):
            messages = self._groups[key]
            while messages and messages[0]["received_at"] <= threshold:
                messages.popleft()
            if not messages:
                del self._groups[key]


class AdminManager:
    def __init__(self, store: LocalStore, config: Any):
        self.store = store
        self.config = config
        self.access = AccessControl(config)
        self.messages = RecentMessages()
        self.pager = ResultPager()
        self.menu = OfficialMenu()

    async def handle(self, ctx: Any, text: str, api: QQGroupAPI, *, request_key: str = "") -> Panel:
        self.access.check_group(ctx)
        if len(text) > 16000:
            raise ValueError("命令过长，请拆分操作。")
        command, args = split_head(text)
        command = command or "菜单"
        if command in {"帮助", "help"}:
            return help_panel()
        if command in {"身份", "我的ID"}:
            return Panel(
                "当前身份",
                f"平台 ID：{ctx.platform_id}\n群 OpenID：{ctx.group_id}\n"
                f"成员 OpenID：{ctx.user_id}\nAstrBot 管理员：{'是' if ctx.is_superuser else '否'}\n\n"
                f"受信任管理员配置项：\n{ctx.platform_id}|{ctx.group_id}|{ctx.user_id}",
                [HOME],
            )
        if command in {"菜单", "面板"}:
            return home_panel()
        if command in {"确认", "取消"}:
            return Panel(
                "旧指令已停用",
                "管理指令已改为直接执行。旧确认记录已失效，请发送新的管理指令。",
                [HOME],
            )
        await self.access.require(ctx, api, global_operation=command in GLOBAL_COMMANDS)
        if command in WRITE_COMMANDS:
            action, params, summary = self._parse_write(ctx, command, args)
            request_key = request_key or (f"message:{ctx.message_id}" if ctx.message_id else "")
            audit_id = await self.store.claim_execution(ctx, request_key, action, params)
            return await self._run_write(ctx, action, params, summary, audit_id, api)
        return await self._read(ctx, command, args, api)

    async def _read(self, ctx: Any, command: str, args: str, api: QQGroupAPI) -> Panel:
        group = ctx.group_id
        if command == "菜单状态":
            if args.strip():
                raise ValueError("用法：/菜单状态，仅查询当前群的官方指令面板。")
            return self.pager.show(
                ctx, "官方指令菜单状态", await self.menu.status(ctx, api), [HOME]
            )
        if command == "结果":
            token, index = split_head(args)
            try:
                index = int(index)
            except ValueError as exc:
                raise ValueError("请使用结果分页按钮，或 /结果 令牌 段号。") from exc
            return self.pager.page(ctx, token, index)
        if command == "信息":
            return self.pager.show(ctx, "群信息", await api.get_group_info(group), [HOME])
        if command == "状态":
            return self.pager.show(ctx, "机器人群内状态", await api.get_bot_state(group), [HOME])
        if command == "禁言状态":
            return self.pager.show(ctx, "群禁言状态", await api.get_mute_state(group), [HOME])
        if command == "成员":
            data = await api.list_members(group, cursor=args.strip())
            buttons = []
            for row in _rows(data, "members")[:6]:
                member = row.get("member_openid")
                if member:
                    buttons.append(
                        ButtonSpec(str(row.get("username") or member)[:10], f"成员详情 {member}")
                    )
            return self.pager.show(
                ctx, "群成员", data, buttons + _page_button(data, "成员") + [HOME]
            )
        if command == "成员详情":
            member = parse_ids(args, 1)[0]
            data = await api.get_member(group, member)
            buttons = [
                ButtonSpec("禁言10分钟", f"禁言 {member} 10m"),
                ButtonSpec("禁言1小时", f"禁言 {member} 1h"),
                ButtonSpec("解除禁言", f"解禁 {member}"),
                ButtonSpec("移出成员", f"移出 {member}", danger=True),
                ButtonSpec("移出并拉黑", f"移出 {member} 拉黑", danger=True),
                HOME,
            ]
            return self.pager.show(ctx, "成员详情", data, buttons)
        if command == "黑名单":
            data = await api.list_blacklist(group, cursor=args.strip())
            buttons = []
            for row in _rows(data, "users")[:4]:
                member = row.get("member_openid")
                if member:
                    buttons.append(
                        ButtonSpec(
                            "解除" + str(row.get("username") or member)[:8], f"取消拉黑 {member}"
                        )
                    )
            return self.pager.show(
                ctx, "群黑名单", data, buttons + _page_button(data, "黑名单") + [HOME]
            )
        if command == "入群":
            data = await api.list_join_requests(group, cursor=args.strip(), limit=4)
            buttons = []
            for index, row in enumerate(_rows(data, "list"), 1):
                if "auto_approved" in row:
                    continue
                member, request = row.get("member_openid"), row.get("join_request_id")
                if member and request and len(buttons) < 8:
                    buttons.extend(
                        [
                            ButtonSpec(f"同意申请{index}", f"同意 {member} {request}"),
                            ButtonSpec(f"拒绝申请{index}", f"拒绝 {member} {request}"),
                        ]
                    )
            return self.pager.show(
                ctx, "入群申请（包含验证信息）", data, buttons + _page_button(data, "入群") + [HOME]
            )
        if command == "策略列表":
            data = await api.list_strategies(cursor=args.strip())
            return self.pager.show(
                ctx,
                "机器人全局审批策略",
                data,
                _page_button(data, "策略列表") + [HOME],
                global_operation=True,
            )
        if command == "日志":
            return self.pager.show(
                ctx, "本群最近操作记录", {"audit": await self.store.recent_audit(ctx)}, [HOME]
            )
        if command == "最近消息":
            rows = self.messages.list(ctx)
            buttons = [
                ButtonSpec(f"撤回消息{i}", f"撤回 {row['message_id']}")
                for i, row in enumerate(rows[:6], 1)
            ]
            return self.pager.show(
                ctx, "最近两分钟可见的消息", {"messages": rows}, buttons + [HOME]
            )
        raise ValueError("未识别的管理命令。发送 /帮助 查看可用命令。")

    def _parse_write(self, ctx: Any, command: str, args: str) -> tuple[str, dict, str]:
        scope = f"群 OpenID：{ctx.group_id}"
        if command in {"设置菜单", "移除菜单"}:
            if args.strip():
                raise ValueError(f"用法：/{command}，仅管理当前群的官方指令面板。")
            return command, {}, f"{scope}\n{command}：当前群的 QQ 官方指令面板。"
        if command == "移出":
            add_blacklist = args.endswith(" 拉黑")
            ids = parse_ids(args[:-3].strip() if add_blacklist else args, 20)
            return (
                command,
                {"members": ids, "add_to_blacklist": add_blacklist},
                (
                    f"{scope}\n移出 {len(ids)} 名成员：\n"
                    + "\n".join(ids)
                    + ("\n同时加入群黑名单。" if add_blacklist else "\n不会同时加入黑名单。")
                ),
            )
        if command in {"拉黑", "取消拉黑", "解禁"}:
            ids = parse_ids(args, 20)
            return (
                command,
                {"members": ids},
                f"{scope}\n{command} {len(ids)} 名成员：\n" + "\n".join(ids),
            )
        if command in {"禁言", "更新禁言"}:
            parts = args.rsplit(maxsplit=1)
            if not parts or (
                len(parts) == 1
                and (
                    re.fullmatch(r"[0-9]+(?:s|m|h|d|秒|分钟|小时|天)", parts[0], re.I)
                    or (parts[0].isdigit() and len(parts[0]) != 32)
                )
            ):
                raise ValueError(
                    f"用法：/{command} @成员 [时长]，例如 /{command} @成员 10m；不填时长默认 10 分钟。"
                )
            ids = parse_ids(parts[0], 20)
            seconds = duration_seconds(parts[1]) if len(parts) == 2 else DEFAULT_MUTE_SECONDS
            return (
                command,
                {"members": ids, "seconds": seconds},
                (f"{scope}\n{command} {len(ids)} 名成员，执行起 {seconds} 秒：\n" + "\n".join(ids)),
            )
        if command in {"同意", "拒绝"}:
            member, rest = split_head(args)
            request, reason = split_head(rest)
            member = parse_ids(member, 1)[0]
            if not request or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,512}", request):
                raise ValueError(f"用法：/{command} 成员OpenID 入群申请ID [拒绝理由]")
            blacklist = False
            if command == "同意" and reason:
                raise ValueError("同意申请不接受拒绝理由或黑名单参数。")
            if command == "拒绝" and (reason == "--拉黑" or reason.startswith("--拉黑 ")):
                blacklist, reason = True, reason[4:].strip()
            if len(reason) > 200:
                raise ValueError("拒绝理由请控制在 200 字以内。")
            params = {
                "member": member,
                "request_id": request,
                "reason": reason,
                "add_to_blacklist": blacklist,
            }
            summary = f"{scope}\n{command}入群申请\n成员：{member}\n申请 ID：{request}"
            if command == "拒绝":
                summary += f"\n理由：{reason or '未填写'}\n同时拉黑：{'是' if blacklist else '否'}"
            return command, params, summary
        if command == "策略创建":
            params = json_object(
                args, {"group_openids", "group_ids", "is_enable", "expire_at", "remark"}
            )
            if ("group_openids" in params) == ("group_ids" in params):
                raise ValueError("创建策略必须选择 group_openids 或 group_ids 中的一种。")
            params.setdefault("is_enable", "on")
            return command, params, self._strategy_summary(command, params)
        if command == "策略修改":
            strategy_id, body = split_head(args)
            params = json_object(body, {"is_enable", "expire_at", "group_action", "remark"})
            params["strategy_id"] = self._strategy_id(strategy_id)
            return command, params, self._strategy_summary(command, params)
        if command in {"策略删除", "策略执行"}:
            params = {"strategy_id": self._strategy_id(args.strip())}
            summary = self._strategy_summary(command, params)
            if command == "策略执行":
                summary += "\n将扫描此策略关联的所有群，官方异步处理预计约 10 分钟。"
            return command, params, summary
        if command == "策略白名单":
            strategy_id, rest = split_head(args)
            op, numbers = split_head(rest)
            if op not in {"add", "del"}:
                raise ValueError("用法：/策略白名单 策略ID add|del QQ号[,QQ号]")
            users = parse_ids(numbers, 10000)
            if any(not value.isascii() or not value.isdigit() for value in users):
                raise ValueError("策略白名单使用 QQ 号码数字字符串，不使用成员 OpenID。")
            params = {
                "strategy_id": self._strategy_id(strategy_id),
                "op": op,
                "whitelist_users": users,
            }
            return command, params, self._strategy_summary(command, params)
        if command == "撤回":
            message_id = args.strip()
            if (
                not message_id
                or len(message_id) > 1024
                or any(char.isspace() for char in message_id)
            ):
                raise ValueError("用法：/撤回 消息ID。ID 来自最近消息或官方消息事件 d.id。")
            return (
                command,
                {"message_id": message_id},
                (
                    f"{scope}\n撤回消息：{message_id}\n平台限制：发送后 2 分钟内；"
                    "撤回普通成员消息要求机器人拥有群管理员身份。"
                ),
            )
        if command in {"通知", "全体通知"}:
            if not args.strip() or len(args) > 1500:
                raise ValueError("通知内容须为 1–1500 字符。")
            if re.search(r"<\s*/?\s*qqbot[-:]|<@|@everyone", args, re.I):
                raise ValueError("通知请使用普通文本；@全体请使用 /全体通知。")
            params = {"text": args, "everyone": command == "全体通知"}
            summary = f"{scope}\n{command}内容：\n{args}"
            if params["everyone"]:
                summary += "\n\n将 @全体成员，机器人必须为管理员，每群每天仅一次。"
            return command, params, summary
        raise ValueError("不支持的写入操作。")

    @staticmethod
    def _strategy_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("请提供正确的策略 ID，使用 /策略列表 查询。")
        return value

    @staticmethod
    def _strategy_summary(command: str, params: dict) -> str:
        summary = f"{command}（机器人全局操作）\n此操作可能影响策略关联的其他群。\n" + json.dumps(
            params, ensure_ascii=False, indent=2
        )
        if command == "策略创建" and "expire_at" not in params:
            summary += "\n未指定到期时间，官方默认有效期为一年。"
        return summary

    async def _run_write(
        self, ctx: Any, action: str, params: dict, summary: str, audit_id: int, api: QQGroupAPI
    ) -> Panel:
        # Each command owns its state even when commands share one transport.
        state = RequestState()
        token = current_request_state.set(state)
        try:
            return await self._run_tracked_write(ctx, action, params, summary, audit_id, api, state)
        finally:
            current_request_state.reset(token)

    async def _run_tracked_write(
        self,
        ctx: Any,
        action: str,
        params: dict,
        summary: str,
        audit_id: int,
        api: QQGroupAPI,
        state: RequestState,
    ) -> Panel:
        try:
            result = await self._execute(ctx, action, params, api)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(
                    audit_id,
                    "unknown" if state.mutation_started else "failed",
                    "任务被中断，请查询官方状态确认结果。"
                    if state.mutation_started
                    else "任务在发送修改请求前被中断，未执行操作。",
                )
            )
            raise
        except QQAPIError as exc:
            mutation_sent = state.mutation_started and not (
                exc.request_sent is False and exc.method in {"POST", "PUT", "PATCH", "DELETE"}
            )
            unknown = mutation_sent and (
                exc.code in ("INVALID_RESPONSE", "TIMEOUT", "NETWORK_ERROR")
                or (exc.status == 0 and exc.code not in ("AUTH_UNAVAILABLE", "TRANSPORT_CLOSED"))
                or exc.status >= 500
            )
            if not mutation_sent:
                title = "未执行操作"
                suffix = "\n未发送修改请求。本次请求不会自动重试，请根据诊断检查后重新发起。"
            elif unknown:
                title = "操作结果尚未确认"
                check = "发送 /菜单状态 查询" if action in {"设置菜单", "移除菜单"} else "查询"
                suffix = f"\n本次请求不会自动重试。请先{check}实际状态，再决定是否发送新指令。"
            else:
                title = "官方接口未执行成功"
                suffix = "\n本次请求不会自动重试。请检查参数及接口权限后发送新指令。"
            detail = exc.user_message() + suffix
            status = "unknown" if unknown else "failed"
            await self._finish(audit_id, status, detail)
            logger.warning(
                "QQ group administration /%s (audit %s, %s): %s",
                action,
                audit_id,
                status,
                exc.user_message(),
            )
            return Panel(title, f"指令：/{action}\n" + detail, [HOME])
        except ValueError as exc:
            if state.mutation_started:
                await self._finish(audit_id, "unknown", "修改请求已发起，但后续校验未完成。")
                return Panel(
                    "操作结果尚未确认",
                    f"修改请求已发起，但后续校验未完成，操作记录 #{audit_id}。请先查询实际状态。",
                    [HOME],
                )
            await self._finish(audit_id, "failed", str(exc))
            return Panel(
                "参数未通过校验", str(exc) + "\n未发送此操作，请修正参数后重新发起。", [HOME]
            )
        except Exception:
            # Once claimed, an unexpected exception must not silently make the
            # same mutation available again. Do not reflect exception secrets.
            unknown = state.mutation_started
            message = (
                "发生内部异常，请先查询实际状态。"
                if unknown
                else "发送修改请求前发生内部异常，未执行操作。"
            )
            await self._finish(audit_id, "unknown" if unknown else "failed", message)
            logger.error("QQ group administration execution failed (audit %s)", audit_id)
            return Panel(
                "操作结果尚未确认" if unknown else "未执行操作",
                f"{message}\n操作记录 #{audit_id}。",
                [HOME],
            )
        failures = result.get("fail_openids") or result.get("add_to_member_blacklist_fail_openids")
        status = "partial" if failures else "success"
        await self._finish(audit_id, status, "部分成员操作失败" if failures else "官方接口返回成功")
        if action == "策略执行":
            return self._write_result(
                ctx,
                action,
                "策略执行请求已受理",
                {"说明": "平台异步处理，通常约 10 分钟；尚不表示申请已通过。", **result},
            )
        return self._write_result(
            ctx,
            action,
            "部分操作未成功" if failures else "操作已完成",
            summary
            + "\n\n"
            + (
                json.dumps(result, ensure_ascii=False, indent=2)
                if result
                else "官方接口已返回成功。"
            ),
        )

    def _write_result(self, ctx: Any, action: str, title: str, data: Any) -> Panel:
        try:
            panel = self.pager.show(
                ctx, title, data, [HOME], global_operation=action in GLOBAL_COMMANDS
            )
            if not any(button.command.startswith("结果 ") for button in panel.buttons):
                panel.title = title
            return panel
        except ValueError:
            # Rendering failure after a completed write must not suggest that
            # the operation never ran or encourage a blind retry.
            return Panel(
                title,
                "官方接口已返回结果，但内容过大无法完整显示。请通过 /日志 和对应状态查询核实，勿直接重复操作。",
                [HOME],
            )

    async def _finish(self, audit_id: int, status: str, detail: str) -> None:
        try:
            await self.store.finish(audit_id, status, detail)
        except Exception:
            logger.error("Could not finalize QQ administration audit %s (%s)", audit_id, status)

    async def _execute(self, ctx: Any, action: str, params: dict, api: QQGroupAPI) -> dict:
        group = ctx.group_id
        if action == "设置菜单":
            return await self.menu.install(ctx, api)
        if action == "移除菜单":
            return await self.menu.remove(ctx, api)
        if action == "移出":
            return await api.remove_members(
                group, params["members"], add_to_blacklist=params["add_to_blacklist"]
            )
        if action in {"拉黑", "取消拉黑"}:
            return await api.update_blacklist(
                group, "add" if action == "拉黑" else "del", params["members"]
            )
        if action in {"禁言", "更新禁言", "解禁"}:
            op = {"禁言": "add", "更新禁言": "update", "解禁": "del"}[action]
            members = [{"op": op, "member_openid": member} for member in params["members"]]
            if op != "del":
                expiry = (datetime.now(UTC) + timedelta(seconds=params["seconds"])).isoformat(
                    timespec="seconds"
                )
                for member in members:
                    member["mute_expire_at"] = expiry
            return await api.set_member_mutes(group, members)
        if action in {"同意", "拒绝"}:
            return await api.approve_join_request(
                group,
                params["member"],
                "approve" if action == "同意" else "decline",
                join_request_id=params["request_id"],
                reject_reason=params["reason"],
                add_to_blacklist=params["add_to_blacklist"],
            )
        if action == "策略创建":
            return await api.create_strategy(**params)
        if action == "策略修改":
            return await api.update_strategy(**params)
        if action == "策略删除":
            return await api.delete_strategy(params["strategy_id"])
        if action == "策略执行":
            return await api.execute_strategy(params["strategy_id"])
        if action == "策略白名单":
            return await api.update_strategy_whitelist(**params)
        if action == "撤回":
            return await api.delete_message(group, params["message_id"])
        if action in {"通知", "全体通知"}:
            payload: dict[str, Any] = {"msg_type": 0, "content": params["text"]}
            if params["everyone"]:
                payload["content"] = "<qqbot-at-everyone />\n" + payload["content"]
            reply = ctx.event_id or ctx.message_id
            if not reply:
                raise ValueError("缺少消息或按钮的回复凭据，请重新发起操作。")
            payload["event_id" if ctx.event_id else "msg_id"] = reply
            payload["msg_seq"] = await api.transport.next_message_sequence(group, reply)
            result = await api.send_message(group, payload)
            if result.get("id") and self.config.get("remember_messages", True):
                self.messages.remember(ctx, message_id=result["id"], author="机器人")
            return result
        raise ValueError("保存的操作类型不受支持，请重新发起。")
