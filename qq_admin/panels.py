"""QQ Markdown management panels without an AstrBot runtime dependency.

Panel titles and bodies are plain text. They are escaped at the final rendering
boundary, including QQ's XML-like mention and command tags. Callback payloads
carry their original context, but the command handler must still authorize the
actual event and validate any confirmation token before making a change.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# A conservative presentation budget, not an assertion about QQ's API limit.
# Oversized confirmation panels fail explicitly rather than hiding their target.
MAX_MARKDOWN_LENGTH = 3800
MAX_BUTTONS = 25
BUTTONS_PER_ROW = 3
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|~])")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PanelContext(Protocol):
    platform_id: str
    group_id: str
    user_id: str
    is_superuser: bool


@dataclass(frozen=True)
class ButtonSpec:
    label: str
    command: str
    kind: str = "callback"
    danger: bool = False


@dataclass
class Panel:
    title: str
    body: str
    buttons: list[ButtonSpec] = field(default_factory=list)


def escape_markdown(value: Any) -> str:
    """Escape plain text, including QQ executable tags and Markdown links.

    Ampersands are escaped first so an existing ``&lt;qqbot-...`` sequence
    cannot become a QQ tag after a renderer decodes entities. Line breaks are
    retained to keep help text and API responses readable.
    """
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARACTERS.sub("", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _markdown_content(panel: Panel) -> str:
    title = escape_markdown(panel.title).replace("\n", " ")
    return f"## {title}\n\n{escape_markdown(panel.body)}"


def build_payload(
    panel: Panel,
    ctx: PanelContext,
    encode_callback: Callable[[str, dict[str, Any]], str],
) -> dict[str, Any]:
    """Build one message; transport supplies msg_id/event_id and msg_seq.

    Callbacks use AstrBot's registered ``qqadmin`` handler and are limited to
    the panel owner on the QQ client. Context values must additionally be
    compared against the actual event by the server-side handler.
    """
    content = _markdown_content(panel)
    if len(content) > MAX_MARKDOWN_LENGTH:
        raise ValueError("面板内容过长，请拆分显示；确认操作的详情不能被截断")
    if len(panel.buttons) > MAX_BUTTONS:
        raise ValueError("单个面板最多 25 个按钮，请分页显示")
    if panel.buttons and not all((ctx.platform_id, ctx.group_id, ctx.user_id)):
        raise ValueError("按钮缺少机器人、群或操作者身份")

    buttons: list[dict[str, Any]] = []
    for index, spec in enumerate(panel.buttons):
        if not isinstance(spec.label, str) or not 1 <= len(spec.label) <= 10:
            raise ValueError("按钮文字须为 1 至 10 个字符")
        if "\n" in spec.label or "\r" in spec.label:
            raise ValueError("按钮文字不能换行")
        if not isinstance(spec.command, str) or not spec.command.strip():
            raise ValueError("按钮指令不能为空")
        if spec.kind not in {"callback", "input"}:
            raise ValueError("按钮类型只支持 callback 或 input")

        action: dict[str, Any] = {
            "type": 1 if spec.kind == "callback" else 2,
            "permission": {"type": 0, "specify_user_ids": [ctx.user_id]},
            "unsupport_tips": "请升级 QQ，或发送 /群管 帮助",
        }
        if spec.kind == "callback":
            action["data"] = encode_callback(
                "qqadmin",
                {
                    "command": spec.command,
                    "platform_id": ctx.platform_id,
                    "group_id": ctx.group_id,
                    "owner_id": ctx.user_id,
                },
            )
        else:
            action.update({"data": f"/群管 {spec.command}", "enter": False})

        button: dict[str, Any] = {
            "id": f"qqadmin_{index}",
            "render_data": {
                "label": spec.label,
                "visited_label": spec.label,
                "style": 3 if spec.danger else (0 if spec.kind == "input" else 1),
            },
            "action": action,
        }
        if spec.danger:
            action["modal"] = {
                "content": f"确认执行“{spec.label}”？请核对上方操作详情。",
                "confirm_text": "确认",
                "cancel_text": "取消",
            }
            if spec.kind == "callback":
                button["group_id"] = "qqadmin_danger"
        buttons.append(button)

    payload: dict[str, Any] = {"msg_type": 2, "markdown": {"content": content}}
    if buttons:
        # At most five rows, each holding at most five buttons. Normal panels
        # use three columns; larger panels expand to fit without dropping any.
        columns = max(BUTTONS_PER_ROW, (len(buttons) + 4) // 5)
        payload["keyboard"] = {
            "content": {
                "rows": [
                    {"buttons": buttons[start : start + columns]}
                    for start in range(0, len(buttons), columns)
                ]
            }
        }
    return payload


def home_panel() -> Panel:
    return Panel(
        title="QQ 群管理",
        body=(
            "选择需要查看的内容。按钮仅供打开此面板的用户操作。\n"
            "管理操作会再次校验当前群身份；入群策略仅限机器人超级管理员。\n"
            "需要填写目标或参数时，请使用 /群管 帮助 中的命令。"
        ),
        buttons=[
            ButtonSpec("群信息", "信息"),
            ButtonSpec("机器人状态", "状态"),
            ButtonSpec("群成员", "成员"),
            ButtonSpec("禁言状态", "禁言状态"),
            ButtonSpec("群黑名单", "黑名单"),
            ButtonSpec("入群申请", "入群"),
            ButtonSpec("入群策略", "策略列表"),
            ButtonSpec("我的身份", "身份"),
            ButtonSpec("使用帮助", "帮助"),
        ],
    )


def help_panel() -> Panel:
    return Panel(
        title="群管使用帮助",
        body=(
            "在本群 @机器人 后发送 /群管 命令。参数用空格分隔；多个成员 OpenID 用逗号分隔。\n"
            "OpenID 来自本群成员列表或“身份”，不可用 QQ 号替代。策略白名单按官方接口使用 QQ 号。\n\n"
            "查看信息\n"
            "/群管 面板｜帮助｜身份｜信息｜状态\n"
            "/群管 成员 [cursor]\n"
            "/群管 成员详情 openid\n"
            "/群管 禁言状态\n"
            "/群管 黑名单 [cursor]\n"
            "/群管 入群 [cursor]\n"
            "/群管 最近消息\n"
            "/群管 日志\n\n"
            "成员管理\n"
            "/群管 禁言 openids 10m\n"
            "/群管 更新禁言 openids 10m\n"
            "/群管 解禁 openids\n"
            "/群管 移出 openids [拉黑]\n"
            "/群管 拉黑 openids\n"
            "/群管 取消拉黑 openids\n"
            "时长示例：60s、10m、2h、1d；可用范围由接口及命令校验决定。\n\n"
            "入群审核\n"
            "/群管 同意 member request\n"
            "/群管 拒绝 member request [reason]\n"
            "member 与 request 请从待处理入群申请中获取。\n\n"
            "入群策略（机器人超级管理员）\n"
            "/群管 策略列表 [cursor]\n"
            "/群管 策略创建 JSON\n"
            "/群管 策略修改 id JSON\n"
            "/群管 策略删除 id\n"
            "/群管 策略执行 id\n"
            "/群管 策略白名单 id add|del qq逗号列表\n\n"
            "消息管理\n"
            "/群管 撤回 messageid\n"
            "/群管 通知 text\n"
            "/群管 全体通知 text\n"
            "撤回限 2 分钟内；撤回普通成员消息要求机器人是群管理员。\n"
            "全体通知要求机器人是群管理员，每群每个自然日限 1 次。\n\n"
            "涉及修改的操作请核对确认面板；按钮不可用时发送：\n"
            "/群管 确认 token\n"
            "/群管 取消 token"
        ),
        buttons=[ButtonSpec("返回面板", "面板"), ButtonSpec("我的身份", "身份")],
    )


def _truncate_for_markdown(text: str, budget: int) -> tuple[str, bool]:
    """Fit ordinary result text by its escaped size, preserving full Unicode."""
    if len(escape_markdown(text)) <= budget:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(escape_markdown(text[:middle])) <= budget:
            low = middle
        else:
            high = middle - 1
    # Favor a full line for JSON/list readability if doing so loses little text.
    end = text.rfind("\n", 0, low)
    if end >= low - 120:
        low = max(0, end)
    return text[:low].rstrip(), True


def result_panel(
    title: str,
    data: Any,
    buttons: list[ButtonSpec] | None = None,
) -> Panel:
    """Present an API result and retain pagination information after truncation.

    Only read/result data is abbreviated. Construct a plain ``Panel`` for a
    confirmation so its full operation and targets are either sent or rejected.
    A caller may pass pagination buttons containing the unmodified cursor.
    """
    if data is None:
        text = "操作已完成。"
    elif isinstance(data, str):
        text = data
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)

    cursor_footer = ""
    if isinstance(data, Mapping) and "next_cursor" in data:
        cursor = data["next_cursor"]
        if cursor not in (None, ""):
            cursor_footer = (
                f"\n\n下一页 next_cursor：{cursor}\n使用下一页按钮，或将此游标作为列表命令参数。"
            )
            if len(escape_markdown(cursor_footer)) > 900:
                cursor_footer = "\n\n返回了较长的 next_cursor，请使用下一页按钮继续。"
        else:
            cursor_footer = "\n\n已到最后一页（next_cursor 为空）。"

    omission_note = "\n\n结果较长，已省略部分内容；请使用分页或成员详情继续查看。"
    overhead = len(_markdown_content(Panel(title, "")))
    budget = (
        MAX_MARKDOWN_LENGTH
        - overhead
        - len(escape_markdown(cursor_footer))
        - len(escape_markdown(omission_note))
    )
    if budget < 1:
        raise ValueError("结果标题过长，请缩短标题")
    shown, truncated = _truncate_for_markdown(text, budget)
    body = shown + (omission_note if truncated else "") + cursor_footer
    return Panel(
        title=title,
        body=body,
        buttons=list(buttons) if buttons is not None else [ButtonSpec("返回面板", "面板")],
    )
