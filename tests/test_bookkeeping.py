"""
记账模块（BookkeepingModule）单元测试

覆盖范围：
- 指令解析（正常/负数/无效）；
- 记账记录创建与持久化；
- 按群、按银行维度统计；
- 日报/周报/月报生成。

使用 Mock 微信客户端与内存数据库，不依赖真实微信与后端。
"""
from __future__ import annotations

import asyncio

import pytest

from config.instance_config import InstanceConfig
from modules.bookkeeping import (
    BookkeepingModule,
    BookkeepingParser,
    BookkeepingRecord,
)


# --------------------------------------------------------------------- #
#  模块夹具
# --------------------------------------------------------------------- #
@pytest.fixture
def bookkeeping(mock_client, db):
    """构造一个启用记账、无后端域名的记账模块实例。"""
    cfg = InstanceConfig(
        instance_id="test",
        jizhang_enabled=True,
        jizhang_domain="",  # 空域名 -> sync_to_backend 直接返回，不走网络
    )
    module = BookkeepingModule(mock_client, db, cfg)
    module.set_group_name("g1@chatroom", "测试群一")
    module.set_group_name("g2@chatroom", "测试群二")
    return module


async def _drain() -> None:
    """排空 handle_message 内部 create_task 产生的后台协程。"""
    # 等待事件循环调度后台任务（sync_to_backend 在空域名下会立即返回）
    await asyncio.sleep(0.05)


# --------------------------------------------------------------------- #
#  1. 指令解析
# --------------------------------------------------------------------- #
def test_parse_command():
    """解析 "记账 100 工商银行 工资" 正常指令。"""
    parser = BookkeepingParser("记账")
    result = parser.parse("记账 100 工商银行 工资")
    assert result is not None
    assert result["amount"] == 100.0
    assert result["bank_name"] == "工商银行"
    assert result["remark"] == "工资"


def test_parse_negative():
    """解析 "记账 -50 建设银行 午餐" 负数金额（退款/支出扣减）。"""
    parser = BookkeepingParser("记账")
    result = parser.parse("记账 -50 建设银行 午餐")
    assert result is not None
    assert result["amount"] == -50.0
    assert result["bank_name"] == "建设银行"
    assert result["remark"] == "午餐"


def test_parse_invalid():
    """无效指令不应被解析为记账记录。"""
    parser = BookkeepingParser("记账")

    # 非记账关键词开头
    assert parser.parse("今天天气不错") is None
    # 缺少银行字段
    assert parser.parse("记账 100") is None
    # 金额非数字
    assert parser.parse("记账 abc 工商银行 备注") is None
    # 空字符串
    assert parser.parse("") is None


async def test_handle_message_ignores_invalid(bookkeeping, make_message):
    """无效指令经 handle_message 处理后不触发记账（返回 None 且不发送确认）。"""
    msg = make_message("今天天气真好", msg_id="invalid_1", group_wxid="g1@chatroom")
    record = await bookkeeping.handle_message(msg)
    assert record is None
    # 不应有任何确认消息发出
    assert bookkeeping.client.sent_texts == []
    # 数据库中也不应有记录
    records = await bookkeeping.get_records()
    assert records == []


async def test_handle_message_records_valid(bookkeeping, make_message):
    """有效记账指令经 handle_message 后应生成记录并发送确认。"""
    msg = make_message(
        "记账 100 工商银行 工资",
        msg_id="valid_1",
        sender_wxid="wxid_zhangsan",
        group_wxid="g1@chatroom",
    )
    record = await bookkeeping.handle_message(msg)
    await _drain()

    assert record is not None
    assert isinstance(record, BookkeepingRecord)
    assert record.amount == 100.0
    assert record.bank_name == "工商银行"
    # 应发出一条确认消息
    assert len(bookkeeping.client.sent_texts) == 1
    target, text = bookkeeping.client.sent_texts[0]
    assert target == "g1@chatroom"
    assert "记账成功" in text
    assert "100" in text


async def test_handle_message_dedup(bookkeeping, make_message):
    """同一 msg_id 重复处理应被去重，只记录一次。"""
    msg = make_message("记账 50 微信 红包", msg_id="dup_1", group_wxid="g1@chatroom")
    first = await bookkeeping.handle_message(msg)
    await _drain()
    second = await bookkeeping.handle_message(msg)  # 同一 msg_id
    await _drain()
    assert first is not None
    assert second is None
    records = await bookkeeping.get_records()
    assert len(records) == 1


