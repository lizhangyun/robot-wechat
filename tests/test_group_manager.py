"""
群管理模块（GroupManagerModule）单元测试

覆盖范围：
- 欢迎新成员入群（含占位符替换与系统消息触发）；
- 广告检测（关键词命中撤回）；
- 关键词撤回 / 正则撤回；
- 群成员统计；
- 定时公告（cron 匹配 + 循环触发）。

使用 Mock 微信客户端与内存数据库，不依赖真实微信。
"""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import and_, func, select

from modules.group_manager import GroupEvent, GroupManagerModule
from wechat.hook_interface import APICommand
from wechat.message_types import MessageType


# --------------------------------------------------------------------- #
#  夹具与辅助
# --------------------------------------------------------------------- #
GROUP_WXID = "12345678901@chatroom"


@pytest_asyncio.fixture
async def gm(mock_client, db):
    """群管理模块实例。"""
    return GroupManagerModule(mock_client, db)


async def _count_events(db, group_wxid: str, event_type: str) -> int:
    """统计指定群、指定类型的事件数量。"""
    async with db.session() as s:
        cnt = await s.scalar(
            select(func.count())
            .select_from(GroupEvent)
            .where(
                and_(
                    GroupEvent.group_wxid == group_wxid,
                    GroupEvent.event_type == event_type,
                )
            )
        )
    return int(cnt or 0)


# --------------------------------------------------------------------- #
#  1. 欢迎新成员
# --------------------------------------------------------------------- #
async def test_welcome_new_member(gm):
    """welcome_member 应发送替换占位符后的欢迎语，并记录 welcome 事件。"""
    await gm.set_group_config(
        GROUP_WXID,
        group_name="测试群A",
        welcome_enabled=True,
        welcome_text="欢迎 {wxid} 加入本群！",
    )

    result = await gm.welcome_member(GROUP_WXID, "wxid_new_001")
    assert result.success is True
    # 占位符 {wxid} 应被替换为成员 wxid
    target, text = gm.client.sent_texts[-1]
    assert target == GROUP_WXID
    assert text == "欢迎 wxid_new_001 加入本群！"
    # welcome 事件已记录
    assert await _count_events(gm.db, GROUP_WXID, "welcome") >= 1


async def test_welcome_via_system_message(gm, make_message):
    """入群系统消息触发自动欢迎。"""
    await gm.set_group_config(
        GROUP_WXID,
        welcome_enabled=True,
        welcome_text="欢迎新朋友！",
    )
    # 系统消息内容含"邀请...加入了群聊"
    msg = make_message(
        '"张三"邀请了"李四"加入了群聊',
        msg_id="sys_join_1",
        sender_wxid="wxid_system",
        group_wxid=GROUP_WXID,
        msg_type=MessageType.SYSTEM,
    )
    await gm.handle_message(msg)

    sent = [t for wxid, t in gm.client.sent_texts if wxid == GROUP_WXID]
    assert any("欢迎新朋友" in t for t in sent)
    assert await _count_events(gm.db, GROUP_WXID, "welcome") >= 1


async def test_welcome_disabled(gm, make_message):
    """关闭欢迎功能后，入群系统消息不应触发欢迎。"""
    await gm.set_group_config(GROUP_WXID, welcome_enabled=False, welcome_text="hi")
    msg = make_message(
        '"张三"邀请了"李四"加入了群聊',
        msg_id="sys_join_2",
        sender_wxid="wxid_system",
        group_wxid=GROUP_WXID,
        msg_type=MessageType.SYSTEM,
    )
    await gm.handle_message(msg)
    assert gm.client.sent_texts == []


