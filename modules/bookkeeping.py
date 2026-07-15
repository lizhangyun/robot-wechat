"""
记账模块（对应原软件 jizhang 模块）

功能：
- 解析群消息中的记账指令（如 "记账 100 银行名称 备注"）；
- 将记录持久化到 BookkeepingRecord 表；
- 按群组、银行、用户维度统计；
- 生成日报/周报/月报；
- 支持后端 API 同步（jizhang_domain）；
- 关键词触发机制（可配置触发关键词）；
- 银行/渠道白名单校验（不在白名单则拒绝记账）；
- 消息分片发送（超过 max_lines 自动分片）；
- AckMessage 确认机制（确保确认消息送达）；
- 多配置实例支持（通过 JizhangConfigManager）。

指令格式：
    记账 <金额> <银行名称> [备注]
    其中金额可为负数（表示退款/支出扣减），如 "记账 -50 微信 退款"。
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
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
from modules.jizhang_config import JizhangConfig, JizhangConfigManager
from modules.message_splitter import MessageSplitter
from network.ack_manager import AckManager
from security.keyword_decoder import KeywordDecoder
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
#  银行/渠道白名单校验器
# ====================================================================== #
class BankWhitelistValidator:
    """银行/渠道名称白名单校验器。

    对应原软件 keyword 解密后包含的银行名称白名单：
      - 仅允许白名单中的渠道名称进行记账；
      - 常见渠道：工商银行、建设银行、微信、支付宝；
      - 白名单为空时表示不限制（允许所有渠道）。

    Args:
        whitelist: 银行/渠道名称列表。为空时允许所有渠道。
    """

    def __init__(self, whitelist: Optional[list[str]] = None) -> None:
        self.whitelist: set[str] = set(whitelist or [])

    def is_allowed(self, bank_name: str) -> bool:
        """校验银行名称是否在白名单中。

        Args:
            bank_name: 银行/渠道名称。

        Returns:
            白名单为空时返回 True（不限制）；否则返回是否在白名单中。
        """
        if not self.whitelist:
            return True
        return bank_name.strip() in self.whitelist

    def add(self, bank_name: str) -> None:
        """添加渠道到白名单。"""
        self.whitelist.add(bank_name.strip())

    def remove(self, bank_name: str) -> None:
        """从白名单移除渠道。"""
        self.whitelist.discard(bank_name.strip())

    def list_banks(self) -> list[str]:
        """返回白名单中的所有渠道（排序后）。"""
        return sorted(self.whitelist)

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "whitelist": sorted(self.whitelist),
            "enabled": bool(self.whitelist),
        }


# ====================================================================== #
#  记账配置数据类
# ====================================================================== #
@dataclass
class BookkeepingConfig:
    """记账配置（整合触发词、白名单、domain 等）。

    由 :class:`BookkeepingModule` 在初始化时从多个来源聚合：
      - 实例配置 (InstanceConfig)；
      - keyword 解密结果 (KeywordDecoder)；
      - 多配置管理器 (JizhangConfigManager)。

    Attributes:
        config_id: 配置标识。
        trigger_words: 触发关键词列表。
        bank_whitelist: 银行/渠道白名单。
        domain: 后端 API 地址。
        enabled: 是否启用。
        db_key: 数据库加密密钥。
        features: 功能开关字典。
    """

    config_id: str = "default"
    trigger_words: list[str] = field(default_factory=lambda: ["记账"])
    bank_whitelist: list[str] = field(default_factory=list)
    domain: str = ""
    enabled: bool = True
    db_key: str = ""
    features: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_jizhang_config(cls, jc: JizhangConfig) -> "BookkeepingConfig":
        """从 :class:`JizhangConfig` 构造。"""
        return cls(
            config_id=jc.config_id or "default",
            trigger_words=list(jc.trigger_words) or ["记账"],
            bank_whitelist=list(jc.bank_whitelist),
            domain=jc.domain,
            enabled=jc.enabled,
            db_key=jc.db_key,
            features=dict(jc.features),
        )


# ====================================================================== #
#  指令解析
# ====================================================================== #
class BookkeepingParser:
    """记账指令解析器。

    支持单个或多个触发关键词。当传入列表时，按最长匹配优先。

    Args:
        keyword: 触发关键词（字符串）或关键词列表。
    """

    # 默认指令格式：关键词 金额 银行名称 备注
    # 金额可为负；银行名称不含空格；备注为剩余部分（可含空格）
    _PATTERN = re.compile(
        r"^\s*(?P<keyword>[^\s]+)\s+"
        r"(?P<amount>-?\d+(?:\.\d+)?)\s+"
        r"(?P<bank>\S+)\s*"
        r"(?P<remark>.*)$"
    )

    def __init__(self, keyword: str | list[str] = "记账") -> None:
        if isinstance(keyword, str):
            self.keywords: list[str] = [keyword] if keyword else ["记账"]
        elif isinstance(keyword, (list, tuple)):
            self.keywords = list(keyword) if keyword else ["记账"]
        else:
            self.keywords = ["记账"]
        # 按长度降序排列，优先匹配最长关键词（避免 "记账" 与 "记账入" 冲突）
        self._sorted_keywords: list[str] = sorted(self.keywords, key=len, reverse=True)
        # 保持 keyword 属性向后兼容
        self.keyword: str = keyword if isinstance(keyword, str) else (
            self.keywords[0] if self.keywords else "记账"
        )

    def parse(self, content: str) -> Optional[dict[str, Any]]:
        """解析消息正文。

        Args:
            content: 消息正文（已去除群前缀）。

        Returns:
            解析结果字典 {amount, bank_name, remark}，非记账指令返回 None。
        """
        text = content.strip()
        # 必须以某个触发关键词开头（最长匹配优先）
        matched: Optional[str] = None
        for kw in self._sorted_keywords:
            if text.startswith(kw):
                rest = text[len(kw):]
                # 关键词后必须是空格或结尾
                if not rest or rest[0].isspace():
                    matched = kw
                    break
        if matched is None:
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

    对应原软件 jizhang 模块，支持：
      - 多配置实例（通过 :class:`JizhangConfigManager`）；
      - keyword AES 解密（通过 :class:`KeywordDecoder`）；
      - 银行名称白名单校验（:class:`BankWhitelistValidator`）；
      - 消息分片发送报表（:class:`MessageSplitter`）；
      - AckMessage 确认消息送达（:class:`AckManager`）；
      - 后端同步（完整的 HTTP 请求、错误处理、重试）；
      - GBK 配置文件读取。

    Args:
        client: 微信客户端接口。
        db: 异步数据库管理器。
        instance_config: 实例配置（含 jizhang_domain / jizhang_keyword 等）。
        config_manager: 记账多配置管理器（可选，用于加载多套 jizhang 配置）。
        keyword_decoder: keyword AES 解密器（可选，用于解密 jizhang_keyword）。
        message_splitter: 消息分片器（可选，默认从 instance_config 自动创建）。
        ack_manager: ACK 确认管理器（可选，默认从 instance_config 自动创建）。
    """

    def __init__(
        self,
        client: WeChatHookInterface,
        db: Database,
        instance_config: Optional[InstanceConfig] = None,
        *,
        config_manager: Optional[JizhangConfigManager] = None,
        keyword_decoder: Optional[KeywordDecoder] = None,
        message_splitter: Optional[MessageSplitter] = None,
        ack_manager: Optional[AckManager] = None,
    ) -> None:
        self.client: WeChatHookInterface = client
        self.db: Database = db
        self.instance_config: InstanceConfig = instance_config or InstanceConfig()

        # 触发关键词与开关
        self.enabled: bool = self.instance_config.jizhang_enabled
        self.domain: str = self.instance_config.jizhang_domain

        # 消息分片器（默认从实例配置自动创建）
        self.message_splitter: MessageSplitter = message_splitter or MessageSplitter(
            max_lines=self.instance_config.msg_max_lines,
            sleep_sec=self.instance_config.msg_sleep_sec,
        )

        # ACK 确认管理器（默认从实例配置自动创建，auto_ack=True 适配模拟/标准客户端）
        self.ack_manager: AckManager = ack_manager or AckManager(
            timeout=self.instance_config.ack_timeout,
            max_retries=self.instance_config.ack_max_retries,
            auto_ack=True,
        )

        # keyword 解密器与多配置管理器
        self.keyword_decoder: Optional[KeywordDecoder] = keyword_decoder
        self.config_manager: Optional[JizhangConfigManager] = config_manager

        # 加载记账配置（多配置聚合）
        self._configs: dict[str, BookkeepingConfig] = {}
        self._load_configs()

        # 聚合所有配置的触发词与白名单
        all_triggers: set[str] = set()
        all_banks: set[str] = set()
        for cfg in self._configs.values():
            all_triggers.update(cfg.trigger_words)
            all_banks.update(cfg.bank_whitelist)

        trigger_list = list(all_triggers) if all_triggers else ["记账"]
        self.parser: BookkeepingParser = BookkeepingParser(trigger_list)
        self.bank_validator: BankWhitelistValidator = BankWhitelistValidator(
            list(all_banks)
        )

        # 若白名单非空，记录日志
        if all_banks:
            logger.info(f"记账银行白名单已启用: {sorted(all_banks)}")

        # 群名缓存：group_wxid -> group_name
        self._group_names: dict[str, str] = {}
        # 已处理 msg_id 去重
        self._processed: set[str] = set()
        self._max_processed_cache: int = 5000

    # ------------------------------------------------------------------ #
    #  配置加载
    # ------------------------------------------------------------------ #
    def _load_configs(self) -> None:
        """从多个来源加载记账配置并聚合。"""
        # 1. 从多配置管理器加载（对应 data/app/jizhang_c1/, jizhang_c12/ 等）
        if self.config_manager and self.instance_config.jizhang_configs:
            for cid in self.instance_config.jizhang_configs:
                try:
                    jc = self.config_manager.load_config(cid)
                    self._configs[cid] = BookkeepingConfig.from_jizhang_config(jc)
                    if jc.domain and not self.domain:
                        self.domain = jc.domain
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"加载记账配置 {cid} 失败: {exc}")

        # 2. 从 keyword 解密加载（对应 config.ini 中 [jizhang] 段的 keyword 字段）
        if self.keyword_decoder:
            keyword_hex = (
                self.instance_config.jizhang_keyword
                or self.instance_config.keyword
            )
            if keyword_hex:
                try:
                    decoded = self.keyword_decoder.decrypt(keyword_hex)
                    self._configs["keyword"] = BookkeepingConfig(
                        config_id="keyword",
                        trigger_words=list(decoded.get("trigger_words", [])) or ["记账"],
                        bank_whitelist=list(decoded.get("bank_whitelist", [])),
                        domain=self.instance_config.jizhang_domain,
                        enabled=self.instance_config.jizhang_enabled,
                        db_key=str(decoded.get("db_key", "")),
                        features=dict(decoded.get("features", {})),
                    )
                    logger.info("keyword 解密配置已加载")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"keyword 解密失败: {exc}")

        # 3. 确保至少有一个默认配置
        if not self._configs:
            self._configs["default"] = BookkeepingConfig(
                config_id="default",
                trigger_words=["记账"],
                bank_whitelist=[],
                domain=self.instance_config.jizhang_domain,
                enabled=self.instance_config.jizhang_enabled,
                db_key="",
                features={},
            )

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

        # 银行白名单校验：不在白名单则拒绝记账并提示
        if not self.bank_validator.is_allowed(parsed["bank_name"]):
            self._mark_processed(message.msg_id)
            reject_msg = (
                f"记账失败: 渠道 \"{parsed['bank_name']}\" 不在白名单中\n"
                f"允许的渠道: {', '.join(self.bank_validator.list_banks())}"
            )
            await self._send_confirm(message.group_wxid, reject_msg)
            logger.info(
                f"记账被拒绝(白名单): {parsed['bank_name']} by "
                f"{message.sender_wxid} in {message.group_wxid}"
            )
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
            # 回复确认（通过 AckManager 确保送达）
            confirm = (
                f"记账成功 ✓\n"
                f"金额: {parsed['amount']}\n"
                f"渠道: {parsed['bank_name']}\n"
                f"备注: {parsed['remark'] or '无'}"
            )
            await self._send_confirm(message.group_wxid, confirm)
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
    #  消息发送（AckManager / MessageSplitter 集成）
    # ------------------------------------------------------------------ #
    async def _send_confirm(self, wxid: str, text: str) -> bool:
        """发送确认消息（通过 AckManager 确保送达）。

        Args:
            wxid: 接收者 wxid。
            text: 确认消息文本。

        Returns:
            是否发送成功。
        """
        if self.ack_manager:
            msg_id = AckManager.generate_msg_id("bk_confirm")
            success = await self.ack_manager.send_with_ack(
                self.client.send_text, msg_id, wxid, text
            )
            if not success:
                logger.warning(f"确认消息通过 AckManager 发送失败: wxid={wxid}")
            return success
        # 无 AckManager 时直接发送
        try:
            result = await self.client.send_text(wxid, text)
            return result.success
        except Exception as exc:  # noqa: BLE001
            logger.error(f"确认消息发送异常: {exc}")
            return False

    async def send_report(
        self,
        group_wxid: str,
        period: str = "daily",
    ) -> list[str]:
        """生成并发送报表（使用 MessageSplitter 分片发送）。

        Args:
            group_wxid: 群 wxid。
            period: "daily" | "weekly" | "monthly"。

        Returns:
            发送的消息 ID 列表。
        """
        report = await self.generate_report(period=period, group_wxid=group_wxid)
        if self.message_splitter and self.instance_config.msg_split_enabled:
            return await self.message_splitter.send(
                self.client, group_wxid, report
            )
        # 分片未启用，直接发送
        result = await self.client.send_text(group_wxid, report)
        return [result.msg_id or ""]

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
    async def sync_to_backend(
        self, record: BookkeepingRecord, max_retries: Optional[int] = None
    ) -> bool:
        """将单条记录同步到后端 API（jizhang_domain）。

        对应原软件通过 HTTP POST 同步每条记账记录到后端：
          - c6801 后端: http://jacn1.huoxing111.com/6802cishi/
          - c6802 后端: https://jizhang105.tztz.eu.org/6802cishi/
          - URL 路径 /6802cishi/ 为固定后缀
          - 同步状态: 0=未同步, 1=已同步, 2=同步失败
          - 支持重试（指数退避）

        Args:
            record: 记账记录。
            max_retries: 最大重试次数（默认从实例配置获取）。

        Returns:
            是否同步成功。
        """
        if not self.domain:
            return False
        if httpx is None:
            logger.warning("httpx 未安装，跳过后端同步")
            return False

        retries = (
            max_retries
            if max_retries is not None
            else self.instance_config.ack_max_retries
        )
        url = self.domain.rstrip("/") + "/api/jizhang/record"
        payload = self._record_to_dict(record)
        last_error: Optional[str] = None

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, json=payload)

                if resp.status_code == 200:
                    await self._update_sync_status(record.id, 1)
                    logger.debug(
                        f"记录 #{record.id} 已同步后端"
                        + (f" (第 {attempt + 1} 次尝试)" if attempt > 0 else "")
                    )
                    return True

                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(
                    f"记录 #{record.id} 同步后端失败 (第 {attempt + 1} 次): "
                    f"HTTP {resp.status_code}"
                )
            except httpx.ConnectError as exc:
                last_error = f"连接错误: {exc}"
                logger.warning(f"记录 #{record.id} 同步后端连接失败: {exc}")
            except httpx.TimeoutException as exc:
                last_error = f"超时: {exc}"
                logger.warning(f"记录 #{record.id} 同步后端超时: {exc}")
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning(f"记录 #{record.id} 同步后端异常: {exc}")

            # 指数退避等待（最后一次不等待）
            if attempt < retries:
                backoff = min(2 ** attempt, 5.0)
                logger.debug(f"记录 #{record.id} 等待 {backoff}s 后重试")
                await asyncio.sleep(backoff)

        # 所有重试耗尽
        await self._update_sync_status(record.id, 2)
        logger.error(
            f"记录 #{record.id} 同步后端最终失败 ({retries + 1} 次尝试): {last_error}"
        )
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
    #  配置管理
    # ------------------------------------------------------------------ #
    def get_configs(self) -> dict[str, BookkeepingConfig]:
        """获取当前加载的所有记账配置。"""
        return dict(self._configs)

    def get_trigger_words(self) -> list[str]:
        """获取所有触发关键词。"""
        return self.parser.keywords

    def get_bank_whitelist(self) -> list[str]:
        """获取银行白名单列表。"""
        return self.bank_validator.list_banks()

    def reload_configs(self) -> None:
        """重新加载配置（清空缓存后重新加载）。"""
        if self.config_manager:
            self.config_manager.clear_cache()
        self._configs.clear()
        self._load_configs()

        # 重新聚合触发词与白名单
        all_triggers: set[str] = set()
        all_banks: set[str] = set()
        for cfg in self._configs.values():
            all_triggers.update(cfg.trigger_words)
            all_banks.update(cfg.bank_whitelist)

        trigger_list = list(all_triggers) if all_triggers else ["记账"]
        self.parser = BookkeepingParser(trigger_list)
        self.bank_validator = BankWhitelistValidator(list(all_banks))
        logger.info("记账配置已重新加载")

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
