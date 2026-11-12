"""持久化直接执行回执与操作审计，阻止事件重投和按钮连续点击。"""

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

RECEIPT_RETENTION_SECONDS = 24 * 60 * 60


class LocalStore:
    def __init__(self, path: Path | str, retention_days: int = 30):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self.retention_days = min(365, max(1, retention_days))
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            DROP TABLE IF EXISTS pending;
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
                group_id TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                params TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_scope ON audit(platform, group_id, created);
            CREATE TABLE IF NOT EXISTS execution_receipts (
                platform TEXT NOT NULL, group_id TEXT NOT NULL, actor TEXT NOT NULL,
                request_key TEXT NOT NULL, audit_id INTEGER,
                expires REAL NOT NULL,
                PRIMARY KEY (platform, group_id, actor, request_key)
            );
            CREATE INDEX IF NOT EXISTS execution_receipts_expiry ON execution_receipts(expires);
        """)
        now = time.time()
        self._db.execute("DELETE FROM execution_receipts WHERE expires<=?", (now,))
        self._db.execute("DELETE FROM audit WHERE created<?", (now - self.retention_days * 86400,))
        self._db.execute(
            "UPDATE audit SET status='unknown',detail='插件重启，未确认最终结果，请查询官方状态。' "
            "WHERE status='executing'"
        )
        self._db.commit()

    @staticmethod
    def _scope(ctx: Any) -> tuple[str, str, str]:
        scope = (ctx.platform_id, ctx.group_id, ctx.user_id)
        if any(not isinstance(value, str) or not value.strip() for value in scope):
            raise ValueError("缺少机器人、群或操作者身份，未执行操作。")
        return scope

    async def claim_execution(self, ctx: Any, request_key: str, action: str, params: dict) -> int:
        """鉴权后原子登记一次执行；成功提交事务后调用方才可发送写请求。

        同一平台、群、操作者及 request_key 在 24 小时内只返回一次 audit_id。
        action 或 params 的变化不会允许同一请求再次执行。回执与审计共同提交，
        即使进程在调用官方接口前后中断，也不能通过事件重投再次发起写操作。
        """
        scope = self._scope(ctx)
        if (
            not isinstance(request_key, str)
            or not request_key.strip()
            or len(request_key) > 2048
            or any(ord(character) < 32 for character in request_key)
        ):
            raise ValueError("缺少有效的请求标识，未执行操作。请重新发送命令或打开面板。")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("缺少操作类型，未执行操作。")
        if not isinstance(params, dict):
            raise ValueError("操作参数无效，未执行操作。")
        audit_params = dict(params)
        if "text" in audit_params:
            # 执行使用调用方持有的原始参数；通知正文从不写入回执或审计。
            audit_params["text"] = f"[正文 {len(str(audit_params['text']))} 字符]"
        payload = json.dumps(
            audit_params, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )

        def write() -> int:
            with self._lock, self._db:
                now = time.time()
                self._db.execute("DELETE FROM execution_receipts WHERE expires<=?", (now,))
                receipt = self._db.execute(
                    "INSERT INTO execution_receipts(platform,group_id,actor,request_key,expires) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(platform,group_id,actor,request_key) DO NOTHING",
                    (*scope, request_key, now + RECEIPT_RETENTION_SECONDS),
                )
                if receipt.rowcount != 1:
                    raise ValueError(
                        "这条命令或按钮已经处理，不会重复执行。请先查询操作日志或实际状态。"
                    )
                cursor = self._db.execute(
                    "INSERT INTO audit(platform,group_id,actor,action,params,status,created) "
                    "VALUES(?,?,?,?,?,'executing',?)",
                    (*scope, action, payload, now),
                )
                audit_id = int(cursor.lastrowid)
                self._db.execute(
                    "UPDATE execution_receipts SET audit_id=? "
                    "WHERE platform=? AND group_id=? AND actor=? AND request_key=?",
                    (audit_id, *scope, request_key),
                )
                return audit_id

        return await asyncio.to_thread(write)

    async def finish(self, audit_id: int, status: str, detail: str = "") -> None:
        def write():
            with self._lock, self._db:
                self._db.execute(
                    "UPDATE audit SET status=?,detail=? WHERE id=?",
                    # Keep both failure lists for a 20-member batch intact.
                    (status, detail[:8000], audit_id),
                )

        await asyncio.to_thread(write)

    async def recent_audit(self, ctx: Any, limit: int = 10) -> list[dict]:
        def read():
            with self._lock:
                rows = self._db.execute(
                    "SELECT id,actor,action,status,detail,created FROM audit "
                    "WHERE platform=? AND group_id=? AND created>? ORDER BY id DESC LIMIT ?",
                    (
                        ctx.platform_id,
                        ctx.group_id,
                        time.time() - self.retention_days * 86400,
                        min(20, max(1, limit)),
                    ),
                ).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(read)

    async def close(self) -> None:
        def close():
            with self._lock:
                self._db.close()

        await asyncio.to_thread(close)

    async def cleanup(self) -> None:
        def write():
            with self._lock, self._db:
                now = time.time()
                self._db.execute("DELETE FROM execution_receipts WHERE expires<=?", (now,))
                self._db.execute(
                    "DELETE FROM audit WHERE created<?", (now - self.retention_days * 86400,)
                )

        await asyncio.to_thread(write)
