"""
数据模型 - SQLAlchemy 2.0 ORM 模型定义
对应原软件的数据库表结构。

说明:
- 联系人 / 消息 / 群组 / 群成员 / 记账记录 / 任务日志 存储于实例库 ({instance_id}_data.db)
- Instance 实例表存储于主库 (data.db)
- 使用 Mapped / mapped_column 的现代声明式语法, 兼容 async + aiosqlite
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Boolean,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有模型的声明基类"""
    pass


# ============================================================================
# 枚举常量 (供业务代码使用, 数据库中以整型 / 字符串存储)
# ============================================================================
class ContactType(enum.IntEnum):
    """联系人类型"""
    PERSON = 1        # 个人
    GROUP = 2         # 群聊
    OFFICIAL = 3      # 公众号


class MessageType(str, enum.Enum):
    """消息类型"""
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    VIDEO = "video"
    VOICE = "voice"
    SYSTEM = "system"


class InstanceStatus(str, enum.Enum):
    """实例状态"""
    RUNNING = "running"
    STOPPED = "stopped"


class TaskStatus(str, enum.Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class BookkeepingStatus(str, enum.Enum):
    """记账记录状态"""
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


# ============================================================================
# 联系人
# ============================================================================
class Contact(Base):
    """联系人 / 群聊 / 公众号"""
    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_instance_wxid", "instance_id", "wxid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wxid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    nickname: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    avatar: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # 1=个人 2=群聊 3=公众号
    type: Mapped[int] = mapped_column(Integer, default=ContactType.PERSON, nullable=False)
    instance_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )

    def __repr__(self) -> str:
        return f"<Contact(wxid={self.wxid!r}, nickname={self.nickname!r}, type={self.type})>"


# ============================================================================
# 消息
# ============================================================================
class Message(Base):
    """聊天消息记录"""
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_instance_msgid", "instance_id", "msg_id"),
        Index("ix_messages_sender", "sender_wxid"),
        Index("ix_messages_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    msg_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sender_wxid: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_wxid: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # text / image / file / video / voice / system
    msg_type: Mapped[str] = mapped_column(String(20), default=MessageType.TEXT.value)
    # True=收到 False=发出
    is_received: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_xml: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:
        return f"<Message(msg_id={self.msg_id!r}, type={self.msg_type}, received={self.is_received})>"


# ============================================================================
# 群组
# ============================================================================
class Group(Base):
    """群聊信息"""
    __tablename__ = "groups"
    __table_args__ = (
        Index("ix_groups_instance_groupwxid", "instance_id", "group_wxid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    group_wxid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    group_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    announcement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_wxid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    # 群成员关系 (群 -> 成员)
    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Group(wxid={self.group_wxid!r}, name={self.group_name!r}, count={self.member_count})>"


# ============================================================================
# 群成员
# ============================================================================
class GroupMember(Base):
    """群成员"""
    __tablename__ = "group_members"
    __table_args__ = (
        Index("ix_group_members_group_wxid", "group_id", "wxid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("groups.id"), nullable=False
    )
    wxid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    join_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 成员 -> 群 反向关系
    group: Mapped["Group"] = relationship(back_populates="members")

    def __repr__(self) -> str:
        return f"<GroupMember(wxid={self.wxid!r}, display_name={self.display_name!r})>"


# ============================================================================
# 记账记录 (对应原软件记账模块)
# ============================================================================
class BookkeepingRecord(Base):
    """记账记录"""
    __tablename__ = "bookkeeping_records"
    __table_args__ = (
        Index("ix_bookkeeping_instance_group", "instance_id", "group_id"),
        Index("ix_bookkeeping_user", "user_wxid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    group_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    user_wxid: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    bank_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default=BookkeepingStatus.PENDING.value
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:
        return f"<BookkeepingRecord(user={self.user_wxid!r}, amount={self.amount}, status={self.status})>"


# ============================================================================
# 实例 (存储于主库 data.db)
# ============================================================================
class Instance(Base):
    """机器人实例 (共享数据, 存储于主库)"""
    __tablename__ = "instances"
    __table_args__ = (
        Index("ix_instances_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    wxid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # running / stopped
    status: Mapped[str] = mapped_column(
        String(20), default=InstanceStatus.STOPPED.value
    )
    config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )

    def __repr__(self) -> str:
        return f"<Instance(id={self.instance_id!r}, wxid={self.wxid!r}, status={self.status})>"


# ============================================================================
# 任务日志
# ============================================================================
class TaskLog(Base):
    """任务执行日志"""
    __tablename__ = "task_logs"
    __table_args__ = (
        Index("ix_task_logs_instance_type", "instance_id", "task_type"),
        Index("ix_task_logs_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.PENDING.value)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:
        return f"<TaskLog(instance={self.instance_id!r}, type={self.task_type!r}, status={self.status})>"


# 实例库需要创建的表
INSTANCE_TABLES = [
    Contact.__table__,
    Message.__table__,
    Group.__table__,
    GroupMember.__table__,
    BookkeepingRecord.__table__,
    TaskLog.__table__,
]

# 主库需要创建的表
MAIN_TABLES = [
    Instance.__table__,
]


__all__ = [
    "Base",
    "ContactType",
    "MessageType",
    "InstanceStatus",
    "TaskStatus",
    "BookkeepingStatus",
    "Contact",
    "Message",
    "Group",
    "GroupMember",
    "BookkeepingRecord",
    "Instance",
    "TaskLog",
    "INSTANCE_TABLES",
    "MAIN_TABLES",
]