# --------------------------------------------------------------------- #
#  2. 广告检测（关键词）
# --------------------------------------------------------------------- #
async def test_detect_advertisement(gm, make_message):
    """含违规关键词的消息应被撤回并提示。"""
    await gm.set_group_config(
        GROUP_WXID,
        anti_ad_enabled=True,
        anti_ad_keywords="加微,免费领,http://",
    )
    msg = make_message(
        "加微免费领礼品 http://x.com",
        msg_id="ad_1",
        sender_wxid="wxid_bad_001",
        group_wxid=GROUP_WXID,
    )
    hit = await gm._check_violation(msg)
    assert hit is True

    # 应调用撤回 API
    revoke_calls = [c for c in gm.client.api_calls if c[0] == APICommand.REVOKE_MSG]
    assert len(revoke_calls) == 1
    assert revoke_calls[0][1]["msg_id"] == "ad_1"
    # 应发送违规提示
    warning = [t for wxid, t in gm.client.sent_texts if wxid == GROUP_WXID]
    assert any("违规内容已被撤回" in t for t in warning)
    # revoke 事件已记录
    assert await _count_events(gm.db, GROUP_WXID, "revoke") >= 1


async def test_detect_clean_message(gm, make_message):
    """正常消息不应触发撤回。"""
    await gm.set_group_config(
        GROUP_WXID, anti_ad_enabled=True, anti_ad_keywords="加微,免费领",
    )
    msg = make_message(
        "今天天气真好，大家中午吃什么",
        msg_id="clean_1",
        sender_wxid="wxid_good_001",
        group_wxid=GROUP_WXID,
    )
    hit = await gm._check_violation(msg)
    assert hit is False
    assert all(c[0] != APICommand.REVOKE_MSG for c in gm.client.api_calls)


# --------------------------------------------------------------------- #
#  3. 关键词撤回
# --------------------------------------------------------------------- #
async def test_keyword_revoke(gm, make_message):
    """单独关键词命中即撤回。"""
    await gm.set_group_config(
        GROUP_WXID, anti_ad_enabled=True, anti_ad_keywords="代购,刷单",
    )
    msg = make_message(
        "专业代购海外商品",
        msg_id="kw_1",
        sender_wxid="wxid_spam",
        group_wxid=GROUP_WXID,
    )
    assert await gm._check_violation(msg) is True
    revoke_calls = [c for c in gm.client.api_calls if c[0] == APICommand.REVOKE_MSG]
    assert len(revoke_calls) == 1


async def test_anti_ad_disabled(gm, make_message):
    """关闭广告检测后即使含关键词也不撤回。"""
    await gm.set_group_config(
        GROUP_WXID, anti_ad_enabled=False, anti_ad_keywords="代购",
    )
    msg = make_message(
        "专业代购", msg_id="kw_2",
        sender_wxid="wxid_spam", group_wxid=GROUP_WXID,
    )
    assert await gm._check_violation(msg) is False
    assert all(c[0] != APICommand.REVOKE_MSG for c in gm.client.api_calls)


# --------------------------------------------------------------------- #
#  4. 正则撤回
# --------------------------------------------------------------------- #
async def test_regex_revoke(gm, make_message):
    """正则命中（如 11 位手机号）触发撤回。"""
    await gm.set_group_config(
        GROUP_WXID,
        anti_ad_enabled=True,
        anti_ad_keywords="",  # 关闭关键词，仅靠正则
        anti_ad_regex=r"\d{11}",
    )
    msg = make_message(
        "联系我 13800138000 有优惠",
        msg_id="re_1",
        sender_wxid="wxid_spam",
        group_wxid=GROUP_WXID,
    )
    assert await gm._check_violation(msg) is True
    revoke_calls = [c for c in gm.client.api_calls if c[0] == APICommand.REVOKE_MSG]
    assert len(revoke_calls) == 1


async def test_regex_no_match(gm, make_message):
    """不匹配正则的消息不撤回。"""
    await gm.set_group_config(
        GROUP_WXID, anti_ad_enabled=True, anti_ad_keywords="", anti_ad_regex=r"\d{11}",
    )
    msg = make_message(
        "今天星期三", msg_id="re_2",
        sender_wxid="wxid_good", group_wxid=GROUP_WXID,
    )
    assert await gm._check_violation(msg) is False


