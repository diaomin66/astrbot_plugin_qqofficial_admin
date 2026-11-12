"""QQ Markdown management panels without an AstrBot runtime dependency.

Panel titles and bodies are plain text. They are escaped at the final rendering
boundary, including QQ's XML-like mention and command tags. Callback payloads
carry their original context, but the command handler must still authorize the
actual event and deduplicate the operation before making a change.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# A conservative presentation budget, not an assertion about QQ's API limit.
# Oversized panels fail explicitly rather than silently hiding their content.
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
    # Diagnostic identifiers must remain copyable even in QQ clients that
    # display Markdown escape characters instead of interpreting them.
    prefer_plain_text: bool = False


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
        raise ValueError("菜单内容过长，请拆分显示；操作详情不能被截断")
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
            "unsupport_tips": "请升级 QQ，或发送 /帮助",
        }
        if spec.kind == "callback":
            action["data"] = encode_callback(
                "qqadmin",
                {
                    "command": spec.command,
                    "platform_id": ctx.platform_id,
                    "group_id": ctx.group_id,
                    "owner_id": ctx.user_id,
                    "operation_id": secrets.token_urlsafe(18),
                },
            )
        else:
            action.update({"data": f"/{spec.command}", "enter": False})

        button: dict[str, Any] = {
            "id": f"qqadmin_{index}",
            "render_data": {
                "label": spec.label,
                "visited_label": spec.label,
                "style": 3 if spec.danger else (0 if spec.kind == "input" else 1),
            },
            "action": action,
        }
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
        title="群管理菜单",
        body=(
            "点击管理指令，补全 @成员、时长或内容后发送，即可执行。\n"
            "例如：/禁言 @成员 10m；不填时长默认 10 分钟。请在 QQ 中选择真实成员。\n"
            "查询按钮直接查看结果。按钮仅供打开菜单的用户操作，管理功能按当前权限执行。"
        ),
        buttons=[
            ButtonSpec("禁言", "禁言 ", "input"),
            ButtonSpec("解禁", "解禁 ", "input"),
            ButtonSpec("移出成员", "移出 ", "input", danger=True),
            ButtonSpec("成员详情", "成员详情 ", "input"),
            ButtonSpec("发送通知", "通知 ", "input"),
            ButtonSpec("全体通知", "全体通知 ", "input", danger=True),
            ButtonSpec("更新禁言", "更新禁言 ", "input"),
            ButtonSpec("加入黑名单", "拉黑 ", "input", danger=True),
            ButtonSpec("取消拉黑", "取消拉黑 ", "input"),
            ButtonSpec("群成员", "成员"),
            ButtonSpec("入群申请", "入群"),
            ButtonSpec("群黑名单", "黑名单"),
            ButtonSpec("禁言状态", "禁言状态"),
            ButtonSpec("群信息", "信息"),
            ButtonSpec("机器人状态", "状态"),
            ButtonSpec("最近消息", "最近消息"),
            ButtonSpec("操作日志", "日志"),
            ButtonSpec("入群策略", "策略列表"),
            ButtonSpec("我的身份", "身份"),
            ButtonSpec("使用帮助", "帮助"),
        ],
    )


def help_panel() -> Panel:
    return Panel(
        title="管理指令帮助",
        body=(
            "发送 /菜单 打开消息快捷菜单；点击管理指令，补全参数后发送即执行。\n"
            "由 AstrBot 管理员发送 /设置菜单 安装 QQ 客户端原生指令面板。\n"
            "也可直接发送下方短命令，查询与审批按钮点击后直接处理。请在 QQ 中选择真实的 @成员。\n"
            "若机器人只能接收 @它的消息，请同时 @机器人；机器人自身不会作为操作目标。\n"
            "成员目标也可填写 /成员 中的 OpenID；多个 OpenID 用逗号分隔。策略白名单使用 QQ 号。\n\n"
            "查看信息\n"
            "/菜单｜/帮助｜/身份｜/信息｜/状态\n"
            "/菜单状态\n"
            "/成员 [cursor]\n"
            "/成员详情 @成员\n"
            "/禁言状态\n"
            "/黑名单 [cursor]\n"
            "/入群 [cursor]\n"
            "/最近消息\n"
            "/日志\n\n"
            "成员管理\n"
            "/禁言 @成员 [时长]\n"
            "/禁言 @成员A @成员B 10m\n"
            "/更新禁言 @成员 [时长]\n"
            "/解禁 @成员\n"
            "/移出 @成员 [拉黑]\n"
            "/拉黑 openids\n"
            "/取消拉黑 openids\n"
            "禁言或更新禁言不填时长默认 10 分钟。时长示例：600（秒）、10m、30分钟、2h、1d；最长 30 天。\n\n"
            "入群审核\n"
            "/同意 member request\n"
            "/拒绝 member request [reason]\n"
            "member 与 request 请从待处理入群申请中获取。\n\n"
            "菜单设置与入群策略（AstrBot 管理员）\n"
            "/设置菜单｜/移除菜单\n"
            "/策略列表 [cursor]\n"
            "/策略创建 JSON\n"
            "/策略修改 id JSON\n"
            "/策略删除 id\n"
            "/策略执行 id\n"
            "/策略白名单 id add|del qq逗号列表\n\n"
            "消息管理\n"
            "/撤回 messageid\n"
            "/通知 text\n"
            "/全体通知 text\n"
            "撤回限 2 分钟内；撤回普通成员消息要求机器人是群管理员。\n"
            "全体通知要求机器人是群管理员，每群每个自然日限 1 次。\n\n"
            "命令参数完整并通过权限检查后直接执行。按钮不可用时可直接发送对应命令。"
        ),
        buttons=[ButtonSpec("返回菜单", "菜单"), ButtonSpec("我的身份", "身份")],
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

    Only read/result data is abbreviated. Construct a plain ``Panel`` when the
    full details must be sent or rejected. A caller may pass pagination buttons
    containing the unmodified cursor.
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
        buttons=list(buttons) if buttons is not None else [ButtonSpec("返回菜单", "菜单")],
    )
