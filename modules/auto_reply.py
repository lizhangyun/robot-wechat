"""
自动回复模块

功能：
- 规则引擎：关键词匹配、正则匹配、全匹配；
- 支持文本/图片/文件回复；
- 支持时间段控制（如仅工作时间回复）；
- 随机延迟回复（防检测）；
- 规则 CRUD 管理（增删改查，持久化到数据库）。

规则匹配优先级：优先级数值越大越先匹配；命中首条即回复。
"""
from __future__ import annotations

import asyncio
import random
import re
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

# 独立运行支持：将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger
from sqlalchemy import String, Text, Boolean, Integer, Float, DateTime, select, update
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, Database
from wechat.hook_interface import WeChatHookInterface
from wechat.message_types import MessageData, MessageType, SendResult


# ====================================================================== #
#  常量
# ====================================================================== #
class MatchType:
    """匹配类型。"""

    KEYWORD = "keyword"  # 关键词包含匹配
    REGEX = "regex"      # 正则匹配
    EXACT = "exact"      # 全匹配（完全一致）


class ReplyType:
    """回复类型。"""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"


# ====================================================================== #
#  ORM 模型
# ====================================================================== #
class AutoReplyRule(Base):
    """自动回复规则 ORM 模型。"""

    __tablename__ = "auto_reply_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), default="", comment="规则名称")
    match_type: Mapped[str] = mapped_column(String(16), default=MatchType.KEYWORD, comment="匹配类型")
    pattern: Mapped[str] = mapped_column(Text, default="", comment="匹配模式(关键词/正则/全匹配文本)")
    reply_type: Mapped[str] = mapped_column(String(16), default=ReplyType.TEXT, comment="回复类型")
    reply_content: Mapped[str] = mapped_column(Text, default="", comment="回复文本内容")
    reply_path: Mapped[str] = mapped_column(Text, default="", comment="回复图片/文件路径")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    # 时间段控制，空表示不限；格式 "HH:MM"
    time_start: Mapped[str] = mapped_column(String(8), default="", comment="生效起始时间 HH:MM")
    time_end: Mapped[str] = mapped_column(String(8), default="", comment="生效结束时间 HH:MM")
    # 随机延迟（秒），防检测
    min_delay: Mapped[float] = mapped_column(Float, default=1.0, comment="最小延迟(秒)")
    max_delay: Mapped[float] = mapped_column(Float, default=3.0, comment="最大延迟(秒)")
    priority: Mapped[int] = mapped_column(Integer, default=0, comment="优先级(越大越先)")
    # 适用范围：空=全部，否则逗号分隔的 wxid/group_wxid
    scope: Mapped[str] = mapped_column(Text, default="", comment="适用范围(逗号分隔wxid)")
    hit_count: Mapped[int] = mapped_column(Integer, default=0, comment="命中次数")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ====================================================================== #
