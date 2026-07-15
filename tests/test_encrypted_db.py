"""
加密数据库单元测试

测试范围:
  - database/encrypted_db.py : AES-256 加密数据库 (基于 pysqlcipher3)

测试内容:
  - EncryptedDatabase 初始化 (降级模式, 因为 Linux 无 pysqlcipher3)
  - init() 打开数据库
  - execute() 执行 SQL
  - query() / fetchone() / fetchall() 查询数据
  - session() 事务上下文 (提交 / 回滚)
  - close() 关闭
  - health_check() 健康检查
  - get_status() 状态查询
  - 上下文管理器 async with

由于测试环境无 pysqlcipher3, 所有测试验证降级为普通 SQLite 的行为。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from database.encrypted_db import EncryptedDatabase, _DRIVER_NAME, _HAS_SQLCIPHER


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


# ============================================================================
# 测试: 初始化与降级
# ============================================================================
def test_init_no_path():
    """无 db_path 时 init 抛 ValueError"""
    db = EncryptedDatabase()
    with pytest.raises(ValueError):
        _run(db.init())


def test_init_degraded_mode(tmp_path):
    """Linux 无 pysqlcipher3 时降级为普通 SQLite (无加密)"""
    db_path = tmp_path / "test_enc.db"
    db = EncryptedDatabase(db_path, key="some-secret-key")
    _run(db.init())
    try:
        # 降级模式下 is_encrypted 应为 False
        assert db.is_encrypted is False
        assert _HAS_SQLCIPHER is False
        assert _DRIVER_NAME == "sqlite3"
    finally:
        _run(db.close())


def test_init_no_key_not_encrypted(tmp_path):
    """无密钥时 is_encrypted 为 False"""
    db_path = tmp_path / "test_plain.db"
    db = EncryptedDatabase(db_path)
    _run(db.init())
    try:
        assert db.is_encrypted is False
    finally:
        _run(db.close())


def test_init_with_path_object(tmp_path):
    """init 接受 Path 对象"""
    db_path = tmp_path / "path_obj.db"
    db = EncryptedDatabase()
    _run(db.init(db_path))
    try:
        assert db.db_path == Path(db_path)
    finally:
        _run(db.close())


def test_init_with_str_path(tmp_path):
    """init 接受字符串路径"""
    db_path = str(tmp_path / "str_path.db")
    db = EncryptedDatabase()
    _run(db.init(db_path))
    try:
        assert db.db_path == Path(db_path)
    finally:
        _run(db.close())


def test_init_creates_parent_dir(tmp_path):
    """init 自动创建父目录"""
    db_path = tmp_path / "nested" / "deep" / "dir" / "test.db"
    db = EncryptedDatabase(db_path)
    _run(db.init())
    try:
        assert db_path.parent.exists()
    finally:
        _run(db.close())


def test_init_override_key(tmp_path):
    """init 可覆盖构造时的密钥"""
    db_path = tmp_path / "override.db"
    db = EncryptedDatabase(db_path, key="key1")
    _run(db.init(key="key2"))
    try:
        # 降级模式下加密始终为 False
        assert db.is_encrypted is False
        assert db.key == "key2"
    finally:
        _run(db.close())


# ============================================================================
# 测试: conn 属性
# ============================================================================
def test_conn_before_init_raises():
    """未初始化时访问 conn 抛 RuntimeError"""
    db = EncryptedDatabase()
    with pytest.raises(RuntimeError):
        _ = db.conn


def test_conn_after_init(tmp_path):
    """初始化后 conn 可访问"""
    db = EncryptedDatabase(tmp_path / "conn.db")
    _run(db.init())
    try:
        assert db.conn is not None
    finally:
        _run(db.close())


# ============================================================================
# 测试: execute / query
# ============================================================================
def test_execute_create_and_insert(tmp_path):
    """execute 执行建表与插入"""
    db = EncryptedDatabase(tmp_path / "exec.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)"))
        _run(db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (1, "张三")))
        _run(db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "李四")))
    finally:
        _run(db.close())


def test_query_returns_list_of_dict(tmp_path):
    """query 返回字典列表"""
    db = EncryptedDatabase(tmp_path / "query.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER, val TEXT)"))
        _run(db.execute("INSERT INTO t VALUES (1, 'a')"))
        _run(db.execute("INSERT INTO t VALUES (2, 'b')"))
        rows = _run(db.query("SELECT * FROM t ORDER BY id"))
        assert isinstance(rows, list)
        assert len(rows) == 2
        assert rows[0]["id"] == 1
        assert rows[0]["val"] == "a"
        assert rows[1]["id"] == 2
        assert rows[1]["val"] == "b"
    finally:
        _run(db.close())


def test_query_with_params(tmp_path):
    """query 支持参数化查询"""
    db = EncryptedDatabase(tmp_path / "query_params.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER, val TEXT)"))
        _run(db.execute("INSERT INTO t VALUES (1, 'a')"))
        _run(db.execute("INSERT INTO t VALUES (2, 'b')"))
        rows = _run(db.query("SELECT * FROM t WHERE id = ?", (2,)))
        assert len(rows) == 1
        assert rows[0]["val"] == "b"
    finally:
        _run(db.close())


def test_query_empty_result(tmp_path):
    """query 无匹配时返回空列表"""
    db = EncryptedDatabase(tmp_path / "empty.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER)"))
        rows = _run(db.query("SELECT * FROM t"))
        assert rows == []
    finally:
        _run(db.close())


def test_fetchone(tmp_path):
    """fetchone 返回单行字典或 None"""
    db = EncryptedDatabase(tmp_path / "fetchone.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER, name TEXT)"))
        _run(db.execute("INSERT INTO t VALUES (1, '张三')"))
        row = _run(db.fetchone("SELECT * FROM t WHERE id = ?", (1,)))
        assert row is not None
        assert row["name"] == "张三"

        none_row = _run(db.fetchone("SELECT * FROM t WHERE id = ?", (999,)))
        assert none_row is None
    finally:
        _run(db.close())


def test_fetchall(tmp_path):
    """fetchall 返回多行 (query 别名)"""
    db = EncryptedDatabase(tmp_path / "fetchall.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER)"))
        for i in range(5):
            _run(db.execute("INSERT INTO t VALUES (?)", (i,)))
        rows = _run(db.fetchall("SELECT * FROM t ORDER BY id"))
        assert len(rows) == 5
    finally:
        _run(db.close())


def test_execute_before_init_raises():
    """未初始化时 execute 抛 RuntimeError"""
    db = EncryptedDatabase()
    with pytest.raises(RuntimeError):
        _run(db.execute("SELECT 1"))


def test_query_before_init_raises():
    """未初始化时 query 抛 RuntimeError"""
    db = EncryptedDatabase()
    with pytest.raises(RuntimeError):
        _run(db.query("SELECT 1"))


# ============================================================================
# 测试: session 事务上下文
# ============================================================================
def test_session_commit(tmp_path):
    """session 正常退出时自动提交"""
    db = EncryptedDatabase(tmp_path / "session_commit.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER, val TEXT)"))

        async def _txn():
            async with db.session() as s:
                await s.execute("INSERT INTO t VALUES (?, ?)", (1, "a"))
                await s.execute("INSERT INTO t VALUES (?, ?)", (2, "b"))

        _run(_txn())
        rows = _run(db.query("SELECT * FROM t ORDER BY id"))
        assert len(rows) == 2
    finally:
        _run(db.close())


def test_session_rollback(tmp_path):
    """session 异常时自动回滚"""
    db = EncryptedDatabase(tmp_path / "session_rollback.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER)"))

        async def _txn():
            async with db.session() as s:
                await s.execute("INSERT INTO t VALUES (?)", (1,))
                raise ValueError("故意抛异常触发回滚")

        with pytest.raises(ValueError):
            _run(_txn())
        # 回滚后应无数据
        rows = _run(db.query("SELECT * FROM t"))
        assert rows == []
    finally:
        _run(db.close())


def test_session_query_inside(tmp_path):
    """session 内可执行 query"""
    db = EncryptedDatabase(tmp_path / "session_query.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER)"))
        _run(db.execute("INSERT INTO t VALUES (1)"))

        async def _txn():
            async with db.session() as s:
                rows = await s.query("SELECT * FROM t")
                return rows

        rows = _run(_txn())
        assert len(rows) == 1
    finally:
        _run(db.close())


def test_session_fetchone_inside(tmp_path):
    """session 内可执行 fetchone"""
    db = EncryptedDatabase(tmp_path / "session_fo.db")
    _run(db.init())
    try:
        _run(db.execute("CREATE TABLE t (id INTEGER, name TEXT)"))
        _run(db.execute("INSERT INTO t VALUES (1, 'x')"))

        async def _txn():
            async with db.session() as s:
                row = await s.fetchone("SELECT * FROM t WHERE id = ?", (1,))
                return row

        row = _run(_txn())
        assert row is not None
        assert row["name"] == "x"
    finally:
        _run(db.close())


def test_session_before_init_raises():
    """未初始化时 session 抛 RuntimeError"""
    db = EncryptedDatabase()

    async def _txn():
        async with db.session() as s:
            await s.execute("SELECT 1")

    with pytest.raises(RuntimeError):
        _run(_txn())


# ============================================================================
# 测试: health_check
# ============================================================================
def test_health_check_ok(tmp_path):
    """健康数据库 health_check 返回 True"""
    db = EncryptedDatabase(tmp_path / "health.db")
    _run(db.init())
    try:
        assert _run(db.health_check()) is True
    finally:
        _run(db.close())


def test_health_check_after_close(tmp_path):
    """关闭后 health_check 返回 False"""
    db = EncryptedDatabase(tmp_path / "health_closed.db")
    _run(db.init())
    _run(db.close())
    assert _run(db.health_check()) is False


# ============================================================================
# 测试: close
# ============================================================================
def test_close(tmp_path):
    """close 关闭连接"""
    db = EncryptedDatabase(tmp_path / "close.db")
    _run(db.init())
    _run(db.close())
    assert db._conn is None
    assert db._initialized is False


def test_close_idempotent(tmp_path):
    """重复 close 不报错"""
    db = EncryptedDatabase(tmp_path / "close_twice.db")
    _run(db.init())
    _run(db.close())
    _run(db.close())  # 第二次不应抛异常


# ============================================================================
# 测试: get_status
# ============================================================================
def test_get_status_not_initialized(tmp_path):
    """未初始化时 get_status 返回正确状态"""
    db = EncryptedDatabase(tmp_path / "status.db", key="k")
    status = db.get_status()
    assert status["driver"] == _DRIVER_NAME
    assert status["initialized"] is False
    # 降级模式加密为 False
    assert status["encrypted"] is False


def test_get_status_initialized(tmp_path):
    """初始化后 get_status 返回正确状态"""
    db = EncryptedDatabase(tmp_path / "status_init.db")
    _run(db.init())
    try:
        status = db.get_status()
        assert status["initialized"] is True
        assert status["db_path"] == str(tmp_path / "status_init.db")
        assert status["driver"] == _DRIVER_NAME
    finally:
        _run(db.close())


# ============================================================================
# 测试: 上下文管理器
# ============================================================================
def test_async_context_manager(tmp_path):
    """async with 自动初始化与关闭"""
    async def _run_test():
        async with EncryptedDatabase(tmp_path / "ctx.db") as db:
            await db.execute("CREATE TABLE t (id INTEGER)")
            await db.execute("INSERT INTO t VALUES (1)")
            rows = await db.query("SELECT * FROM t")
            assert len(rows) == 1
        # 退出后应已关闭
        assert db._conn is None

    _run(_run_test())


def test_async_context_manager_already_initialized(tmp_path):
    """async with 已初始化的数据库不再重复 init"""
    db = EncryptedDatabase(tmp_path / "ctx_init.db")
    _run(db.init())

    async def _run_test():
        async with db:
            await db.execute("CREATE TABLE t (id INTEGER)")

    _run(_run_test())
    # 退出后应已关闭
    assert db._conn is None
