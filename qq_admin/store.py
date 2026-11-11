"""持久化一次性确认与操作审计。SQLite 的事务同时防止并发双击与重放。"""

import asyncio
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PendingAction:
    token: str
    action: str
    params: dict[str, Any]
    summary: str
    expires_at: float


class LocalStore:
    def __init__(self, path: Path | str, retention_days: int = 30):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self.retention_days = min(365, max(1, retention_days))
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS pending (
                token TEXT PRIMARY KEY, platform TEXT NOT NULL, group_id TEXT NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, params TEXT NOT NULL,
                summary TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
                group_id TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                params TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_scope ON audit(platform, group_id, created);
        """)
        self._db.execute("DELETE FROM pending WHERE expires<? OR used=1", (time.time(),))
        self._db.execute(
            "DELETE FROM audit WHERE created<?", (time.time() - self.retention_days * 86400,)
        )
        self._db.execute(
            "UPDATE audit SET status='unknown',detail='插件重启，未确认最终结果，请查询官方状态。' "
            "WHERE status='executing'"
        )
        self._db.commit()

    @staticmethod
    def _scope(ctx: Any) -> tuple[str, str, str]:
        return ctx.platform_id, ctx.group_id, ctx.user_id

    @staticmethod
    def _pending(row: sqlite3.Row) -> PendingAction:
        return PendingAction(
            row["token"], row["action"], json.loads(row["params"]), row["summary"], row["expires"]
        )

    async def prepare(
        self, ctx: Any, action: str, params: dict, summary: str, ttl: int = 120
    ) -> PendingAction:
        return await asyncio.to_thread(self._prepare, ctx, action, params, summary, ttl)

    def _prepare(
        self, ctx: Any, action: str, params: dict, summary: str, ttl: int
    ) -> PendingAction:
        now = time.time()
        token = secrets.token_urlsafe(18)
        expires = now + max(30, min(300, ttl))
        payload = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._db:
            self._db.execute("DELETE FROM pending WHERE expires < ?", (now,))
            self._db.execute(
                "DELETE FROM audit WHERE created < ?", (now - self.retention_days * 86400,)
            )
            count = self._db.execute(
                "SELECT COUNT(*) FROM pending WHERE platform=? AND group_id=? AND actor=? AND used=0",
                self._scope(ctx),
            ).fetchone()[0]
            if count >= 20:
                raise ValueError("你有过多未确认的操作，请取消旧操作或等待确认过期。")
            self._db.execute(
                "INSERT INTO pending VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (token, *self._scope(ctx), action, payload, summary, expires),
            )
        return PendingAction(token, action, params, summary, expires)

    def _find(self, ctx: Any, token: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM pending WHERE token=? AND platform=? AND group_id=? AND actor=?",
            (token, *self._scope(ctx)),
        ).fetchone()
        if row is None or row["used"] or row["expires"] <= time.time():
            raise ValueError("确认已过期、已处理或不属于当前操作者。请重新发起操作。")
        return row

    async def peek(self, ctx: Any, token: str) -> PendingAction:
        def read():
            with self._lock:
                return self._pending(self._find(ctx, token))

        return await asyncio.to_thread(read)

    async def cancel(self, ctx: Any, token: str) -> None:
        def write():
            with self._lock, self._db:
                self._find(ctx, token)
                result = self._db.execute(
                    "DELETE FROM pending WHERE token=? AND used=0 AND expires>?",
                    (token, time.time()),
                )
                if result.rowcount != 1:
                    raise ValueError("该操作已经处理或已过期，不能再取消。")

        await asyncio.to_thread(write)

    async def claim(self, ctx: Any, token: str) -> tuple[PendingAction, int]:
        """调用方先重新鉴权，再原子消费令牌并写入执行中审计记录。"""

        def write():
            with self._lock, self._db:
                row = self._find(ctx, token)
                result = self._db.execute(
                    "UPDATE pending SET used=1 WHERE token=? AND used=0 AND expires>?",
                    (token, time.time()),
                )
                if result.rowcount != 1:
                    raise ValueError("该操作已经处理或已过期。")
                params = json.loads(row["params"])
                # 通知正文仅用于待确认执行，不长期进入操作日志。
                if "text" in params:
                    params["text"] = f"[正文 {len(params['text'])} 字符]"
                cursor = self._db.execute(
                    "INSERT INTO audit(platform,group_id,actor,action,params,status,created) "
                    "VALUES(?,?,?,?,?,'executing',?)",
                    (
                        *self._scope(ctx),
                        row["action"],
                        json.dumps(params, ensure_ascii=False),
                        time.time(),
                    ),
                )
                self._db.execute("DELETE FROM pending WHERE token=?", (token,))
                return self._pending(row), int(cursor.lastrowid)

        return await asyncio.to_thread(write)

    async def finish(self, audit_id: int, status: str, detail: str = "") -> None:
        def write():
            with self._lock, self._db:
                self._db.execute(
                    "UPDATE audit SET status=?,detail=? WHERE id=?",
                    (status, detail[:1500], audit_id),
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
                self._db.execute("DELETE FROM pending WHERE expires<? OR used=1", (now,))
                self._db.execute(
                    "DELETE FROM audit WHERE created<?", (now - self.retention_days * 86400,)
                )

        await asyncio.to_thread(write)
