"""
记账模块（对应原软件 jizhang 模块）

功能：
- 解析群消息中的记账指令（如 "记账 100 银行名称 备注"）；
- 将记录持久化到 BookkeepingRecord 表；
- 按群组、银行、用户维度统计；
- 生成日报/周报/月报；
- 支持后端 API 同步（jizhang_domain）；
- 关键词触发机制（可配置触发关键词）。

指令格式：
    记账 <金额> <银行名称> [备注]
    其中金额可为负数（表示退款/支出扣减），如 "记账 -50 微信 退款"。
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# 独立运行支持：将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger
from sqlalchemy import (
    String, Text, Float, DateTime, Integer, select, func, and_, case,
)
from sqlalchemy.orm import Mapped, mapped_column

from config.instance_config import InstanceConfig
from database import Base, Database
from wechat.hook_interface import WeChatHookInterface
from wechat.message_types import MessageData, MessageType

# httpx 为可选依赖（同步后端用），缺失时降级为仅本地
try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


# ====================================================================== #
#  ORM 模型
# ====================================================================== #
class BookkeepingRecord(Base):
    """记账记录 ORM 模型。"""

    __tablename__ = "bookkeeping_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(32), default="", comment="实例ID")
    group_wxid: Mapped[str] = mapped_column(String(64), default="", index=True, comment="群wxid")
    group_name: Mapped[str] = mapped_column(String(128), default="", comment="群名")
    sender_wxid: Mapped[str] = mapped_column(String(64), default="", index=True, comment="记账人wxid")
    sender_name: Mapped[str] = mapped_column(String(128), default="", comment="记账人昵称")
    amount: Mapped[float] = mapped_column(Float, default=0.0, comment="金额(可负)")
    bank_name: Mapped[str] = mapped_column(String(64), default="", index=True, comment="银行/渠道名称")
    remark: Mapped[str] = mapped_column(String(256), default="", comment="备注")
    raw_msg: Mapped[str] = mapped_column(Text, default="", comment="原始消息")
    msg_id: Mapped[str] = mapped_column(String(64), default="", comment="消息ID")
    sync_status: Mapped[int] = mapped_column(Integer, default=0, comment="后端同步状态 0未同步1已同步2失败")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True, comment="记账时间"
    )


# ====================================================================== #
#  指令解析
# ====================================================================== #
class BookkeepingParser:
    """记账指令解析器。"""

    # 默认指令格式：关键词 金额 银行名称 备注
    # 金额可为负；银行名称不含空格；备注为剩余部分（可含空格）
    _PATTERN = re.compile(
        r"^\s*(?P<keyword>[^\s]+)\s+"
        r"(?P<amount>-?\d+(?:\.\d+)?)\s+"
        r"(?P<bank>\S+)\s*"
        r"(?P<remark>.*)$"
    )

    def __init__(self, keyword: str = "记账") -> None:
        self.keyword: str = keyword

    def parse(self, content: str) -> Optional[dict[str, Any]]:
        """解析消息正文。

        Args:
            content: 消息正文（已去除群前缀）。

        Returns:
            解析结果字典 {amount, bank_name, remark}，非记账指令返回 None。
        """
        text = content.strip()
        # 必须以触发关键词开头
        if not text.startswith(self.keyword):
            return None
        m = self._PATTERN.match(text)
        if not m:
            return None
        try:
            amount = float(m.group("amount"))
        except (TypeError, ValueError):
            return None
        return {
            "amount": amount,
            "bank_name": m.group("bank").strip(),
            "remark": m.group("remark").strip(),
        }


# ====================================================================== #
#  记账模块
# ====================================================================== #
class BookkeepingModule:
    """记账模块。

    Args:
        client: 微信客户端接口。
        db: 异步数据库管理器。
        instance_config: 实例配置（含 jizhang_domain / jizhang_keyword）。
    """

    def __init__(
        self,
        client: WeChatHookInterface,
        db: Database,
        instance_config: Optional[InstanceConfig] = None,
    ) -> None:
        self.client: WeChatHookInterface = client
        self.db: Database = db
        self.instance_config: InstanceConfig = instance_config or InstanceConfig()

        # 触发关键词：优先实例配置，默认 "记账"
        self.keyword: str = "记账"
        self.enabled: bool = self.instance_config.jizhang_enabled
        self.domain: str = self.instance_config.jizhang_domain

        self.parser: BookkeepingParser = BookkeepingParser(self.keyword)
        # 群名缓存：group_wxid -> group_name
        self._group_names: dict[str, str] = {}
        # 已处理 msg_id 去重
        self._processed: set[str] = set()
        self._max_processed_cache: int = 5000

    # ------------------------------------------------------------------ #
    #  消息处理入口
    # ------------------------------------------------------------------ #
    async def handle_message(self, message: MessageData) -> Optional[BookkeepingRecord]:
        """处理一条消息：判断是否记账指令，是则记录。

        Args:
            message: 接收到的消息。

        Returns:
            成功记录的 BookkeepingRecord，否则 None。
        """
        if not self.enabled:
            return None
        # 仅处理文本消息
        if message.msg_type != MessageType.TEXT:
            return None
        # 仅处理群消息（记账发生在群内）
        if not message.is_group or not message.group_wxid:
            return None
        # 去重
        if message.msg_id in self._processed:
            return None

        body = message.content_body
        parsed = self.parser.parse(body)
        if parsed is None:
            return None

        # 记录并回复确认
        record = await self.add_record(
            group_wxid=message.group_wxid,
            group_name=self._group_names.get(message.group_wxid, ""),
            sender_wxid=message.sender_wxid,
            sender_name=message.actual_sender_in_group or message.sender_wxid,
            amount=parsed["amount"],
            bank_name=parsed["bank_name"],
            remark=parsed["remark"],
            raw_msg=body,
            msg_id=message.msg_id,
        )
        if record is not None:
            self._mark_processed(message.msg_id)
            # 回复确认
            confirm = (
                f"记账成功 ✓\n"
                f"金额: {parsed['amount']}\n"
                f"渠道: {parsed['bank_name']}\n"
                f"备注: {parsed['remark'] or '无'}"
            )
            await self.client.send_text(message.group_wxid, confirm)
            # 异步同步到后端（不阻塞）
            asyncio.create_task(self.sync_to_backend(record))
        return record

    def _mark_processed(self, msg_id: str) -> None:
        """记录已处理消息ID，超限时清理。"""
        self._processed.add(msg_id)
        if len(self._processed) > self._max_processed_cache:
            # 保留最近一半
            self._processed = set(list(self._processed)[-self._max_processed_cache // 2 :])

    # ------------------------------------------------------------------ #
    #  增删查
    # ------------------------------------------------------------------ #
    async def add_record(
        self,
        group_wxid: str,
        group_name: str,
        sender_wxid: str,
        sender_name: str,
        amount: float,
        bank_name: str,
        remark: str,
        raw_msg: str = "",
        msg_id: str = "",
    ) -> Optional[BookkeepingRecord]:
        """新增一条记账记录。"""
        try:
            record = BookkeepingRecord(
                instance_id=self.instance_config.instance_id,
                group_wxid=group_wxid,
                group_name=group_name,
                sender_wxid=sender_wxid,
                sender_name=sender_name,
                amount=amount,
                bank_name=bank_name,
                remark=remark,
                raw_msg=raw_msg,
                msg_id=msg_id,
                sync_status=0,
                created_at=datetime.utcnow(),
            )
            async with self.db.session() as session:
                session.add(record)
                await session.commit()
                await session.refresh(record)
            logger.info(
                f"记账记录已保存: {sender_name} {amount} {bank_name} {remark}"
            )
            return record
        except Exception as e:  # noqa: BLE001
            logger.exception(f"保存记账记录失败: {e}")
            return None

    async def get_records(
        self,
        group_wxid: Optional[str] = None,
        sender_wxid: Optional[str] = None,
        bank_name: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询记账记录，支持多维度筛选。"""
        async with self.db.session() as session:
            stmt = select(BookkeepingRecord)
            conditions = []
            if group_wxid:
                conditions.append(BookkeepingRecord.group_wxid == group_wxid)
            if sender_wxid:
                conditions.append(BookkeepingRecord.sender_wxid == sender_wxid)
            if bank_name:
                conditions.append(BookkeepingRecord.bank_name == bank_name)
            if start:
                conditions.append(BookkeepingRecord.created_at >= start)
            if end:
                conditions.append(BookkeepingRecord.created_at <= end)
            if conditions:
                stmt = stmt.where(and_(*conditions))
            stmt = stmt.order_by(BookkeepingRecord.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            return [self._record_to_dict(r) for r in result.scalars().all()]

    # ------------------------------------------------------------------ #
    #  统计
    # ------------------------------------------------------------------ #
    async def get_statistics(
        self,
        dimension: str = "group",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """按维度统计。

        Args:
            dimension: 统计维度，"group" | "bank" | "user"。
            start/end: 时间范围。

        Returns:
            [{key, count, total_amount, income, expense}, ...]
        """
        dim_map = {
            "group": BookkeepingRecord.group_wxid,
            "bank": BookkeepingRecord.bank_name,
            "user": BookkeepingRecord.sender_wxid,
        }
        dim_col = dim_map.get(dimension, BookkeepingRecord.group_wxid)

        async with self.db.session() as session:
            stmt = (
                select(
                    dim_col.label("key"),
                    func.count(BookkeepingRecord.id).label("count"),
                    func.sum(BookkeepingRecord.amount).label("total_amount"),
                    func.sum(
                        case(
                            (BookkeepingRecord.amount > 0, BookkeepingRecord.amount),
                            else_=0.0,
                        )
                    ).label("income"),
                    func.sum(
                        case(
                            (BookkeepingRecord.amount < 0, BookkeepingRecord.amount),
                            else_=0.0,
                        )
                    ).label("expense"),
                )
                .group_by(dim_col)
            )
            conditions = []
            if start:
                conditions.append(BookkeepingRecord.created_at >= start)
            if end:
                conditions.append(BookkeepingRecord.created_at <= end)
            if conditions:
                stmt = stmt.where(and_(*conditions))
            result = await session.execute(stmt)
            rows = result.all()
            return [
                {
                    "key": r.key or "",
                    "count": int(r.count or 0),
                    "total_amount": float(r.total_amount or 0.0),
                    "income": float(r.income or 0.0),
                    "expense": float(r.expense or 0.0),
                }
                for r in rows
            ]

    # ------------------------------------------------------------------ #
    #  报表
    # ------------------------------------------------------------------ #
    async def generate_report(
        self, period: str = "daily", group_wxid: Optional[str] = None
    ) -> str:
        """生成报表文本。

        Args:
            period: "daily" | "weekly" | "monthly"。
            group_wxid: 限定群，None=全部。

        Returns:
            报表文本（可直接发送）。
        """
        now = datetime.utcnow()
        if period == "daily":
            start = now - timedelta(days=1)
            title = "日报"
        elif period == "weekly":
            start = now - timedelta(days=7)
            title = "周报"
        elif period == "monthly":
            start = now - timedelta(days=30)
            title = "月报"
        else:
            start = now - timedelta(days=1)
            title = "日报"

        records = await self.get_records(
            group_wxid=group_wxid, start=start, end=now, limit=10000
        )
        if not records:
            return f"【记账{title}】\n区间内无记账记录。"

        total = sum(r["amount"] for r in records)
        income = sum(r["amount"] for r in records if r["amount"] > 0)
        expense = sum(r["amount"] for r in records if r["amount"] < 0)
        by_bank: dict[str, float] = {}
        by_user: dict[str, float] = {}
        for r in records:
            by_bank[r["bank_name"]] = by_bank.get(r["bank_name"], 0.0) + r["amount"]
            by_user[r["sender_name"]] = by_user.get(r["sender_name"], 0.0) + r["amount"]

        lines = [
            f"【记账{title}】",
            f"统计区间: {start.strftime('%m-%d %H:%M')} ~ {now.strftime('%m-%d %H:%M')}",
            f"记录笔数: {len(records)}",
            f"总收入: {income:.2f}",
            f"总支出: {abs(expense):.2f}",
            f"净额: {total:.2f}",
            "",
            "按渠道:",
        ]
        for bank, amt in sorted(by_bank.items(), key=lambda x: -abs(x[1])):
            lines.append(f"  {bank}: {amt:.2f}")
        lines.append("")
        lines.append("按人员:")
        for user, amt in sorted(by_user.items(), key=lambda x: -abs(x[1])):
            lines.append(f"  {user}: {amt:.2f}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  后端同步
    # ------------------------------------------------------------------ #
    async def sync_to_backend(self, record: BookkeepingRecord) -> bool:
        """将单条记录同步到后端 API（jizhang_domain）。

        Args:
            record: 记账记录。

        Returns:
            是否同步成功。
        """
        if not self.domain:
            return False
        if httpx is None:
            logger.warning("httpx 未安装，跳过后端同步")
            return False
        url = self.domain.rstrip("/") + "/api/jizhang/record"
        payload = self._record_to_dict(record)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
            ok = resp.status_code == 200
            # 更新同步状态
            await self._update_sync_status(record.id, 1 if ok else 2)
            if ok:
                logger.debug(f"记录 #{record.id} 已同步后端")
            else:
                logger.warning(f"记录 #{record.id} 同步后端失败: {resp.status_code}")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.warning(f"后端同步异常 记录#{record.id}: {e}")
            await self._update_sync_status(record.id, 2)
            return False

    async def sync_unsynced(self) -> int:
        """批量同步所有未同步成功的记录。

        Returns:
            成功同步条数。
        """
        async with self.db.session() as session:
            stmt = select(BookkeepingRecord).where(
                BookkeepingRecord.sync_status != 1
            ).limit(500)
            result = await session.execute(stmt)
            records = result.scalars().all()

        success = 0
        for r in records:
            if await self.sync_to_backend(r):
                success += 1
        logger.info(f"批量同步完成: {success}/{len(records)}")
        return success

    async def _update_sync_status(self, record_id: int, status: int) -> None:
        """更新记录同步状态。"""
        async with self.db.session() as session:
            record = await session.get(BookkeepingRecord, record_id)
            if record is not None:
                record.sync_status = status
                await session.commit()

    # ------------------------------------------------------------------ #
    #  群名维护
    # ------------------------------------------------------------------ #
    def set_group_name(self, group_wxid: str, group_name: str) -> None:
        """更新群名缓存。"""
        if group_wxid:
            self._group_names[group_wxid] = group_name

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _record_to_dict(record: BookkeepingRecord) -> dict[str, Any]:
        """ORM 对象转字典。"""
        return {
            "id": record.id,
            "instance_id": record.instance_id,
            "group_wxid": record.group_wxid,
            "group_name": record.group_name,
            "sender_wxid": record.sender_wxid,
            "sender_name": record.sender_name,
            "amount": record.amount,
            "bank_name": record.bank_name,
            "remark": record.remark,
            "raw_msg": record.raw_msg,
            "msg_id": record.msg_id,
            "sync_status": record.sync_status,
            "created_at": record.created_at.isoformat() if record.created_at else "",
        }


# ====================================================================== #
#  独立运行测试（模拟模式）
# ====================================================================== #
async def _self_test() -> None:
    """模拟模式自测：解析指令、记录、统计、报表。"""
    from wechat.wechat_client import WeChatClient

    client = WeChatClient(instance_id="test", mock=True)
    await client.init("test")
    await client.load_window()

    db = Database(":memory:")
    await db.init()

    inst = InstanceConfig(instance_id="test", jizhang_enabled=True, jizhang_domain="")
    module = BookkeepingModule(client, db, inst)
    module.set_group_name("12345678902@chatroom", "记账交流群")

    # 构造记账消息
    test_msgs = [
        ("记账 100 工商银行 午餐", "wxid_test001", "张三"),
        ("记账 -50 微信 退款", "wxid_test002", "李四"),
        ("记账 200 支付宝 工资", "wxid_test001", "张三"),
        ("记账 80 工商银行 打车", "wxid_test003", "王五"),
        ("今天天气不错", "wxid_test001", "张三"),  # 非记账指令
    ]
    for i, (text, sender, name) in enumerate(test_msgs):
        msg = MessageData(
            msg_id=f"bk_{i}",
            sender_wxid=sender,
            receiver_wxid="wxid_self_000",
            content=f"{sender}:\n{text}",
            is_group=True,
            group_wxid="12345678902@chatroom",
        )
        rec = await module.handle_message(msg)
        logger.info(f"消息 '{text}' -> {'已记录' if rec else '忽略'}")

    # 统计
    stats = await module.get_statistics(dimension="bank")
    logger.info(f"按渠道统计: {stats}")
    stats2 = await module.get_statistics(dimension="user")
    logger.info(f"按人员统计: {stats2}")

    # 报表
    report = await module.generate_report(period="daily")
    logger.info(f"日报:\n{report}")

    await db.close()
    await client.uninstall()
    logger.info("记账模块自测完成")


if __name__ == "__main__":
    asyncio.run(_self_test())
