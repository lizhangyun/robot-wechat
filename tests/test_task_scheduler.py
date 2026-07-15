"""
定时任务调度（TaskScheduler / CronParser / parse_interval）单元测试

覆盖范围：
- cron 表达式解析（*、*/5、1-5、逗号列表、周日 7->0、非法表达式）；
- cron 触发判定（每分钟、每 5 分钟、工作日）；
- 间隔任务解析与最小间隔强制；
- 任务持久化（落盘后重启可恢复）；
- 重启后恢复（过期 next_run 重新计算）；
- 任务状态监控（运行次数、状态、已注册处理器）；
- 下次运行时间计算。

使用内存数据库与临时文件数据库，不依赖真实微信。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from database import Database
from modules.task_scheduler import (
    CronParser,
    ScheduledTask,
    TaskScheduler,
    TaskStatus,
    TaskType,
    parse_interval,
)


# --------------------------------------------------------------------- #
#  夹具
# --------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def scheduler(db):
    """min_interval 较小的调度器，便于间隔断言。"""
    return TaskScheduler(db, min_interval=1)


# --------------------------------------------------------------------- #
#  1. cron 表达式解析
# --------------------------------------------------------------------- #
def test_cron_parse():
    """解析常见 cron 表达式，校验各字段取值集合。"""
    # 全通配
    p = CronParser("* * * * *")
    assert p.fields[0] == set(range(60))   # 分钟 0..59
    assert p.fields[1] == set(range(24))   # 小时 0..23
    assert p.fields[2] == set(range(1, 32))  # 日 1..31
    assert p.fields[3] == set(range(1, 13))  # 月 1..12
    assert p.fields[4] == set(range(7))     # 周 0..6

    # 步长 */5
    p = CronParser("*/5 * * * *")
    assert p.fields[0] == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
    assert p.fields[1] == set(range(24))

    # 范围 + 步长 1-5
    p = CronParser("0 9 * * 1-5")
    assert p.fields[0] == {0}
    assert p.fields[1] == {9}
    assert p.fields[4] == {1, 2, 3, 4, 5}  # 周一..周五

    # 逗号列表 + 周日 7 视为 0
    p = CronParser("0,30 9 * * 0,6,7")
    assert p.fields[0] == {0, 30}
    assert p.fields[4] == {0, 6}  # 7 被归一为 0（周日）


def test_cron_parse_invalid():
    """非法 cron 表达式应抛 ValueError。"""
    with pytest.raises(ValueError):
        CronParser("bad expr")          # 非 5 字段
    with pytest.raises(ValueError):
        CronParser("a b c d e")        # 非数字
    with pytest.raises(ValueError):
        CronParser("*/0 * * * *")      # 步长为 0


# --------------------------------------------------------------------- #
#  2. cron 触发判定
# --------------------------------------------------------------------- #
def test_cron_every_minute():
    """* * * * * 下一次运行恰为基准时间的下一分钟。"""
    base = datetime(2026, 7, 14, 10, 30, 0)
    nr = CronParser("* * * * *").next_run(base)
    assert nr == datetime(2026, 7, 14, 10, 31, 0)


def test_cron_every_5_min():
    """*/5 * * * * 从 10:32 起，下一次落在 10:35。"""
    base = datetime(2026, 7, 14, 10, 32, 0)
    nr = CronParser("*/5 * * * *").next_run(base)
    assert nr == datetime(2026, 7, 14, 10, 35, 0)


def test_cron_weekday():
    """0 9 * * 1-5 下一次运行必须是工作日 09:00。"""
    base = datetime(2026, 7, 13, 8, 0, 0)
    nr = CronParser("0 9 * * 1-5").next_run(base)
    assert nr > base
    assert nr.hour == 9
    assert nr.minute == 0
    # Python weekday(): 周一=0 .. 周日=6；工作日即 0..4
    assert nr.weekday() in {0, 1, 2, 3, 4}


def test_cron_specific_day():
    """30 2 1 * * 每月 1 日 02:30 触发。"""
    base = datetime(2026, 7, 14, 12, 0, 0)
    nr = CronParser("30 2 1 * *").next_run(base)
    assert nr.day == 1
    assert nr.hour == 2
    assert nr.minute == 30
    assert nr > base


# --------------------------------------------------------------------- #
#  3. 间隔任务
# --------------------------------------------------------------------- #
def test_parse_interval():
    """间隔字符串解析为秒数。"""
    assert parse_interval("30") == 30
    assert parse_interval("30s") == 30
    assert parse_interval("5m") == 300
    assert parse_interval("2h") == 7200
    with pytest.raises(ValueError):
        parse_interval("")
    with pytest.raises(ValueError):
        parse_interval("abc")


async def test_interval_task(scheduler):
    """新增间隔任务：类型、调度规则、下次运行时间约等于 now+30s。"""
    task = await scheduler.add_task(
        name="每30秒", task_type=TaskType.INTERVAL, schedule="30s",
        command="heartbeat", params={"k": "v"}, enabled=True,
    )
    assert task is not None
    assert task.task_type == TaskType.INTERVAL
    assert task.schedule == "30s"
    assert task.enabled == 1
    assert task.status == TaskStatus.IDLE
    # next_run 应在 now 之后约 30 秒
    now = datetime.utcnow()
    delta = (task.next_run - now).total_seconds()
    assert 25 <= delta <= 35


async def test_interval_min_clamp(db):
    """低于 min_interval 的间隔任务被强制提升到 min_interval。"""
    sched = TaskScheduler(db, min_interval=10)
    task = await sched.add_task(
        name="高频任务", task_type=TaskType.INTERVAL, schedule="5s",
        command="x", enabled=True,
    )
    assert task is not None
    now = datetime.utcnow()
    delta = (task.next_run - now).total_seconds()
    # 5s 被 clamp 到 10s
    assert 8 <= delta <= 14


# --------------------------------------------------------------------- #
#  4. 任务持久化
# --------------------------------------------------------------------- #
async def test_task_persistence(tmp_path):
    """任务落盘后，重启（新建数据库与调度器）应能读回。"""
    db_path = tmp_path / "tasks.db"
    db1 = Database(str(db_path))
    await db1.init()
    sched1 = TaskScheduler(db1, min_interval=1)
    await sched1.add_task("持久任务A", TaskType.INTERVAL, "60s", "cmd_a")
    await sched1.add_task("持久任务B", TaskType.CRON, "0 9 * * *", "cmd_b")
    await db1.close()

    # 模拟重启：用同一文件新建数据库与调度器
    db2 = Database(str(db_path))
    await db2.init()
    sched2 = TaskScheduler(db2, min_interval=1)
    tasks = await sched2.list_tasks()
    assert len(tasks) == 2
    names = {t["name"] for t in tasks}
    assert names == {"持久任务A", "持久任务B"}
    await db2.close()


# --------------------------------------------------------------------- #
#  5. 重启后恢复
# --------------------------------------------------------------------- #
async def test_task_recovery(db):
    """过期任务的 next_run 在恢复时被重新计算为未来时间。"""
    sched = TaskScheduler(db, min_interval=1)
    task = await sched.add_task(
        name="恢复任务", task_type=TaskType.INTERVAL, schedule="60s",
        command="noop", enabled=True,
    )
    assert task is not None

    # 手动将 next_run 置为过去，模拟宕机后过期
    async with db.session() as s:
        row = await s.get(ScheduledTask, task.id)
        row.next_run = datetime.utcnow() - timedelta(hours=1)
        row.status = TaskStatus.ERROR
        await s.commit()

    # 模拟重启：新建调度器并执行恢复
    sched2 = TaskScheduler(db, min_interval=1)
    restored = await sched2._restore_tasks()
    assert restored >= 1

    detail = await sched2.get_task(task.id)
    assert detail is not None
    assert detail["status"] == TaskStatus.IDLE
    # next_run 已被重新计算到未来
    assert detail["next_run"] != ""
    recovered_next = datetime.fromisoformat(detail["next_run"])
    assert recovered_next > datetime.utcnow()


# --------------------------------------------------------------------- #
#  6. 任务状态监控
# --------------------------------------------------------------------- #
async def test_task_status(scheduler):
    """get_status 反映任务总数、启用数、已注册处理器；运行后 run_count 自增。"""
    # 注册处理器
    called: list[str] = []

    async def my_handler(name: str = "default") -> str:
        called.append(name)
        return f"done:{name}"

    scheduler.register_handler("my_cmd", my_handler)

    # 新增 2 个任务，禁用其中一个
    t1 = await scheduler.add_task("任务一", TaskType.INTERVAL, "60s", "my_cmd",
                                 params={"name": "job1"})
    t2 = await scheduler.add_task("任务二", TaskType.INTERVAL, "60s", "my_cmd",
                                 params={"name": "job2"})
    assert t1 is not None and t2 is not None
    await scheduler.toggle_task(t2.id, False)

    status = await scheduler.get_status()
    assert status["total_tasks"] == 2
    assert status["enabled_tasks"] == 1
    assert status["running_tasks"] == 0
    assert "my_cmd" in status["registered_handlers"]

    # 直接执行任务一，验证状态与计数更新
    await scheduler._run_task(t1.id)
    assert called == ["job1"]
    detail = await scheduler.get_task(t1.id)
    assert detail["run_count"] == 1
    assert detail["status"] == TaskStatus.IDLE
    assert detail["last_run"] != ""


async def test_task_error_status(scheduler):
    """执行未注册命令的任务应进入 error 状态并累计错误次数。"""
    task = await scheduler.add_task(
        name="坏任务", task_type=TaskType.INTERVAL, schedule="60s",
        command="not_registered", enabled=True,
    )
    assert task is not None
    await scheduler._run_task(task.id)

    detail = await scheduler.get_task(task.id)
    assert detail["status"] == TaskStatus.ERROR
    assert detail["error_count"] == 1
    assert "未注册" in detail["last_error"]


# --------------------------------------------------------------------- #
#  7. 下次运行时间计算
# --------------------------------------------------------------------- #
def test_next_run_time_cron():
    """_calc_next_run 对 cron 返回下一个匹配时刻（小时分钟正确）。"""
    sched = TaskScheduler.__new__(TaskScheduler)  # 不连数据库，仅用静态逻辑
    sched.min_interval = 1
    nr = sched._calc_next_run(TaskType.CRON, "0 9 * * *")
    assert isinstance(nr, datetime)
    assert nr.hour == 9
    assert nr.minute == 0


def test_next_run_time_interval():
    """_calc_next_run 对 interval 返回约 now + interval 秒。"""
    sched = TaskScheduler.__new__(TaskScheduler)
    sched.min_interval = 1
    before = datetime.utcnow()
    nr = sched._calc_next_run(TaskType.INTERVAL, "30s")
    after = datetime.utcnow()
    delta = (nr - before).total_seconds()
    # 约等于 30 秒，且在合理区间内
    assert 28 <= delta <= 32
    assert nr > before
    assert nr <= after + timedelta(seconds=31)


# --------------------------------------------------------------------- #
#  8. 调度循环执行（端到端）
# --------------------------------------------------------------------- #
async def test_scheduler_loop_executes(db):
    """启动调度器后，到期任务应被实际执行。"""
    sched = TaskScheduler(db, min_interval=1)
    fired: list[str] = []

    async def fire(tag: str = "x") -> str:
        fired.append(tag)
        return tag

    sched.register_handler("fire", fire)

    # 间隔任务，next_run 立即到期（min_interval=1 -> 约 1s 后触发）
    task = await sched.add_task(
        name="即时任务", task_type=TaskType.INTERVAL, schedule="1s",
        command="fire", params={"tag": "boom"}, enabled=True,
    )
    assert task is not None
    # 将 next_run 提前到过去，确保下一轮调度即触发
    async with db.session() as s:
        row = await s.get(ScheduledTask, task.id)
        row.next_run = datetime.utcnow() - timedelta(seconds=1)
        await s.commit()

    await sched.start()
    # 调度循环 tick 间隔 5s，等待一轮
    await asyncio.sleep(6.5)
    await sched.stop()

    assert fired == ["boom"]
    detail = await sched.get_task(task.id)
    assert detail["run_count"] >= 1