# --------------------------------------------------------------------- #
#  5. 群统计
# --------------------------------------------------------------------- #
async def test_group_stats(gm):
    """群统计应返回成员数与事件计数。"""
    # 配置 5 个群成员
    gm.client.group_members_map[GROUP_WXID] = [
        {"wxid": f"wxid_m_{i}", "nickname": f"成员{i}", "display_name": f"成员{i}"}
        for i in range(5)
    ]
    await gm.set_group_config(GROUP_WXID, group_name="测试群A")

    # 制造事件：2 次欢迎 + 1 次撤回
    await gm.welcome_member(GROUP_WXID, "wxid_new_1")
    await gm.welcome_member(GROUP_WXID, "wxid_new_2")
    await gm.recall_message("msg_x", GROUP_WXID, "广告内容", "关键词")

    stats = await gm.get_member_stats(GROUP_WXID)
    assert stats["group_wxid"] == GROUP_WXID
    assert stats["member_count"] == 5
    assert stats["welcome_count"] == 2
    assert stats["revoke_count"] == 1
    assert stats["total_events"] >= 3


# --------------------------------------------------------------------- #
#  6. 定时公告
# --------------------------------------------------------------------- #
def test_cron_match_static():
    """_cron_match 静态方法对常见表达式的匹配判定。"""
    # 通配：任意时刻均匹配
    assert GroupManagerModule._cron_match("* * * * *", datetime(2026, 7, 14, 9, 30)) is True
    # 精确分钟小时匹配
    assert GroupManagerModule._cron_match("30 9 * * *", datetime(2026, 7, 14, 9, 30)) is True
    # 分钟不匹配
    assert GroupManagerModule._cron_match("0 9 * * *", datetime(2026, 7, 14, 9, 30)) is False
    # 小时不匹配
    assert GroupManagerModule._cron_match("30 9 * * *", datetime(2026, 7, 14, 10, 30)) is False
    # 逗号列表匹配
    assert GroupManagerModule._cron_match("0,30 9 * * *", datetime(2026, 7, 14, 9, 30)) is True
    assert GroupManagerModule._cron_match("0,30 9 * * *", datetime(2026, 7, 14, 9, 15)) is False
    # 非法表达式（字段数不对）返回 False
    assert GroupManagerModule._cron_match("0 9 * *", datetime(2026, 7, 14, 9, 0)) is False


async def test_send_announcement(gm):
    """send_announcement 应调用公告 API 并记录事件。"""
    ok = await gm.send_announcement(GROUP_WXID, "每日提醒：请遵守群规")
    assert ok is True
    announce_calls = [c for c in gm.client.api_calls if c[0] == APICommand.GROUP_ANNOUNCEMENT]
    assert len(announce_calls) == 1
    assert announce_calls[0][1]["group_wxid"] == GROUP_WXID
    assert await _count_events(gm.db, GROUP_WXID, "announce") >= 1


async def test_announcement_scheduled(gm):
    """定时公告循环：cron 匹配当前时刻时应自动发送公告。"""
    now = datetime.now()
    # 用当前分钟与下一分钟构造 cron，规避跨分钟边界
    minute_field = f"{now.minute},{(now.minute + 1) % 60}"
    cron_expr = f"{minute_field} * * * *"

    await gm.set_group_config(
        GROUP_WXID,
        group_name="测试群A",
        announcement="定时公告内容测试",
        announcement_cron=cron_expr,
    )

    # 启动循环（1 秒检查一次），等待一轮触发
    await gm.start_scheduled_announcements(check_interval=1)
    await __import__("asyncio").sleep(1.6)
    await gm.stop_scheduled_announcements()

    # 应至少触发一次公告事件
    announce_count = await _count_events(gm.db, GROUP_WXID, "announce")
    assert announce_count >= 1
