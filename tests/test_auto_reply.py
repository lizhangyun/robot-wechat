"""
自动回复模块（AutoReplyModule）单元测试

覆盖范围：
- 关键词 / 正则 / 全匹配 三种匹配类型；
- 时间段控制（生效/不生效窗口）；
- 随机延迟生成（random.uniform -> asyncio.sleep）；
- 规则增删改查（CRUD）；
- 命中计数；
- 文本回复发送；
- 不匹配时无回复。

匹配逻辑测试使用无数据库的模块实例（_match 不依赖数据库），
端到端流程使用内存数据库。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from modules import auto_reply as ar_module
from modules.auto_reply import (
    AutoReplyModule,
    AutoReplyRule,
    MatchType,
    ReplyType,
)


# --------------------------------------------------------------------- #
#  夹具
# --------------------------------------------------------------------- #
@pytest.fixture
def matcher(mock_client):
    """无数据库的 AutoReplyModule，仅用于测试匹配/时间窗口逻辑。"""
    # _match / _in_time_window 不访问数据库，传 None 即可
    return AutoReplyModule(mock_client, None)


@pytest_asyncio.fixture
async def auto_reply(mock_client, db):
    """带内存数据库的 AutoReplyModule，用于端到端流程测试。"""
    return AutoReplyModule(mock_client, db)


# --------------------------------------------------------------------- #
#  1. 匹配类型
# --------------------------------------------------------------------- #
def test_keyword_match(matcher):
    """关键词包含匹配：pattern 为逗号分隔多关键词，任一包含即命中。"""
    rule = AutoReplyRule(match_type=MatchType.KEYWORD, pattern="你好,在吗")
    assert matcher._match(rule, "你好啊") is True
    assert matcher._match(rule, "请问在吗") is True
    assert matcher._match(rule, "再见") is False
    assert matcher._match(rule, "") is False  # 空文本不匹配


def test_regex_match(matcher):
    """正则匹配：search 命中即触发。"""
    rule = AutoReplyRule(match_type=MatchType.REGEX, pattern=r"价格|多少钱|费用")
    assert matcher._match(rule, "这个多少钱") is True
    assert matcher._match(rule, "请问价格多少") is True
    assert matcher._match(rule, "再见") is False


def test_regex_invalid_pattern(matcher):
    """非法正则应安全降级（编译失败 -> 不匹配），不抛异常。"""
    rule = AutoReplyRule(match_type=MatchType.REGEX, pattern=r"(*invalid")
    assert matcher._match(rule, "anything") is False


def test_exact_match(matcher):
    """全匹配：去除首尾空白后必须完全一致。"""
    rule = AutoReplyRule(match_type=MatchType.EXACT, pattern="帮助")
    assert matcher._match(rule, "帮助") is True
    assert matcher._match(rule, " 帮助 ") is True  # 空白容忍
    assert matcher._match(rule, "帮助一下") is False


# --------------------------------------------------------------------- #
#  2. 时间段控制
# --------------------------------------------------------------------- #
def test_time_range_no_restriction(matcher):
    """未设置时间段时恒为生效。"""
    rule = AutoReplyRule(time_start="", time_end="")
    assert AutoReplyModule._in_time_window(rule) is True


def test_time_range_covers_now(matcher):
    """覆盖当前时刻的窗口应生效。"""
    now = datetime.now()
    start = (now - timedelta(minutes=30)).strftime("%H:%M")
    end = (now + timedelta(minutes=30)).strftime("%H:%M")
    rule = AutoReplyRule(time_start=start, time_end=end)
    assert AutoReplyModule._in_time_window(rule) is True


def test_time_range_future_window(matcher):
    """完全在未来的窗口不应生效（当前时刻不在其中）。"""
    now = datetime.now()
    start = (now + timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=2)).strftime("%H:%M")
    rule = AutoReplyRule(time_start=start, time_end=end)
    assert AutoReplyModule._in_time_window(rule) is False


def test_time_range_full_day(matcher):
    """全天窗口 00:00~23:59 应始终生效。"""
    rule = AutoReplyRule(time_start="00:00", time_end="23:59")
    assert AutoReplyModule._in_time_window(rule) is True


# --------------------------------------------------------------------- #
#  3. 随机延迟
# --------------------------------------------------------------------- #
async def test_random_delay(auto_reply, monkeypatch, make_message):
    """随机延迟由 random.uniform 生成并传给 asyncio.sleep，值落在 [min,max] 内。"""
    fixed_delay = 1.23
    captured: dict[str, float] = {}

    # 固定 random.uniform 返回值
    monkeypatch.setattr(
        ar_module.random, "uniform", lambda a, b: fixed_delay
    )
    # 拦截 asyncio.sleep，记录延迟且不真正睡眠
    async def fake_sleep(delay):
        captured["delay"] = delay

    monkeypatch.setattr(ar_module.asyncio, "sleep", fake_sleep)

    await auto_reply.add_rule({
        "name": "问候",
        "match_type": MatchType.KEYWORD,
        "pattern": "你好",
        "reply_type": ReplyType.TEXT,
        "reply_content": "你好，有什么可以帮您？",
        "min_delay": 1.0,
        "max_delay": 2.0,
        "priority": 10,
    })

    msg = make_message("你好", is_group=False, group_wxid=None)
    result = await auto_reply.handle_message(msg)
    assert result is not None
    assert result.success is True
    # 延迟值应等于固定值，且在 [1.0, 2.0] 区间内
    assert captured["delay"] == fixed_delay
    assert 1.0 <= captured["delay"] <= 2.0


# --------------------------------------------------------------------- #
#  4. 规则 CRUD
# --------------------------------------------------------------------- #
async def test_rule_crud(auto_reply):
    """规则的增、查、改、禁用、删全流程。"""
    # 新增 3 条规则（优先级不同）
    a = await auto_reply.add_rule({
        "name": "A", "match_type": MatchType.KEYWORD, "pattern": "你好",
        "reply_content": "A-reply", "priority": 10,
    })
    b = await auto_reply.add_rule({
        "name": "B", "match_type": MatchType.REGEX, "pattern": "价格",
        "reply_content": "B-reply", "priority": 5,
    })
    c = await auto_reply.add_rule({
        "name": "C", "match_type": MatchType.EXACT, "pattern": "帮助",
        "reply_content": "C-reply", "priority": 8,
    })
    assert a is not None and b is not None and c is not None

    # 查询：按优先级降序 -> A(10), C(8), B(5)
    rules = await auto_reply.list_rules()
    assert len(rules) == 3
    assert [r["name"] for r in rules] == ["A", "C", "B"]

    # 改：更新 B 的回复内容
    ok = await auto_reply.update_rule(b.id, {"reply_content": "B-new"})
    assert ok is True
    rules = await auto_reply.list_rules()
    b_row = next(r for r in rules if r["name"] == "B")
    assert b_row["reply_content"] == "B-new"

    # 禁用 A：仅启用列表应不含 A
    assert await auto_reply.toggle_rule(a.id, False) is True
    enabled = await auto_reply.list_rules(enabled_only=True)
    assert len(enabled) == 2
    assert all(r["name"] != "A" for r in enabled)

    # 删 C
    assert await auto_reply.delete_rule(c.id) is True
    rules = await auto_reply.list_rules()
    assert len(rules) == 2
    assert all(r["name"] != "C" for r in rules)

    # 删除不存在的规则返回 False
    assert await auto_reply.delete_rule(99999) is False


# --------------------------------------------------------------------- #
#  5. 命中计数
# --------------------------------------------------------------------- #
async def test_hit_count(auto_reply, make_message):
    """命中规则后 hit_count 应自增。"""
    rule = await auto_reply.add_rule({
        "name": "问候", "match_type": MatchType.KEYWORD, "pattern": "你好",
        "reply_content": "你好呀", "min_delay": 0.0, "max_delay": 0.0,
        "priority": 10,
    })
    assert rule is not None

    # 触发两次
    for i in range(2):
        msg = make_message("你好", msg_id=f"hc_{i}", is_group=False, group_wxid=None)
        res = await auto_reply.handle_message(msg)
        assert res is not None and res.success

    rules = await auto_reply.list_rules()
    assert rules[0]["hit_count"] == 2


# --------------------------------------------------------------------- #
#  6. 文本回复
# --------------------------------------------------------------------- #
async def test_reply_text(auto_reply, make_message):
    """文本回复应通过 send_text 发出，内容与规则一致。"""
    await auto_reply.add_rule({
        "name": "文本回复", "match_type": MatchType.KEYWORD, "pattern": "你好",
        "reply_type": ReplyType.TEXT, "reply_content": "您好，欢迎咨询！",
        "min_delay": 0.0, "max_delay": 0.0, "priority": 10,
    })
    msg = make_message("你好啊", msg_id="rt_1", is_group=False, group_wxid=None)
    result = await auto_reply.handle_message(msg)
    assert result is not None
    assert result.success is True
    # 客户端应收到一条文本
    assert len(auto_reply.client.sent_texts) == 1
    target, text = auto_reply.client.sent_texts[0]
    assert target == msg.sender_wxid
    assert text == "您好，欢迎咨询！"


async def test_reply_priority(auto_reply, make_message):
    """优先级高的规则先匹配并回复。"""
    await auto_reply.add_rule({
        "name": "低优先级", "match_type": MatchType.KEYWORD, "pattern": "你好",
        "reply_content": "低", "min_delay": 0.0, "max_delay": 0.0, "priority": 1,
    })
    await auto_reply.add_rule({
        "name": "高优先级", "match_type": MatchType.KEYWORD, "pattern": "你好",
        "reply_content": "高", "min_delay": 0.0, "max_delay": 0.0, "priority": 100,
    })
    msg = make_message("你好", msg_id="pr_1", is_group=False, group_wxid=None)
    await auto_reply.handle_message(msg)
    assert auto_reply.client.sent_texts[0][1] == "高"


# --------------------------------------------------------------------- #
#  7. 不匹配时无回复
# --------------------------------------------------------------------- #
async def test_no_match(auto_reply, make_message):
    """无规则命中时应返回 None 且不发送任何消息。"""
    await auto_reply.add_rule({
        "name": "仅你好", "match_type": MatchType.KEYWORD, "pattern": "你好",
        "reply_content": "hi", "min_delay": 0.0, "max_delay": 0.0, "priority": 10,
    })
    msg = make_message("今天天气真好", msg_id="nm_1", is_group=False, group_wxid=None)
    result = await auto_reply.handle_message(msg)
    assert result is None
    assert auto_reply.client.sent_texts == []


async def test_no_rules(auto_reply, make_message):
    """无任何规则时 handle_message 直接返回 None。"""
    msg = make_message("你好", msg_id="nr_1", is_group=False, group_wxid=None)
    result = await auto_reply.handle_message(msg)
    assert result is None
    assert auto_reply.client.sent_texts == []
