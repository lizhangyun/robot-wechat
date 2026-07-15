"""
数据库管理器 - 基于 aiosqlite 的异步数据访问层

表结构:
  - instances:   机器人实例
  - messages:    消息记录 (收/发)
  - contacts:    联系人
  - groups:      群
  - group_members: 群成员
  - bookkeeping:  记账记录
  - firewall_black / firewall_white: IP 防火墙名单
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from loguru import logger

from config.settings import settings


class DatabaseManager:
    """异步 SQLite 数据库管理器"""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path: Path = db_path or (settings.db_dir / settings.main_db_name)
        self._db: Optional[aiosqlite.Connection] = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("数据库尚未初始化, 请先调用 init()")
        return self._db

    async def init(self) -> None:
        """初始化数据库连接并建表"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._create_tables()
        await self._db.commit()
        logger.info(f"数据库已初始化: {self.db_path}")

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db is not None:
            await self._db.close()
            self._db = None
            logger.info("数据库连接已关闭")

    async def _create_tables(self) -> None:
        """创建所有表"""
        assert self._db is not None
        # 实例
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS instances (
                instance_id   TEXT PRIMARY KEY,
                display_name  TEXT NOT NULL DEFAULT '',
                wxid          TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'stopped',
                config_json   TEXT NOT NULL DEFAULT '{}',
                created_at    REAL NOT NULL DEFAULT 0,
                started_at    REAL
            )
            """
        )
        # 消息
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id   TEXT NOT NULL,
                wxid          TEXT NOT NULL,
                direction     TEXT NOT NULL,   -- in / out
                msg_type      TEXT NOT NULL DEFAULT 'text', -- text/image/file
                content       TEXT NOT NULL DEFAULT '',
                extra         TEXT NOT NULL DEFAULT '{}',
                created_at    REAL NOT NULL DEFAULT 0
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_instance ON messages(instance_id, wxid)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_time ON messages(created_at DESC)"
        )
        # 联系人
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                instance_id   TEXT NOT NULL,
                wxid          TEXT NOT NULL,
                nickname      TEXT NOT NULL DEFAULT '',
                remark        TEXT NOT NULL DEFAULT '',
                avatar        TEXT NOT NULL DEFAULT '',
                updated_at    REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (instance_id, wxid)
            )
            """
        )
        # 群
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                instance_id   TEXT NOT NULL,
                group_wxid    TEXT NOT NULL,
                name          TEXT NOT NULL DEFAULT '',
                owner         TEXT NOT NULL DEFAULT '',
                member_count  INTEGER NOT NULL DEFAULT 0,
                announcement  TEXT NOT NULL DEFAULT '',
                updated_at    REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (instance_id, group_wxid)
            )
            """
        )
        # 群成员
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_members (
                group_wxid    TEXT NOT NULL,
                wxid          TEXT NOT NULL,
                nickname      TEXT NOT NULL DEFAULT '',
                display_name  TEXT NOT NULL DEFAULT '',
                join_time     REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (group_wxid, wxid)
            )
            """
        )
        # 记账
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS bookkeeping (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id   TEXT NOT NULL,
                wxid          TEXT NOT NULL,
                amount        REAL NOT NULL DEFAULT 0,
                kind          TEXT NOT NULL DEFAULT 'expense', -- income/expense
                category      TEXT NOT NULL DEFAULT '',
                note          TEXT NOT NULL DEFAULT '',
                created_at    REAL NOT NULL DEFAULT 0
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_bk_instance ON bookkeeping(instance_id, created_at DESC)"
        )
        # 防火墙黑/白名单
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS firewall_black (
                ip          TEXT PRIMARY KEY,
                note        TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL DEFAULT 0
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS firewall_white (
                ip          TEXT PRIMARY KEY,
                note        TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL DEFAULT 0
            )
            """
        )

    # ======================== 通用查询 ========================

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        """执行写/DDL 语句"""
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        """查询多行"""
        cur = await self.conn.execute(sql, params)
        return await cur.fetchall()

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        """查询单行"""
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    # ======================== 实例 ========================

    async def upsert_instance(self, instance_id: str, display_name: str, wxid: str,
                              status: str, config_json: dict) -> None:
        await self.execute(
            """
            INSERT INTO instances (instance_id, display_name, wxid, status, config_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_id) DO UPDATE SET
                display_name=excluded.display_name,
                wxid=excluded.wxid,
                status=excluded.status,
                config_json=excluded.config_json
            """,
            (instance_id, display_name, wxid, status, json.dumps(config_json, ensure_ascii=False, default=str), time.time()),
        )

    async def set_instance_status(self, instance_id: str, status: str,
                                  started_at: Optional[float] = None) -> None:
        await self.execute(
            "UPDATE instances SET status=?, started_at=? WHERE instance_id=?",
            (status, started_at, instance_id),
        )

    async def list_instances(self) -> list[dict]:
        rows = await self.fetchall("SELECT * FROM instances ORDER BY created_at DESC")
        return [self._row_to_dict(r) for r in rows]

    async def get_instance(self, instance_id: str) -> Optional[dict]:
        row = await self.fetchone("SELECT * FROM instances WHERE instance_id=?", (instance_id,))
        return self._row_to_dict(row) if row else None

    async def delete_instance(self, instance_id: str) -> None:
        await self.execute("DELETE FROM instances WHERE instance_id=?", (instance_id,))

    async def update_instance_config(self, instance_id: str, config_json: dict) -> None:
        await self.execute(
            "UPDATE instances SET config_json=? WHERE instance_id=?",
            (json.dumps(config_json, ensure_ascii=False, default=str), instance_id),
        )

    # ======================== 消息 ========================

    async def add_message(self, instance_id: str, wxid: str, direction: str,
                          msg_type: str, content: str, extra: Optional[dict] = None) -> int:
        cur = await self.execute(
            """
            INSERT INTO messages (instance_id, wxid, direction, msg_type, content, extra, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (instance_id, wxid, direction, msg_type, content,
             json.dumps(extra or {}, ensure_ascii=False, default=str), time.time()),
        )
        return int(cur.lastrowid or 0)

    async def get_message_history(self, instance_id: str, wxid: str, limit: int = 50) -> list[dict]:
        rows = await self.fetchall(
            """
            SELECT * FROM messages
            WHERE instance_id=? AND wxid=?
            ORDER BY created_at DESC LIMIT ?
            """,
            (instance_id, wxid, limit),
        )
        return [self._row_to_dict(r) for r in rows]

    async def get_recent_messages(self, limit: int = 100, direction: Optional[str] = None) -> list[dict]:
        if direction:
            rows = await self.fetchall(
                "SELECT * FROM messages WHERE direction=? ORDER BY created_at DESC LIMIT ?",
                (direction, limit),
            )
        else:
            rows = await self.fetchall(
                "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [self._row_to_dict(r) for r in rows]

    async def count_messages_today(self) -> int:
        """统计今日消息数"""
        import datetime as _dt
        start = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = await self.fetchone("SELECT COUNT(*) AS c FROM messages WHERE created_at>=?", (start,))
        return int(row["c"]) if row else 0

    # ======================== 联系人 ========================

    async def upsert_contact(self, instance_id: str, wxid: str, nickname: str,
                             remark: str = "", avatar: str = "") -> None:
        await self.execute(
            """
            INSERT INTO contacts (instance_id, wxid, nickname, remark, avatar, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_id, wxid) DO UPDATE SET
                nickname=excluded.nickname, avatar=excluded.avatar, updated_at=excluded.updated_at
            """,
            (instance_id, wxid, nickname, remark, avatar, time.time()),
        )

    async def update_contact_remark(self, instance_id: str, wxid: str, remark: str) -> bool:
        cur = await self.execute(
            "UPDATE contacts SET remark=?, updated_at=? WHERE instance_id=? AND wxid=?",
            (remark, time.time(), instance_id, wxid),
        )
        return cur.rowcount > 0

    async def list_contacts(self, instance_id: str, limit: int = 500) -> list[dict]:
        rows = await self.fetchall(
            "SELECT * FROM contacts WHERE instance_id=? ORDER BY updated_at DESC LIMIT ?",
            (instance_id, limit),
        )
        return [self._row_to_dict(r) for r in rows]

    async def search_contacts(self, instance_id: str, keyword: str, limit: int = 50) -> list[dict]:
        like = f"%{keyword}%"
        rows = await self.fetchall(
            """
            SELECT * FROM contacts
            WHERE instance_id=? AND (wxid LIKE ? OR nickname LIKE ? OR remark LIKE ?)
            ORDER BY updated_at DESC LIMIT ?
            """,
            (instance_id, like, like, like, limit),
        )
        return [self._row_to_dict(r) for r in rows]

    # ======================== 群 ========================

    async def upsert_group(self, instance_id: str, group_wxid: str, name: str = "",
                           owner: str = "", member_count: int = 0) -> None:
        await self.execute(
            """
            INSERT INTO groups (instance_id, group_wxid, name, owner, member_count, announcement, updated_at)
            VALUES (?, ?, ?, ?, ?, '', ?)
            ON CONFLICT(instance_id, group_wxid) DO UPDATE SET
                name=excluded.name, owner=excluded.owner,
                member_count=excluded.member_count, updated_at=excluded.updated_at
            """,
            (instance_id, group_wxid, name, owner, member_count, time.time()),
        )

    async def list_groups(self, instance_id: str) -> list[dict]:
        rows = await self.fetchall(
            "SELECT * FROM groups WHERE instance_id=? ORDER BY updated_at DESC", (instance_id,)
        )
        return [self._row_to_dict(r) for r in rows]

    async def get_group(self, instance_id: str, group_wxid: str) -> Optional[dict]:
        row = await self.fetchone(
            "SELECT * FROM groups WHERE instance_id=? AND group_wxid=?",
            (instance_id, group_wxid),
        )
        return self._row_to_dict(row) if row else None

    async def set_group_announcement(self, instance_id: str, group_wxid: str,
                                     announcement: str) -> None:
        await self.execute(
            "UPDATE groups SET announcement=?, updated_at=? WHERE instance_id=? AND group_wxid=?",
            (announcement, time.time(), instance_id, group_wxid),
        )

    async def upsert_group_member(self, group_wxid: str, wxid: str, nickname: str = "",
                                 display_name: str = "") -> None:
        await self.execute(
            """
            INSERT INTO group_members (group_wxid, wxid, nickname, display_name, join_time)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_wxid, wxid) DO UPDATE SET
                nickname=excluded.nickname, display_name=excluded.display_name
            """,
            (group_wxid, wxid, nickname, display_name, time.time()),
        )

    async def list_group_members(self, group_wxid: str) -> list[dict]:
        rows = await self.fetchall(
            "SELECT * FROM group_members WHERE group_wxid=? ORDER BY nickname", (group_wxid,)
        )
        return [self._row_to_dict(r) for r in rows]

    # ======================== 记账 ========================

    async def add_bookkeeping(self, instance_id: str, wxid: str, amount: float, kind: str,
                              category: str = "", note: str = "") -> int:
        cur = await self.execute(
            """
            INSERT INTO bookkeeping (instance_id, wxid, amount, kind, category, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (instance_id, wxid, amount, kind, category, note, time.time()),
        )
        return int(cur.lastrowid or 0)

    async def list_bookkeeping(self, instance_id: str, limit: int = 100) -> list[dict]:
        rows = await self.fetchall(
            "SELECT * FROM bookkeeping WHERE instance_id=? ORDER BY created_at DESC LIMIT ?",
            (instance_id, limit),
        )
        return [self._row_to_dict(r) for r in rows]

    async def bookkeeping_stats(self, instance_id: str) -> dict:
        rows = await self.fetchall(
            """
            SELECT kind, COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt
            FROM bookkeeping WHERE instance_id=?
            GROUP BY kind
            """,
            (instance_id,),
        )
        stats = {"income": 0.0, "expense": 0.0, "income_count": 0, "expense_count": 0}
        for r in rows:
            kind = r["kind"]
            if kind == "income":
                stats["income"] = float(r["total"])
                stats["income_count"] = int(r["cnt"])
            elif kind == "expense":
                stats["expense"] = float(r["total"])
                stats["expense_count"] = int(r["cnt"])
        stats["balance"] = round(stats["income"] - stats["expense"], 2)
        return stats

    # ======================== 防火墙 ========================

    async def add_firewall_ip(self, table: str, ip: str, note: str = "") -> bool:
        assert table in ("firewall_black", "firewall_white")
        try:
            await self.execute(
                f"INSERT INTO {table} (ip, note, created_at) VALUES (?, ?, ?)",
                (ip, note, time.time()),
            )
            return True
        except aiosqlite.IntegrityError:
            return False  # 已存在

    async def remove_firewall_ip(self, table: str, ip: str) -> bool:
        assert table in ("firewall_black", "firewall_white")
        cur = await self.execute(f"DELETE FROM {table} WHERE ip=?", (ip,))
        return cur.rowcount > 0

    async def list_firewall_ips(self, table: str) -> list[str]:
        assert table in ("firewall_black", "firewall_white")
        rows = await self.fetchall(f"SELECT ip FROM {table} ORDER BY created_at DESC")
        return [r["ip"] for r in rows]

    # ======================== 工具 ========================

    @staticmethod
    def _row_to_dict(row: Optional[aiosqlite.Row]) -> dict:
        if row is None:
            return {}
        d = dict(row)
        # 反序列化 JSON 字段
        for key in ("config_json", "extra"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, ValueError):
                    pass
        return d


# 全局单例
db_manager = DatabaseManager()
