"""
数据库迁移 - 建表与默认数据填充

提供:
- create_tables(): 创建主库表
- create_instance_tables(): 创建实例库表
- seed_default_data(): 填充默认系统联系人 (如文件传输助手)
- upgrade(): 迁移入口
"""
from __future__ import annotations

from typing import Optional

from loguru import logger
from sqlalchemy import select

from database.db_manager import DatabaseManager
from database.models import Contact, ContactType, Instance, InstanceStatus


# 微信内置系统联系人 (wxid -> 昵称 / 类型)
DEFAULT_SYSTEM_CONTACTS: list[dict] = [
    {
        "wxid": "filehelper",
        "username": "filehelper",
        "nickname": "文件传输助手",
        "type": ContactType.PERSON,
    },
    {
        "wxid": "notifymessage",
        "username": "notifymessage",
        "nickname": "微信团队",
        "type": ContactType.OFFICIAL,
    },
    {
        "wxid": "fmessage",
        "username": "fmessage",
        "nickname": "朋友推荐消息",
        "type": ContactType.PERSON,
    },
    {
        "wxid": "medianote",
        "username": "medianote",
        "nickname": "语音记事本",
        "type": ContactType.PERSON,
    },
    {
        "wxid": "floatbottle",
        "username": "floatbottle",
        "nickname": "漂流瓶",
        "type": ContactType.PERSON,
    },
    {
        "wxid": "service_notification",
        "username": "service_notification",
        "nickname": "服务通知",
        "type": ContactType.OFFICIAL,
    },
]


async def create_tables(db_manager: DatabaseManager) -> None:
    """创建主库 (共享) 表"""
    await db_manager.init_main_db()
    logger.info("主库表已创建/确认")


async def create_instance_tables(
    db_manager: DatabaseManager, instance_id: str
) -> None:
    """创建实例库表"""
    await db_manager.init_instance_db(instance_id)
    logger.info(f"实例库表已创建/确认: instance_id={instance_id}")


async def seed_default_data(
    db_manager: DatabaseManager, instance_id: str
) -> None:
    """填充默认系统联系人 (幂等, 已存在则跳过)"""
    db_name = DatabaseManager.instance_db_name(instance_id)
    inserted = 0
    async with db_manager.get_session(db_name) as session:
        for item in DEFAULT_SYSTEM_CONTACTS:
            wxid = item["wxid"]
            existing = await session.execute(
                select(Contact).where(
                    Contact.instance_id == instance_id,
                    Contact.wxid == wxid,
                )
            )
            if existing.scalar_one_or_none() is not None:
                continue
            session.add(
                Contact(
                    instance_id=instance_id,
                    wxid=wxid,
                    username=item["username"],
                    nickname=item["nickname"],
                    type=item["type"],
                )
            )
            inserted += 1
        await session.commit()
    logger.info(f"默认系统联系人已填充: instance_id={instance_id}, 新增={inserted}")


async def upsert_instance_record(
    db_manager: DatabaseManager,
    instance_id: str,
    display_name: str = "",
    wxid: str = "",
    status: str = InstanceStatus.STOPPED.value,
    config_json: str = "",
) -> Instance:
    """在主库中插入或更新实例记录 (供 instance_manager 使用)"""
    from config.settings import settings

    async with db_manager.get_session(settings.main_db_name) as session:
        result = await session.execute(
            select(Instance).where(Instance.instance_id == instance_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            record = Instance(
                instance_id=instance_id,
                display_name=display_name or instance_id,
                wxid=wxid,
                status=status,
                config_json=config_json,
            )
            session.add(record)
            logger.info(f"已创建实例记录: {instance_id}")
        else:
            if display_name:
                record.display_name = display_name
            if wxid:
                record.wxid = wxid
            record.status = status
            if config_json:
                record.config_json = config_json
        await session.commit()
        await session.refresh(record)
        return record


async def upgrade(
    db_manager: DatabaseManager,
    instance_id: Optional[str] = None,
    *,
    seed: bool = True,
) -> None:
    """迁移入口

    Args:
        db_manager: 数据库管理器
        instance_id: 若提供则同时初始化实例库并填充默认数据
        seed: 是否填充默认数据
    """
    logger.info("开始执行数据库迁移...")
    await create_tables(db_manager)
    if instance_id:
        await create_instance_tables(db_manager, instance_id)
        if seed:
            await seed_default_data(db_manager, instance_id)
    logger.info("数据库迁移完成")


__all__ = [
    "create_tables",
    "create_instance_tables",
    "seed_default_data",
    "upsert_instance_record",
    "upgrade",
    "DEFAULT_SYSTEM_CONTACTS",
]
