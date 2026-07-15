"""
数据库层单元测试

测试范围:
  - database/models.py   : SQLAlchemy ORM 模型建表
  - database/db_manager.py : DatabaseManager (SQLAlchemy 引擎/会话管理)
  - database/migrations.py  : 建表与种子数据填充
  - database/manager.py     : aiosqlite 版 DatabaseManager (用于 firewall 等模块)

所有测试使用临时目录存放数据库文件, 不污染项目 data 目录。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path, 确保导入正确
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sqlalchemy import select, text

from database.models import (
    Base,
    Contact,
    ContactType,
    Group,
    GroupMember,
    Message,
    BookkeepingRecord,
    Instance,
    InstanceStatus,
    TaskLog,
    TaskStatus,
    MessageType,
    BookkeepingStatus,
    INSTANCE_TABLES,
    MAIN_TABLES,
)
from database.db_manager import DatabaseManager
from database import migrations
from database.migrations import DEFAULT_SYSTEM_CONTACTS


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


def _make_temp_dir() -> Path:
    """创建临时目录"""
    return Path(tempfile.mkdtemp(prefix="robot3_test_db_"))


def _make_db_manager() -> DatabaseManager:
    """创建使用临时目录的 DatabaseManager"""
    return DatabaseManager(db_dir=_make_temp_dir(), echo=False, log_sql=False)


# ============================================================================
# 测试: 模型创建
# ============================================================================
def test_models_creation():
    """验证所有 SQLAlchemy 模型可正确创建表"""
    dbm = _make_db_manager()

    async def _run_test():
        # 创建实例库 (包含 INSTANCE_TABLES)
        await dbm.init_instance_db("test_model_instance")
        db_name = DatabaseManager.instance_db_name("test_model_instance")

        # 验证实例库的每张表都存在
        engine = dbm.get_engine(db_name)
        async with engine.connect() as conn:
            for table_obj in INSTANCE_TABLES:
                result = await conn.execute(
                    text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_obj.name}'")
                )
                row = result.fetchone()
                assert row is not None, f"表 {table_obj.name} 未创建"

        # 创建主库 (包含 MAIN_TABLES)
        await dbm.init_main_db()
        main_engine = dbm.get_engine("data.db")
        async with main_engine.connect() as conn:
            for table_obj in MAIN_TABLES:
                result = await conn.execute(
                    text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_obj.name}'")
                )
                row = result.fetchone()
                assert row is not None, f"主库表 {table_obj.name} 未创建"

        await dbm.close_all()

    _run(_run_test())


def test_models_enums():
    """验证模型枚举常量完整性"""
    # 联系人类型
    assert ContactType.PERSON == 1
    assert ContactType.GROUP == 2
    assert ContactType.OFFICIAL == 3

    # 消息类型
    assert MessageType.TEXT.value == "text"
    assert MessageType.IMAGE.value == "image"
    assert MessageType.FILE.value == "file"
    assert MessageType.VIDEO.value == "video"
    assert MessageType.VOICE.value == "voice"
    assert MessageType.SYSTEM.value == "system"

    # 实例状态
    assert InstanceStatus.RUNNING.value == "running"
    assert InstanceStatus.STOPPED.value == "stopped"

    # 任务状态
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.RUNNING.value == "running"
    assert TaskStatus.SUCCESS.value == "success"
    assert TaskStatus.FAILED.value == "failed"

    # 记账状态
    assert BookkeepingStatus.PENDING.value == "pending"
    assert BookkeepingStatus.CONFIRMED.value == "confirmed"
    assert BookkeepingStatus.REJECTED.value == "rejected"


# ============================================================================
# 测试: DatabaseManager 初始化
# ============================================================================
def test_db_manager_init():
    """测试 DatabaseManager 初始化主库和实例库"""
    dbm = _make_db_manager()

    async def _run_test():
        # 初始化主库 + 实例库
        await dbm.init_db("init_test_instance")

        # 验证引擎已创建
        assert "data.db" in dbm._engines, "主库引擎未创建"
        assert "init_test_instance_data.db" in dbm._engines, "实例库引擎未创建"

        # 验证会话工厂已创建
        assert "data.db" in dbm._session_factories
        assert "init_test_instance_data.db" in dbm._session_factories

        # 验证 get_session 可正常获取会话
        session = dbm.get_session("data.db")
        assert session is not None

        # 验证健康检查
        health = await dbm.health_check()
        assert health["ok"] is True, f"健康检查失败: {health}"

        # 验证状态信息
        status = dbm.get_status()
        assert "data.db" in status["databases"]
        assert "init_test_instance_data.db" in status["databases"]

        await dbm.close_all()

    _run(_run_test())


def test_db_manager_instance_db_name():
    """测试实例数据库文件名生成"""
    assert DatabaseManager.instance_db_name("c6801") == "c6801_data.db"
    assert DatabaseManager.instance_db_name("test") == "test_data.db"


# ============================================================================
# 测试: CRUD 操作
# ============================================================================
def test_crud_operations():
    """测试 Contact / Message / Group / GroupMember 等表的增删改查"""
    dbm = _make_db_manager()
    instance_id = "crud_test"

    async def _run_test():
        await dbm.init_instance_db(instance_id)
        db_name = DatabaseManager.instance_db_name(instance_id)

        # --- Contact 增查改 ---
        async with dbm.get_session(db_name) as session:
            contact = Contact(
                instance_id=instance_id,
                wxid="wxid_crud_001",
                nickname="测试用户",
                remark="备注A",
                type=ContactType.PERSON,
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
            assert contact.id is not None, "Contact ID 未生成"
            assert contact.nickname == "测试用户"

        # 查询
        async with dbm.get_session(db_name) as session:
            result = await session.execute(
                select(Contact).where(Contact.wxid == "wxid_crud_001")
            )
            found = result.scalar_one_or_none()
            assert found is not None, "Contact 查询失败"
            assert found.remark == "备注A"

            # 修改
            found.remark = "备注B"
            await session.commit()

        # 验证修改
        async with dbm.get_session(db_name) as session:
            result = await session.execute(
                select(Contact).where(Contact.wxid == "wxid_crud_001")
            )
            updated = result.scalar_one_or_none()
            assert updated.remark == "备注B", "Contact 修改未生效"

        # --- Message 增查 ---
        async with dbm.get_session(db_name) as session:
            msg = Message(
                instance_id=instance_id,
                msg_id="msg_001",
                sender_wxid="wxid_crud_001",
                receiver_wxid="wxid_self",
                content="你好，这是一条测试消息",
                msg_type=MessageType.TEXT.value,
                is_received=True,
            )
            session.add(msg)
            await session.commit()
            await session.refresh(msg)
            assert msg.id is not None
            assert msg.content == "你好，这是一条测试消息"

        async with dbm.get_session(db_name) as session:
            result = await session.execute(select(Message))
            messages = result.scalars().all()
            assert len(messages) == 1

        # --- Group + GroupMember 关联增查删 ---
        async with dbm.get_session(db_name) as session:
            group = Group(
                instance_id=instance_id,
                group_wxid="123456@chatroom",
                group_name="测试群",
                member_count=2,
                owner_wxid="wxid_crud_001",
            )
            session.add(group)
            await session.commit()
            await session.refresh(group)

            # 添加群成员
            member1 = GroupMember(
                group_id=group.id, wxid="wxid_member_1", display_name="成员一"
            )
            member2 = GroupMember(
                group_id=group.id, wxid="wxid_member_2", display_name="成员二"
            )
            session.add_all([member1, member2])
            await session.commit()

        # 验证群与成员的关联关系 (直接查询 GroupMember, 异步模式不支持延迟加载)
        async with dbm.get_session(db_name) as session:
            result = await session.execute(
                select(Group).where(Group.group_wxid == "123456@chatroom")
            )
            grp = result.scalar_one_or_none()
            assert grp is not None
            assert grp.group_name == "测试群"

            # 直接查询群成员
            member_result = await session.execute(
                select(GroupMember).where(GroupMember.group_id == grp.id)
            )
            members = member_result.scalars().all()
            assert len(members) == 2, f"群成员关联查询失败: 找到 {len(members)} 个成员"

        # 删除群 (级联删除成员)
        async with dbm.get_session(db_name) as session:
            result = await session.execute(
                select(Group).where(Group.group_wxid == "123456@chatroom")
            )
            grp = result.scalar_one_or_none()
            assert grp is not None
            await session.delete(grp)
            await session.commit()

        # 验证级联删除
        async with dbm.get_session(db_name) as session:
            result = await session.execute(select(GroupMember))
            remaining = result.scalars().all()
            assert len(remaining) == 0, "级联删除群成员失败"

        # --- BookkeepingRecord ---
        async with dbm.get_session(db_name) as session:
            record = BookkeepingRecord(
                instance_id=instance_id,
                user_wxid="wxid_crud_001",
                amount=99.5,
                bank_name="工商银行",
                description="午餐",
                status=BookkeepingStatus.PENDING.value,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            assert record.amount == 99.5

        # --- TaskLog ---
        async with dbm.get_session(db_name) as session:
            log = TaskLog(
                instance_id=instance_id,
                task_type="send_message",
                status=TaskStatus.SUCCESS.value,
                result="发送成功",
            )
            session.add(log)
            await session.commit()
            await session.refresh(log)
            assert log.task_type == "send_message"

        await dbm.close_all()

    _run(_run_test())


def test_crud_instance_main_db():
    """测试主库 Instance 表的增删改查"""
    dbm = _make_db_manager()

    async def _run_test():
        await dbm.init_main_db()

        # 新增
        async with dbm.get_session("data.db") as session:
            inst = Instance(
                instance_id="main_crud_001",
                display_name="主库测试实例",
                wxid="wxid_main_001",
                status=InstanceStatus.STOPPED.value,
                config_json='{"key": "value"}',
            )
            session.add(inst)
            await session.commit()
            await session.refresh(inst)
            assert inst.id is not None

        # 查询
        async with dbm.get_session("data.db") as session:
            result = await session.execute(
                select(Instance).where(Instance.instance_id == "main_crud_001")
            )
            found = result.scalar_one_or_none()
            assert found is not None
            assert found.display_name == "主库测试实例"

            # 修改
            found.status = InstanceStatus.RUNNING.value
            await session.commit()

        # 验证修改
        async with dbm.get_session("data.db") as session:
            result = await session.execute(
                select(Instance).where(Instance.instance_id == "main_crud_001")
            )
            updated = result.scalar_one_or_none()
            assert updated.status == InstanceStatus.RUNNING.value

        # 删除
        async with dbm.get_session("data.db") as session:
            result = await session.execute(
                select(Instance).where(Instance.instance_id == "main_crud_001")
            )
            to_delete = result.scalar_one_or_none()
            await session.delete(to_delete)
            await session.commit()

        # 验证删除
        async with dbm.get_session("data.db") as session:
            result = await session.execute(
                select(Instance).where(Instance.instance_id == "main_crud_001")
            )
            assert result.scalar_one_or_none() is None

        await dbm.close_all()

    _run(_run_test())


# ============================================================================
# 测试: 多实例独立数据库
# ============================================================================
def test_multi_instance_db():
    """测试多实例独立数据库, 数据互不干扰"""
    dbm = _make_db_manager()

    async def _run_test():
        # 初始化两个实例
        await dbm.init_instance_db("inst_a")
        await dbm.init_instance_db("inst_b")

        db_a = DatabaseManager.instance_db_name("inst_a")
        db_b = DatabaseManager.instance_db_name("inst_b")

        # 实例 A 写入数据
        async with dbm.get_session(db_a) as session:
            session.add(Contact(
                instance_id="inst_a", wxid="wxid_a_001", nickname="用户A1"
            ))
            await session.commit()

        # 实例 B 写入数据
        async with dbm.get_session(db_b) as session:
            session.add(Contact(
                instance_id="inst_b", wxid="wxid_b_001", nickname="用户B1"
            ))
            session.add(Contact(
                instance_id="inst_b", wxid="wxid_b_002", nickname="用户B2"
            ))
            await session.commit()

        # 验证实例 A 只有 1 条
        async with dbm.get_session(db_a) as session:
            result = await session.execute(select(Contact))
            contacts_a = result.scalars().all()
            assert len(contacts_a) == 1
            assert contacts_a[0].nickname == "用户A1"

        # 验证实例 B 有 2 条
        async with dbm.get_session(db_b) as session:
            result = await session.execute(select(Contact))
            contacts_b = result.scalars().all()
            assert len(contacts_b) == 2

        # 验证实例 A 中不存在实例 B 的数据
        async with dbm.get_session(db_a) as session:
            result = await session.execute(
                select(Contact).where(Contact.wxid == "wxid_b_001")
            )
            assert result.scalar_one_or_none() is None

        await dbm.close_all()

    _run(_run_test())


# test_multi_instance_isolation 是 test_multi_instance_db 的别名, 用于兼容
def test_multi_instance_isolation():
    """测试多实例数据库隔离性 (补充验证)"""
    test_multi_instance_db()


# ============================================================================
# 测试: 种子数据
# ============================================================================
def test_seed_data():
    """测试种子数据(文件传输助手等)正确插入"""
    dbm = _make_db_manager()
    instance_id = "seed_test"

    async def _run_test():
        # 初始化实例库
        await dbm.init_instance_db(instance_id)

        # 填充种子数据
        await migrations.seed_default_data(dbm, instance_id)

        db_name = DatabaseManager.instance_db_name(instance_id)

        # 验证所有默认系统联系人已插入
        async with dbm.get_session(db_name) as session:
            result = await session.execute(select(Contact))
            contacts = result.scalars().all()
            assert len(contacts) == len(DEFAULT_SYSTEM_CONTACTS), \
                f"种子数据数量不匹配: 期望 {len(DEFAULT_SYSTEM_CONTACTS)}, 实际 {len(contacts)}"

            # 验证文件传输助手
            result = await session.execute(
                select(Contact).where(Contact.wxid == "filehelper")
            )
            filehelper = result.scalar_one_or_none()
            assert filehelper is not None, "文件传输助手未插入"
            assert filehelper.nickname == "文件传输助手"
            assert filehelper.type == ContactType.PERSON

            # 验证微信团队
            result = await session.execute(
                select(Contact).where(Contact.wxid == "notifymessage")
            )
            wechat_team = result.scalar_one_or_none()
            assert wechat_team is not None, "微信团队未插入"
            assert wechat_team.nickname == "微信团队"
            assert wechat_team.type == ContactType.OFFICIAL

        await dbm.close_all()

    _run(_run_test())


def test_seed_data_idempotent():
    """测试种子数据幂等性 (重复插入不会产生重复记录)"""
    dbm = _make_db_manager()
    instance_id = "seed_idem"

    async def _run_test():
        await dbm.init_instance_db(instance_id)

        # 第一次填充
        await migrations.seed_default_data(dbm, instance_id)
        # 第二次填充 (应跳过已存在的)
        await migrations.seed_default_data(dbm, instance_id)

        db_name = DatabaseManager.instance_db_name(instance_id)
        async with dbm.get_session(db_name) as session:
            result = await session.execute(select(Contact))
            contacts = result.scalars().all()
            assert len(contacts) == len(DEFAULT_SYSTEM_CONTACTS), \
                "重复填充产生了重复记录"

        await dbm.close_all()

    _run(_run_test())


# ============================================================================
# 测试: 加密数据库
# ============================================================================
def test_encrypted_db():
    """
    测试加密数据库 (如有)

    当前项目的 DatabaseManager 未集成 SQLCipher, db_encrypt_key 配置项存在但未启用。
    此测试验证:
      1. settings.db_encrypt_key 字段存在
      2. 不加密时数据库可正常工作
      3. crypto 模块可对数据库内容进行加密/解密往返
    """
    from config.settings import settings
    from security.crypto import CryptoUtils

    # 验证配置项存在
    assert hasattr(settings, "db_encrypt_key"), "settings 缺少 db_encrypt_key 字段"

    # 不加密时数据库正常工作
    dbm = _make_db_manager()

    async def _run_test():
        await dbm.init_instance_db("encrypt_test")
        db_name = DatabaseManager.instance_db_name("encrypt_test")

        async with dbm.get_session(db_name) as session:
            session.add(Contact(
                instance_id="encrypt_test",
                wxid="wxid_encrypted",
                nickname="加密测试用户",
            ))
            await session.commit()

        async with dbm.get_session(db_name) as session:
            result = await session.execute(
                select(Contact).where(Contact.wxid == "wxid_encrypted")
            )
            found = result.scalar_one_or_none()
            assert found is not None
            assert found.nickname == "加密测试用户"

        await dbm.close_all()

    _run(_run_test())

    # 验证 crypto 模块可对数据进行加密/解密往返 (模拟加密存储场景)
    crypto = CryptoUtils(master_secret="test_encrypt_key_123")
    original_data = {"wxid": "wxid_secret", "nickname": "秘密用户", "amount": 123.45}
    encrypted = crypto.encrypt_config(original_data)
    assert encrypted != str(original_data), "加密结果不应等于明文"
    decrypted = crypto.decrypt_config(encrypted)
    assert decrypted == original_data, "解密结果应等于原始数据"


# ============================================================================
# 测试: 完整迁移流程
# ============================================================================
def test_migrations_upgrade():
    """测试完整迁移流程 (主库 + 实例库 + 种子数据)"""
    dbm = _make_db_manager()
    instance_id = "migrate_test"

    async def _run_test():
        # 执行完整迁移
        await migrations.upgrade(dbm, instance_id, seed=True)

        # 验证主库
        assert "data.db" in dbm._engines

        # 验证实例库
        db_name = DatabaseManager.instance_db_name(instance_id)
        assert db_name in dbm._engines

        # 验证种子数据
        async with dbm.get_session(db_name) as session:
            result = await session.execute(select(Contact))
            contacts = result.scalars().all()
            assert len(contacts) > 0, "迁移后种子数据为空"

        await dbm.close_all()

    _run(_run_test())
