"""Lossless, short-lived result pagination scoped to one QQ group operator."""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .panels import MAX_MARKDOWN_LENGTH, ButtonSpec, Panel, PanelContext, escape_markdown

# Bound both individual results and aggregate memory. Oversized results fail
# explicitly, so a caller can narrow its query without silently losing records.
MAX_SNAPSHOT_CHARACTERS = 262_144
MAX_CACHED_CHARACTERS = 2_097_152
_FOOTER_RESERVE = 450
_PAGE_TITLE_RESERVE = 40


@dataclass(frozen=True)
class _Snapshot:
    scope: tuple[str, str, str]
    title: str
    chunks: tuple[str, ...]
    buttons: tuple[ButtonSpec, ...]
    pagination_hint: str
    global_operation: bool
    expires_at: float
    character_count: int


def _split_complete_text(text: str, budget: int) -> tuple[str, ...]:
    """Split at line boundaries when possible and never strip or omit text."""
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        cost = len(escape_markdown(line))
        if used + cost <= budget:
            current.append(line)
            used += cost
            continue
        if current:
            chunks.append("".join(current))
            current, used = [], 0
        if cost <= budget:
            current.append(line)
            used = cost
            continue
        # A large JSON value can occupy one line. Split it at Unicode character
        # boundaries, measuring the final escaped size rather than raw length.
        for character in line:
            character_cost = len(escape_markdown(character))
            if used + character_cost > budget:
                chunks.append("".join(current))
                current, used = [], 0
            current.append(character)
            used += character_cost
    if current:
        chunks.append("".join(current))
    return tuple(chunks) or ("",)


class ResultPager:
    """Cache complete read results for five minutes, without persisting them.

    ``page`` uses zero-based indices; user-visible labels use one-based numbers.
    Scope checks supplement the command handler's current permission check.
    Reading a page does not extend its lifetime.
    """

    def __init__(
        self,
        ttl: float = 300,
        max_snapshots: int = 128,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not 0 < ttl <= 300:
            raise ValueError("结果缓存有效期须在 0 至 300 秒之间")
        if (
            not isinstance(max_snapshots, int)
            or isinstance(max_snapshots, bool)
            or not 1 <= max_snapshots <= 128
        ):
            raise ValueError("结果缓存最多保存 128 份快照")
        self._ttl = float(ttl)
        self._capacity = int(max_snapshots)
        self._clock = clock or time.monotonic
        self._snapshots: OrderedDict[str, _Snapshot] = OrderedDict()
        self._cached_characters = 0
        self._lock = threading.RLock()

    @staticmethod
    def _scope(ctx: PanelContext) -> tuple[str, str, str]:
        scope = (ctx.platform_id, ctx.group_id, ctx.user_id)
        if not all(isinstance(value, str) and value for value in scope):
            raise ValueError("结果分页缺少机器人、群或操作者身份")
        return scope

    def _discard(self, token: str) -> None:
        self._cached_characters -= self._snapshots.pop(token).character_count

    def _cleanup(self, now: float) -> None:
        expired = [
            token for token, snapshot in self._snapshots.items() if snapshot.expires_at <= now
        ]
        for token in expired:
            self._discard(token)

    def clear(self) -> None:
        """Release cached results when the plugin is unloaded."""
        with self._lock:
            self._snapshots.clear()
            self._cached_characters = 0

    def cleanup(self) -> None:
        """Remove expired snapshots even when no further queries arrive."""
        with self._lock:
            self._cleanup(self._clock())

    def show(
        self,
        ctx: PanelContext,
        title: str,
        data: Any,
        buttons: list[ButtonSpec] | None = None,
        *,
        global_operation: bool = False,
    ) -> Panel:
        scope = self._scope(ctx)
        if global_operation and not ctx.is_superuser:
            raise ValueError("此结果包含机器人全局策略，仅 AstrBot 管理员可以查看")
        if not isinstance(title, str):
            raise ValueError("结果标题须为文本")
        if data is None:
            text = "操作已完成。"
        elif isinstance(data, str):
            text = data
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        if len(text) > MAX_SNAPSHOT_CHARACTERS:
            raise ValueError("本次结果过大，无法完整显示；请缩小查询范围或使用成员详情")
        base_buttons = tuple(buttons) if buttons is not None else (ButtonSpec("返回面板", "面板"),)
        if len(base_buttons) > 10:
            raise ValueError("结果分页每段最多附带 10 个操作按钮")

        title_size = len(escape_markdown(title).replace("\n", " "))
        budget = MAX_MARKDOWN_LENGTH - title_size - 5 - _FOOTER_RESERVE - _PAGE_TITLE_RESERVE
        if budget < 256:
            raise ValueError("结果标题过长，请缩短标题")
        chunks = _split_complete_text(text, budget)
        hint = ""
        if isinstance(data, Mapping) and "next_cursor" in data:
            if data["next_cursor"] not in (None, ""):
                hint = "接口仍有下一页；完整结果中包含 next_cursor。"
            else:
                hint = "接口已到最后一页。"

        with self._lock:
            now = self._clock()
            self._cleanup(now)
            while self._snapshots and (
                len(self._snapshots) >= self._capacity
                or self._cached_characters + len(text) > MAX_CACHED_CHARACTERS
            ):
                self._discard(next(iter(self._snapshots)))
            token = secrets.token_urlsafe(18)
            while token in self._snapshots:
                token = secrets.token_urlsafe(18)
            snapshot = _Snapshot(
                scope,
                title,
                chunks,
                base_buttons,
                hint,
                bool(global_operation),
                now + self._ttl,
                len(text),
            )
            self._snapshots[token] = snapshot
            self._cached_characters += len(text)
            return self._panel(token, snapshot, 0)

    def page(self, ctx: PanelContext, token: str, index: int) -> Panel:
        scope = self._scope(ctx)
        if not isinstance(token, str) or not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("结果分页参数无效，请重新查询")
        with self._lock:
            self._cleanup(self._clock())
            snapshot = self._snapshots.get(token)
            if snapshot is None or snapshot.scope != scope:
                raise ValueError("结果已过期或不属于当前操作者、群及机器人，请重新查询")
            if snapshot.global_operation and not ctx.is_superuser:
                raise ValueError("此结果包含机器人全局策略，仅 AstrBot 管理员可以查看")
            if not 0 <= index < len(snapshot.chunks):
                raise ValueError("结果段号超出范围，请使用分页按钮")
            return self._panel(token, snapshot, index)

    @staticmethod
    def _panel(token: str, snapshot: _Snapshot, index: int) -> Panel:
        total = len(snapshot.chunks)
        footer = f"\n\n结果分页：第 {index + 1}/{total} 段；查询后 5 分钟内可翻阅。"
        if snapshot.pagination_hint:
            footer += "\n" + snapshot.pagination_hint
        navigation: list[ButtonSpec] = []
        if index > 0:
            command = f"结果 {token} {index - 1}"
            navigation.append(ButtonSpec("上一段", command))
            footer += f"\n上一段：/{command}"
        if index + 1 < total:
            command = f"结果 {token} {index + 1}"
            navigation.append(ButtonSpec("下一段", command))
            footer += f"\n下一段：/{command}"
        return Panel(
            title=f"{snapshot.title}（{index + 1}/{total}）",
            body=snapshot.chunks[index] + footer,
            buttons=navigation + list(snapshot.buttons),
        )