#  自动回复模块
# ====================================================================== #
class AutoReplyModule:
    """自动回复模块。

    Args:
        client: 微信客户端接口。
        db: 异步数据库管理器。
    """

    def __init__(self, client: WeChatHookInterface, db: Database) -> None:
        self.client: WeChatHookInterface = client
        self.db: Database = db
        # 规则内存缓存（按优先级降序）
        self._rules: list[AutoReplyRule] = []
        self._cache_ready: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        # 正则编译缓存
        self._regex_cache: dict[str, re.Pattern[str]] = {}

    # ------------------------------------------------------------------ #
    #  消息处理入口
    # ------------------------------------------------------------------ #
    async def handle_message(self, message: MessageData) -> Optional[SendResult]:
        """处理一条消息：匹配规则，命中则延迟回复。

        Args:
            message: 接收到的消息。

        Returns:
            发送结果，未命中返回 None。
        """
        if not self._cache_ready:
            await self.reload_rules()
        if not self._rules:
            return None

        # 仅处理文本消息
        if message.msg_type != MessageType.TEXT:
            return None

        text = message.content_body
        # 回复目标：群消息回复到群，私聊回复到发送者
        target = message.group_wxid if message.is_group else message.sender_wxid
        if not target:
            return None

        # 找到第一个命中的规则
        matched: Optional[AutoReplyRule] = None
        for rule in self._rules:
            if not rule.enabled:
                continue
            if not self._in_scope(rule, message):
                continue
            if not self._in_time_window(rule):
                continue
            if self._match(rule, text):
                matched = rule
                break

        if matched is None:
            return None

        # 随机延迟回复
        delay = random.uniform(matched.min_delay, matched.max_delay)
        logger.debug(
            f"命中规则 '{matched.name}'，延迟 {delay:.2f}s 后回复 -> {target}"
        )
        await asyncio.sleep(delay)

        # 执行回复
        result = await self._send_reply(target, matched)
        # 更新命中次数
        await self._incr_hit_count(matched.id)
        return result

    # ------------------------------------------------------------------ #
    #  匹配逻辑
    # ------------------------------------------------------------------ #
    def _match(self, rule: AutoReplyRule, text: str) -> bool:
        """根据匹配类型判断是否命中。"""
        if not text:
            return False
        if rule.match_type == MatchType.EXACT:
            return text.strip() == rule.pattern.strip()
        if rule.match_type == MatchType.KEYWORD:
            # 关键词可多个（逗号分隔），任一包含即命中
            keywords = [k.strip() for k in rule.pattern.split(",") if k.strip()]
            return any(k in text for k in keywords)
        if rule.match_type == MatchType.REGEX:
            pattern = self._get_regex(rule.pattern)
            if pattern is None:
                return False
            return pattern.search(text) is not None
        return False

    def _get_regex(self, pattern: str) -> Optional[re.Pattern[str]]:
        """获取（缓存）编译后的正则。"""
        if pattern in self._regex_cache:
            return self._regex_cache[pattern]
        try:
            compiled = re.compile(pattern)
            self._regex_cache[pattern] = compiled
            return compiled
        except re.error as e:
            logger.warning(f"正则编译失败 '{pattern}': {e}")
            return None

    def _in_scope(self, rule: AutoReplyRule, message: MessageData) -> bool:
        """判断消息是否在规则适用范围内。"""
        if not rule.scope:
            return True
        scope_ids = [s.strip() for s in rule.scope.split(",") if s.strip()]
        targets = {message.sender_wxid}
        if message.group_wxid:
            targets.add(message.group_wxid)
        return any(s in targets for s in scope_ids)

    @staticmethod
    def _in_time_window(rule: AutoReplyRule) -> bool:
        """判断当前时间是否在规则生效时间段内。"""
        if not rule.time_start or not rule.time_end:
            return True
        try:
            now = datetime.now().time()
            start = dtime.fromisoformat(rule.time_start)
            end = dtime.fromisoformat(rule.time_end)
        except (ValueError, TypeError):
            return True
        # 处理跨天情况（如 22:00 ~ 06:00）
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    # ------------------------------------------------------------------ #
    #  发送回复
    # ------------------------------------------------------------------ #
    async def _send_reply(self, target: str, rule: AutoReplyRule) -> SendResult:
        """根据回复类型发送回复。"""
        try:
            if rule.reply_type == ReplyType.TEXT:
                return await self.client.send_text(target, rule.reply_content)
            if rule.reply_type == ReplyType.IMAGE:
                return await self.client.send_image(target, rule.reply_path)
            if rule.reply_type == ReplyType.FILE:
                return await self.client.send_file(target, rule.reply_path)
            return SendResult.fail(f"未知回复类型: {rule.reply_type}")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"发送回复失败: {e}")
            return SendResult.fail(str(e))

    # ------------------------------------------------------------------ #
    #  规则 CRUD
    # ------------------------------------------------------------------ #
    async def add_rule(self, rule_data: dict[str, Any]) -> Optional[AutoReplyRule]:
        """新增规则。

        Args:
            rule_data: 规则字段字典。

        Returns:
            新建的规则对象，失败返回 None。
        """
        try:
            rule = AutoReplyRule(
                name=rule_data.get("name", ""),
                match_type=rule_data.get("match_type", MatchType.KEYWORD),
                pattern=rule_data.get("pattern", ""),
                reply_type=rule_data.get("reply_type", ReplyType.TEXT),
                reply_content=rule_data.get("reply_content", ""),
                reply_path=rule_data.get("reply_path", ""),
                enabled=rule_data.get("enabled", True),
                time_start=rule_data.get("time_start", ""),
                time_end=rule_data.get("time_end", ""),
                min_delay=float(rule_data.get("min_delay", 1.0)),
                max_delay=float(rule_data.get("max_delay", 3.0)),
                priority=int(rule_data.get("priority", 0)),
                scope=rule_data.get("scope", ""),
            )
            async with self.db.session() as session:
                session.add(rule)
                await session.commit()
                await session.refresh(rule)
            await self.reload_rules()
            logger.info(f"新增规则: {rule.name}(id={rule.id})")
            return rule
        except Exception as e:  # noqa: BLE001
            logger.exception(f"新增规则失败: {e}")
            return None

    async def update_rule(self, rule_id: int, rule_data: dict[str, Any]) -> bool:
        """更新规则。"""
        try:
            async with self.db.session() as session:
                rule = await session.get(AutoReplyRule, rule_id)
                if rule is None:
                    logger.warning(f"规则不存在: id={rule_id}")
                    return False
                for k, v in rule_data.items():
                    if hasattr(rule, k) and v is not None:
                        setattr(rule, k, v)
                rule.updated_at = datetime.utcnow()
                await session.commit()
            await self.reload_rules()
            logger.info(f"更新规则 id={rule_id}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"更新规则失败: {e}")
            return False

    async def delete_rule(self, rule_id: int) -> bool:
        """删除规则。"""
        try:
            async with self.db.session() as session:
                rule = await session.get(AutoReplyRule, rule_id)
                if rule is None:
                    return False
                await session.delete(rule)
                await session.commit()
            await self.reload_rules()
            logger.info(f"删除规则 id={rule_id}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"删除规则失败: {e}")
            return False

    async def list_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """列出全部规则。"""
        async with self.db.session() as session:
            stmt = select(AutoReplyRule).order_by(AutoReplyRule.priority.desc())
            if enabled_only:
                stmt = stmt.where(AutoReplyRule.enabled == True)  # noqa: E712
            result = await session.execute(stmt)
            return [self._rule_to_dict(r) for r in result.scalars().all()]

    async def toggle_rule(self, rule_id: int, enabled: bool) -> bool:
        """启用/禁用规则。"""
        return await self.update_rule(rule_id, {"enabled": enabled})

    # ------------------------------------------------------------------ #
    #  缓存管理
    # ------------------------------------------------------------------ #
    async def reload_rules(self) -> int:
        """从数据库重新加载规则到内存缓存。"""
        async with self.db.session() as session:
            stmt = select(AutoReplyRule).order_by(AutoReplyRule.priority.desc())
            result = await session.execute(stmt)
            self._rules = list(result.scalars().all())
        self._cache_ready = True
        # 清空正则缓存
        self._regex_cache.clear()
        logger.info(f"自动回复规则已加载: {len(self._rules)} 条")
        return len(self._rules)

    async def _incr_hit_count(self, rule_id: int) -> None:
        """增加命中次数。"""
        try:
            async with self.db.session() as session:
                rule = await session.get(AutoReplyRule, rule_id)
                if rule is not None:
                    rule.hit_count += 1
                    await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"更新命中次数失败: {e}")

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _rule_to_dict(rule: AutoReplyRule) -> dict[str, Any]:
        """ORM 对象转字典。"""
        return {
            "id": rule.id,
            "name": rule.name,
            "match_type": rule.match_type,
            "pattern": rule.pattern,
            "reply_type": rule.reply_type,
            "reply_content": rule.reply_content,
            "reply_path": rule.reply_path,
            "enabled": rule.enabled,
            "time_start": rule.time_start,
            "time_end": rule.time_end,
            "min_delay": rule.min_delay,
            "max_delay": rule.max_delay,
            "priority": rule.priority,
            "scope": rule.scope,
            "hit_count": rule.hit_count,
            "created_at": rule.created_at.isoformat() if rule.created_at else "",
            "updated_at": rule.updated_at.isoformat() if rule.updated_at else "",
        }


# ====================================================================== #
#  独立运行测试（模拟模式）
# ====================================================================== #
async def _self_test() -> None:
    """模拟模式自测：CRUD、规则匹配、延迟回复。"""
    from wechat.wechat_client import WeChatClient

    client = WeChatClient(instance_id="test", mock=True)
    await client.init("test")
    await client.load_window()

    db = Database(":memory:")
    await db.init()

    module = AutoReplyModule(client, db)

    # 新增规则
    await module.add_rule({
        "name": "问候",
        "match_type": MatchType.KEYWORD,
        "pattern": "你好,在吗",
        "reply_type": ReplyType.TEXT,
        "reply_content": "你好，有什么可以帮您？",
        "min_delay": 0.1,
        "max_delay": 0.3,
        "priority": 10,
    })
    await module.add_rule({
        "name": "价格正则",
        "match_type": MatchType.REGEX,
        "pattern": r"价格|多少钱|费用",
        "reply_type": ReplyType.TEXT,
        "reply_content": "请咨询客服获取最新报价。",
        "min_delay": 0.1,
        "max_delay": 0.2,
        "priority": 5,
    })
    await module.add_rule({
        "name": "全匹配-帮助",
        "match_type": MatchType.EXACT,
        "pattern": "帮助",
        "reply_type": ReplyType.TEXT,
        "reply_content": "输入关键词即可获取帮助。",
        "min_delay": 0.1,
        "max_delay": 0.2,
        "priority": 8,
    })

    rules = await module.list_rules()
    logger.info(f"规则数量: {len(rules)}")

    # 测试匹配
    test_cases = ["你好", "这个多少钱", "帮助", "今天天气不错"]
    for text in test_cases:
        msg = MessageData(
            msg_id=f"ar_{text}",
            sender_wxid="wxid_test001",
            receiver_wxid="wxid_self_000",
            content=text,
            is_group=False,
        )
        res = await module.handle_message(msg)
        logger.info(f"消息 '{text}' -> {'已回复' if res and res.success else '未回复'}")

    # 更新与删除
    await module.toggle_rule(rules[0]["id"], False)
    await module.delete_rule(rules[1]["id"])
    logger.info(f"操作后规则数: {len(await module.list_rules())}")

    await db.close()
    await client.uninstall()
    logger.info("自动回复模块自测完成")


if __name__ == "__main__":
    asyncio.run(_self_test())