# --------------------------------------------------------------------- #
#  2. 记账记录创建
# --------------------------------------------------------------------- #
async def test_record_creation(bookkeeping):
    """add_record 创建记录后应能通过 get_records 查询到，字段一致。"""
    record = await bookkeeping.add_record(
        group_wxid="g1@chatroom",
        group_name="测试群一",
        sender_wxid="wxid_zhangsan",
        sender_name="张三",
        amount=88.5,
        bank_name="支付宝",
        remark="测试记录",
        raw_msg="记账 88.5 支付宝 测试记录",
        msg_id="rc_1",
    )
    assert record is not None
    assert record.id is not None
    assert record.amount == 88.5
    assert record.bank_name == "支付宝"
    assert record.sender_name == "张三"
    assert record.sync_status == 0  # 未同步

    # 查询验证
    records = await bookkeeping.get_records(group_wxid="g1@chatroom")
    assert len(records) == 1
    assert records[0]["amount"] == 88.5
    assert records[0]["bank_name"] == "支付宝"
    assert records[0]["sender_name"] == "张三"


# --------------------------------------------------------------------- #
#  3. 统计
# --------------------------------------------------------------------- #
async def _seed_stats_data(module: BookkeepingModule) -> None:
    """灌入多群多渠道的记账样本数据。"""
    samples = [
        ("g1@chatroom", "测试群一", "wxid_a", "张三", 100.0, "工商银行", "工资"),
        ("g1@chatroom", "测试群一", "wxid_b", "李四", -50.0, "微信", "退款"),
        ("g2@chatroom", "测试群二", "wxid_c", "王五", 200.0, "工商银行", "奖金"),
    ]
    for i, (gw, gn, sw, sn, amt, bank, remark) in enumerate(samples):
        await module.add_record(
            group_wxid=gw, group_name=gn, sender_wxid=sw, sender_name=sn,
            amount=amt, bank_name=bank, remark=remark,
            raw_msg=f"记账 {amt} {bank} {remark}", msg_id=f"seed_{i}",
        )


async def test_stats_by_group(bookkeeping):
    """按群维度统计：笔数、总额、收入、支出正确。"""
    await _seed_stats_data(bookkeeping)
    stats = await bookkeeping.get_statistics(dimension="group")
    # 转为 key->row 便于断言
    by_key = {row["key"]: row for row in stats}

    g1 = by_key["g1@chatroom"]
    assert g1["count"] == 2
    assert g1["total_amount"] == pytest.approx(50.0)  # 100 + (-50)
    assert g1["income"] == pytest.approx(100.0)
    assert g1["expense"] == pytest.approx(-50.0)

    g2 = by_key["g2@chatroom"]
    assert g2["count"] == 1
    assert g2["total_amount"] == pytest.approx(200.0)
    assert g2["income"] == pytest.approx(200.0)
    assert g2["expense"] == pytest.approx(0.0)


async def test_stats_by_bank(bookkeeping):
    """按银行维度统计：工商银行汇总 100+200，微信汇总 -50。"""
    await _seed_stats_data(bookkeeping)
    stats = await bookkeeping.get_statistics(dimension="bank")
    by_key = {row["key"]: row for row in stats}

    icbc = by_key["工商银行"]
    assert icbc["count"] == 2
    assert icbc["total_amount"] == pytest.approx(300.0)

    wechat = by_key["微信"]
    assert wechat["count"] == 1
    assert wechat["total_amount"] == pytest.approx(-50.0)


# --------------------------------------------------------------------- #
#  4. 报表
# --------------------------------------------------------------------- #
async def _seed_report_data(module: BookkeepingModule) -> None:
    """灌入报表所需样本数据。"""
    await module.add_record(
        group_wxid="g1@chatroom", group_name="测试群一",
        sender_wxid="wxid_a", sender_name="张三",
        amount=150.0, bank_name="工商银行", remark="工资",
        raw_msg="记账 150 工商银行 工资", msg_id="rep_1",
    )
    await module.add_record(
        group_wxid="g1@chatroom", group_name="测试群一",
        sender_wxid="wxid_b", sender_name="李四",
        amount=-30.0, bank_name="微信", remark="午餐",
        raw_msg="记账 -30 微信 午餐", msg_id="rep_2",
    )


async def test_daily_report(bookkeeping):
    """日报应包含标题、笔数、收支汇总与渠道明细。"""
    await _seed_report_data(bookkeeping)
    report = await bookkeeping.generate_report(period="daily")
    assert "【记账日报】" in report
    assert "记录笔数: 2" in report
    assert "总收入: 150.00" in report
    assert "总支出: 30.00" in report
    assert "净额: 120.00" in report
    assert "工商银行" in report
    assert "微信" in report


async def test_weekly_report(bookkeeping):
    """周报标题与统计区间正确。"""
    await _seed_report_data(bookkeeping)
    report = await bookkeeping.generate_report(period="weekly")
    assert "【记账周报】" in report
    assert "统计区间" in report
    assert "记录笔数: 2" in report


async def test_monthly_report(bookkeeping):
    """月报标题与净额正确。"""
    await _seed_report_data(bookkeeping)
    report = await bookkeeping.generate_report(period="monthly")
    assert "【记账月报】" in report
    assert "净额: 120.00" in report


async def test_report_empty(bookkeeping):
    """无记录时报表应提示区间内无记录。"""
    report = await bookkeeping.generate_report(period="daily")
    assert "无记账记录" in report
