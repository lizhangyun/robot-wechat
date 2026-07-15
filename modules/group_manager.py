"""
群管理模块

功能：
- 欢迎新成员入群（监听系统入群消息，自动发欢迎语）；
- 关键词撤回（检测广告/违规内容，自动撤回并提示）；
- 定时群公告（按计划向群发送公告）；
- @所有人提醒；
- 群成员统计（人数、活跃度等）。

违规检测采用关键词与正则双重策略，命中后调用 REVOKE_MSG 撤回。
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 独立运行支持：将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger
from sqlalchemy import (
    String, Text, Boolean, Integer, DateTime, select, func, and_,
)
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, Database
from wechat.hook_interface import APICommand, WeChatHookInterface
from wechat.message_types import MessageData, MessageType, SendResult


# ====================================================================== #
#  ORM 模型
# ====================================================================== #
class GroupConfig(Base):
    """群配置 ORM 模型（每个群一条）。"""

    __tablename__ = "group_configs"

    group_wxid: Mapped[str] = mapped_column(String(64), primary_key=True, comment="群wxid")
    group_name: Mapped[str] = mapped_column(String(128), default="", comment="群名")
    welcome_enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否开启欢迎")
    welcome_text: Mapped[str] = mapped_column(Text, default="欢迎新成员入群！", comment="欢迎语")
    anti_ad_enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否开启广告检测")
    anti_ad_keywords: Mapped[str] = mapped_column(
        Text, default="加微,代购,免费领,点击链接,http://,https://",
        comment="违规关键词(逗号分隔)",
    )
    anti_ad_regex: Mapped[str] = mapped_column(Text, default="", comment="违规正则(可选)")
    announcement: Mapped[str] = mapped_column(Text, default="", comment="定时公告内容")
    announcement_cron: Mapped[str] = mapped_column(String(64), default="", comment="公告cron表达式")
    member_count: Mapped[int] = mapped_column(Integer, default=0, comment="成员数快照")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class GroupEvent(Base):
    """群事件记录（入群/撤回等），用于统计与审计。"""

    __tablename__ = "group_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_wxid: Mapped[str] = mapped_column(String(64), default="", index=True, comment="群wxid")
    event_type: Mapped[str] = mapped_column(String(32), default="", comment="事件类型 welcome/revoke/atall/announce")
    target_wxid: Mapped[str] = mapped_column(String(64), default="", comment="相关wxid")
    content: Mapped[str] = mapped_column(Text, default="", comment="事件内容/原因")
    msg_id: Mapped[str] = mapped_column(String(64), default="", comment="相关消息ID")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# ====================================================================== #
#  群管理模块
# ====================================================================== #
class GroupManagerModule:
    """群管理模块。

    Args:
        client: 微信客户端接口。
        db: 异步数据库管理器。
    """

    # 入群系统消息正则（如 "xxx"邀请了"yyy"加入了群聊）
    _JOIN_PATTERN = re.compile(r"邀请.*?加入了群聊|通过扫描.*?二维码.*?进群")

    def __init__(self, client: WeChatHookInterface, db: Database) -> None:
        self.client: WeChatHookInterface = client
        self.db: Database = db
        # 群配置缓存：group_wxid -> GroupConfig
        self._configs: dict[str, GroupConfig] = {}
        self._cache_ready: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        # 定时公告任务
        self._announce_task: Optional[asyncio.Task[None]] = None
        self._running: bool = False
        # 正则缓存
        self._regex_cache: dict[str, re.Pattern[str]] = {}

    # ------------------------------------------------------------------ #
    #  消息处理入口
    # ------------------------------------------------------------------ #
    async def handle_message(self, message: MessageData) -> None:
        """处理一条消息：检测入群、违规内容。"""
        if not self._cache_ready:
            await self.reload_configs()
        if not message.is_group or not message.group_wxid:
            return

        # 1. 系统消息：检测入群
        if message.msg_type == MessageType.SYSTEM:
            await self._handle_system_message(message)
            return

        # 2. 文本消息：检测违规
        if message.msg_type == MessageType.TEXT:
            await self._check_violation(message)

    async def _handle_system_message(self, message: MessageData) -> None:
        """处理系统消息（入群检测）。"""
        content = message.content_body
        if self._JOIN_PATTERN.search(content):
            cfg = self._get_config(message.group_wxid)
            if cfg and cfg.welcome_enabled:
                await self.welcome_member(message.group_wxid, message.sender_wxid, cfg.welcome_text)

    async def _check_violation(self, message: MessageData) -> bool:
        """检测违规内容，命中则撤回。

        Returns:
            是否检测到违规并处理。
        """
        cfg = self._get_config(message.group_wxid)
        if not cfg or not cfg.anti_ad_enabled:
            return False

        text = message.content_body
        # 关键词检测
        keywords = [k.strip() for k in (cfg.anti_ad_keywords or "").split(",") if k.strip()]
        hit_keyword = any(k in text for k in keywords)
        # 正则检测
        hit_regex = False
        if cfg.anti_ad_regex:
            pattern = self._get_regex(cfg.anti_ad_regex)
            if pattern and pattern.search(text):
                hit_regex = True

        if not (hit_keyword or hit_regex):
            return False

        # 撤回消息
        reason = "关键词" if hit_keyword else "正则"
        logger.warning(
            f"检测到违规内容[{reason}] 群={message.group_wxid} 发送者={message.sender_wxid} "
            f"内容={text[:30]}"
        )
        await self.recall_message(message.msg_id, message.group_wxid, text, reason)
        # 提示
        await self.client.send_text(
            message.group_wxid,
            f"@{message.sender_wxid} 您的消息含违规内容已被撤回，请遵守群规。",
        )
        return True

    # ------------------------------------------------------------------ #
    #  群操作
    # ------------------------------------------------------------------ #
    async def welcome_member(
        self, group_wxid: str, member_wxid: str, welcome_text: str = ""
    ) -> SendResult:
        """欢迎新成员入群。

        Args:
            group_wxid: 群 wxid。
            member_wxid: 新成员 wxid。
            welcome_text: 自定义欢迎语（为空则用配置）。

        Returns:
            发送结果。
        """
        cfg = self._get_config(group_wxid)
        text = welcome_text or (cfg.welcome_text if cfg else "欢迎新成员入群！")
        # 替换占位符
        text = text.replace("{wxid}", member_wxid).replace("{nick}", member_wxid)
        logger.info(f"欢迎新成员: 群={group_wxid} 成员={member_wxid}")
        result = await self.client.send_text(group_wxid, text)
        await self._record_event(group_wxid, "welcome", member_wxid, text)
        return result

    async def recall_message(
        self,
        msg_id: str,
        group_wxid: str = "",
        content: str = "",
        reason: str = "",
    ) -> bool:
        """撤回一条消息。

        Args:
            msg_id: 待撤回消息ID。
            group_wxid: 所属群（记录用）。
            content: 原消息内容（记录用）。
            reason: 撤回原因。

        Returns:
            是否撤回成功。
        """
        try:
            result = await self.client.api(APICommand.REVOKE_MSG, {"msg_id": msg_id})
            ok = isinstance(result, dict) and result.get("code") in (0, 200)
            logger.info(f"撤回消息 msg_id={msg_id} {'成功' if ok else '失败'}")
            await self._record_event(
                group_wxid, "revoke", "", f"[{reason}] {content[:50]}", msg_id
            )
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception(f"撤回消息异常: {e}")
            return False

    async def send_announcement(self, group_wxid: str, content: str) -> bool:
        """发布/修改群公告。

        Args:
            group_wxid: 群 wxid。
            content: 公告内容。

        Returns:
            是否成功。
        """
        try:
            result = await self.client.api(
                APICommand.GROUP_ANNOUNCEMENT,
                {"group_wxid": group_wxid, "content": content},
            )
            ok = isinstance(result, dict) and result.get("code") in (0, 200)
            logger.info(f"发布群公告 群={group_wxid} {'成功' if ok else '失败'}")
            await self._record_event(group_wxid, "announce", "", content[:50])
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception(f"发布群公告异常: {e}")
            return False

    async def at_all(self, group_wxid: str, content: str) -> SendResult:
        """@所有人发送提醒。

        Args:
            group_wxid: 群 wxid。
            content: 提醒内容。

        Returns:
            发送结果。
        """
        try:
            # 通过 SEND_AT 命令 @所有人（notify@all 表示全体）
            result = await self.client.api(
                APICommand.SEND_AT,
                {
                    "group_wxid": group_wxid,
                    "content": content,
                    "at_list": ["notify@all"],  # 全体成员
                },
            )
            ok = isinstance(result, dict) and result.get("code") in (0, 200)
            logger.info(f"@所有人 群={group_wxid} {'成功' if ok else '失败'}")
            await self._record_event(group_wxid, "atall", "", content[:50])
            if ok:
                return SendResult.ok(result.get("msg_id"))
            return SendResult.fail(result.get("msg", "@所有人失败"))
        except Exception as e:  # noqa: BLE001
            logger.exception(f"@所有人异常: {e}")
            return SendResult.fail(str(e))

    # ------------------------------------------------------------------ #
    #  群成员统计
    # ------------------------------------------------------------------ #
    async def get_member_stats(self, group_wxid: str) -> dict[str, Any]:
        """获取群成员统计信息。

        Args:
            group_wxid: 群 wxid。

        Returns:
            统计字典，含成员数、事件统计等。
        """
        # 实时成员数
        members = await self.client.get_group_members(group_wxid)
        member_count = len(members)

        # 事件统计
        async with self.db.session() as session:
            total_events = await session.scalar(
                select(func.count()).select_from(GroupEvent).where(
                    GroupEvent.group_wxid == group_wxid
                )
            )
            welcome_count = await session.scalar(
                select(func.count()).select_from(GroupEvent).where(
                    and_(
                        GroupEvent.group_wxid == group_wxid,
                        GroupEvent.event_type == "welcome",
                    )
                )
            )
            revoke_count = await session.scalar(
                select(func.count()).select_from(GroupEvent).where(
                    and_(
                        GroupEvent.group_wxid == group_wxid,
                        GroupEvent.event_type == "revoke",
                    )
                )
            )

        # 更新配置中的成员数快照
        await self._update_member_count(group_wxid, member_count)

        return {
            "group_wxid": group_wxid,
            "member_count": member_count,
            "total_events": int(total_events or 0),
            "welcome_count": int(welcome_count or 0),
            "revoke_count": int(revoke_count or 0),
        }

    # ------------------------------------------------------------------ #
    #  定时群公告
    # ------------------------------------------------------------------ #
    async def start_scheduled_announcements(self, check_interval: int = 60) -> None:
        """启动定时群公告检查任务。

        Args:
            check_interval: 检查间隔(秒)。
        """
        if self._running:
            return
        self._running = True
        self._announce_task = asyncio.create_task(
            self._announcement_loop(check_interval)
        )
        logger.info(f"定时群公告任务已启动 间隔={check_interval}s")

    async def _announcement_loop(self, interval: int) -> None:
        """定时检查并发送公告（简化版：按 cron 分钟匹配）。"""
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                now = datetime.now()
                for cfg in self._configs.values():
                    if not cfg.announcement or not cfg.announcement_cron:
                        continue
                    if self._cron_match(cfg.announcement_cron, now):
                        await self.send_announcement(cfg.group_wxid, cfg.announcement)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception(f"定时公告循环异常: {e}")

    async def stop_scheduled_announcements(self) -> None:
        """停止定时群公告任务。"""
        self._running = False
        if self._announce_task and not self._announce_task.done():
            self._announce_task.cancel()
            try:
                await self._announce_task
            except asyncio.CancelledError:
                pass
        self._announce_task = None

    # ------------------------------------------------------------------ #
    #  配置管理
    # ------------------------------------------------------------------ #
    async def set_group_config(self, group_wxid: str, **fields: Any) -> bool:
        """设置/更新群配置。

        Args:
            group_wxid: 群 wxid。
            **fields: 配置字段。

        Returns:
            是否成功。
        """
        try:
            async with self.db.session() as session:
                cfg = await session.get(GroupConfig, group_wxid)
                if cfg is None:
                    cfg = GroupConfig(group_wxid=group_wxid)
                    session.add(cfg)
                for k, v in fields.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
                cfg.updated_at = datetime.utcnow()
                await session.commit()
            await self.reload_configs()
            logger.info(f"更新群配置: {group_wxid}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"更新群配置失败: {e}")
            return False

    async def get_group_config(self, group_wxid: str) -> Optional[dict[str, Any]]:
        """获取群配置。"""
        cfg = self._get_config(group_wxid)
        return self._config_to_dict(cfg) if cfg else None

    async def reload_configs(self) -> int:
        """从数据库重新加载群配置到内存缓存。"""
        async with self.db.session() as session:
            result = await session.execute(select(GroupConfig))
            rows = result.scalars().all()
        self._configs = {r.group_wxid: r for r in rows}
        self._cache_ready = True
        self._regex_cache.clear()
        logger.info(f"群配置已加载: {len(self._configs)} 个群")
        return len(self._configs)

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    def _get_config(self, group_wxid: str) -> Optional[GroupConfig]:
        """从缓存获取群配置。"""
        return self._configs.get(group_wxid)

    def _get_regex(self, pattern: str) -> Optional[re.Pattern[str]]:
        """获取编译后的正则（带缓存）。"""
        if pattern in self._regex_cache:
            return self._regex_cache[pattern]
        try:
            compiled = re.compile(pattern)
            self._regex_cache[pattern] = compiled
            return compiled
        except re.error as e:
            logger.warning(f"正则编译失败 '{pattern}': {e}")
            return None

    @staticmethod
    def _cron_match(cron_expr: str, now: datetime) -> bool:
        """简化 cron 匹配（仅支持 5 字段：分 时 日 月 周）。

        支持 ``*``、具体值、逗号列表。用于定时公告触发判断。
        """
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        minute, hour, day, month, weekday = parts
        checks = [
            (minute, now.minute),
            (hour, now.hour),
            (day, now.day),
            (month, now.month),
            (weekday, now.weekday() + 1),  # cron 周日=0/7，这里周一=1
        ]
        for field, value in checks:
            if field == "*":
                continue
            allowed = {int(x) for x in field.split(",") if x.isdigit()}
            if value not in allowed:
                return False
        return True

    async def _record_event(
        self,
        group_wxid: str,
        event_type: str,
        target_wxid: str = "",
        content: str = "",
        msg_id: str = "",
    ) -> None:
        """记录群事件。"""
        try:
            async with self.db.session() as session:
                session.add(GroupEvent(
                    group_wxid=group_wxid,
                    event_type=event_type,
                    target_wxid=target_wxid,
                    content=content,
                    msg_id=msg_id,
                    created_at=datetime.utcnow(),
                ))
                await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"记录群事件失败: {e}")

    async def _update_member_count(self, group_wxid: str, count: int) -> None:
        """更新群成员数快照。"""
        try:
            async with self.db.session() as session:
                cfg = await session.get(GroupConfig, group_wxid)
                if cfg is not None:
                    cfg.member_count = count
                    cfg.updated_at = datetime.utcnow()
                    await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"更新成员数失败: {e}")

    @staticmethod
    def _config_to_dict(cfg: GroupConfig) -> dict[str, Any]:
        """ORM 对象转字典。"""
        return {
            "group_wxid": cfg.group_wxid,
            "group_name": cfg.group_name,
            "welcome_enabled": cfg.welcome_enabled,
            "welcome_text": cfg.welcome_text,
            "anti_ad_enabled": cfg.anti_ad_enabled,
            "anti_ad_keywords": cfg.anti_ad_keywords,
            "anti_ad_regex": cfg.anti_ad_regex,
            "announcement": cfg.announcement,
            "announcement_cron": cfg.announcement_cron,
            "member_count": cfg.member_count,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else "",
        }


# ====================================================================== #
#  独立运行测试（模拟模式）
# ====================================================================== #
async def _self_test() -> None:
    """模拟模式自测：配置、违规检测、欢迎、@所有人、统计。"""
    from wechat.wechat_client import WeChatClient

    client = WeChatClient(instance_id="test", mock=True)
    await client.init("test")
    await client.load_window()

    db = Database(":memory:")
    await db.init()

    module = GroupManagerModule(client, db)
    group = "12345678901@chatroom"

    # 设置群配置
    await module.set_group_config(
        group,
        group_name="测试群A",
        welcome_enabled=True,
        welcome_text="欢迎 {wxid} 加入本群！",
        anti_ad_enabled=True,
        anti_ad_keywords="加微,免费领,http://",
        anti_ad_regex=r"\d{11}",  # 11位数字（手机号）
        announcement="每日提醒：请遵守群规",
        announcement_cron="0 9 * * *",
    )
    await module.reload_configs()

    # 测试违规检测
    ad_msg = MessageData(
        msg_id="g_ad_1",
        sender_wxid="wxid_bad_001",
        receiver_wxid="wxid_self_000",
        content="wxid_bad_001:\n加微免费领礼品 http://xxx.com",
        is_group=True,
        group_wxid=group,
    )
    await module.handle_message(ad_msg)

    # 测试欢迎
    await module.welcome_member(group, "wxid_new_001")

    # 测试 @所有人
    await module.at_all(group, "重要通知：今晚8点开会")

    # 测试统计
    stats = await module.get_member_stats(group)
    logger.info(f"群统计: {stats}")

    cfg = await module.get_group_config(group)
    logger.info(f"群配置: welcome_text={cfg['welcome_text']}")

    await db.close()
    await client.uninstall()
    logger.info("群管理模块自测完成")


if __name__ == "__main__":
    asyncio.run(_self_test())
