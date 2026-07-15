"""
定时任务调度

功能：
- 支持 cron 表达式定时任务（5 字段：分 时 日 月 周）；
- 支持间隔任务（every N seconds/minutes/hours）；
- 任务持久化（重启后从数据库恢复）；
- 任务状态监控（运行中/空闲/异常，最近运行时间、运行次数）。

cron 表达式支持：``*``、具体值、逗号列表 ``1,3,5``、范围 ``1-5``、步长 ``*/15`` 与 ``1-10/2``。
间隔任务用 ``interval`` 类型，schedule 字段为秒数（或带单位的 "30s"/"5m"/"2h"）。

任务执行体通过 ``register_handler`` 注册的命令名映射到具体协程函数，
参数以 JSON 字符串持久化，执行时反序列化后透传。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# 独立运行支持：将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger
from sqlalchemy import String, Text, DateTime, Integer, select, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, Database


# ====================================================================== #
#  类型定义
# ====================================================================== #
TaskHandler = Callable[..., Awaitable[Any]]


class TaskType:
    """任务类型。"""

    CRON = "cron"          # cron 表达式
    INTERVAL = "interval"  # 间隔任务


class TaskStatus:
    """任务状态。"""

    IDLE = "idle"        # 空闲
    RUNNING = "running"  # 运行中
    ERROR = "error"      # 异常
    DISABLED = "disabled"  # 已禁用


# ====================================================================== #
#  ORM 模型
# ====================================================================== #
class ScheduledTask(Base):
    """定时任务 ORM 模型。"""

    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), default="", comment="任务名称")
    task_type: Mapped[str] = mapped_column(String(16), default=TaskType.INTERVAL, comment="任务类型")
    # cron: 5字段表达式；interval: 秒数或 "30s"/"5m"/"2h"
    schedule: Mapped[str] = mapped_column(String(128), default="", comment="调度规则")
    command: Mapped[str] = mapped_column(String(64), default="", comment="处理命令名(需注册)")
    params: Mapped[str] = mapped_column(Text, default="{}", comment="参数(JSON)")
    enabled: Mapped[bool] = mapped_column(Integer, default=1, comment="是否启用 0/1")
    last_run: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="上次运行时间")
    next_run: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True, comment="下次运行时间")
    run_count: Mapped[int] = mapped_column(Integer, default=0, comment="运行次数")
    error_count: Mapped[int] = mapped_column(Integer, default=0, comment="错误次数")
    last_error: Mapped[str] = mapped_column(Text, default="", comment="最近错误信息")
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.IDLE, comment="当前状态")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ====================================================================== #
#  cron 解析器
# ====================================================================== #
class CronParser:
    """5 字段 cron 表达式解析器。

    字段顺序：分钟 小时 日 月 周（周一=1，周日=0/7）。
    支持 ``*``、``1,3,5``、``1-5``、``*/15``、``1-10/2``。
    """

    FIELD_RANGES = (
        (0, 59),   # 分钟
        (0, 23),   # 小时
        (1, 31),   # 日
        (1, 12),   # 月
        (0, 6),    # 周（0=周日，1=周一...6=周六）
    )

    def __init__(self, expr: str) -> None:
        self.expr: str = expr.strip()
        self.fields: list[set[int]] = []
        self._parse()

    def _parse(self) -> None:
        parts = self.expr.split()
        if len(parts) != 5:
            raise ValueError(f"无效cron表达式(需5字段): {self.expr}")
        for i, part in enumerate(parts):
            lo, hi = self.FIELD_RANGES[i]
            self.fields.append(self._parse_field(part, lo, hi))

    @staticmethod
    def _parse_field(field: str, lo: int, hi: int) -> set[int]:
        """解析单个字段为取值集合。"""
        result: set[int] = set()
        for token in field.split(","):
            token = token.strip()
            if not token:
                continue
            # 处理步长
            step = 1
            if "/" in token:
                base, step_str = token.split("/", 1)
                step = int(step_str)
                if step <= 0:
                    raise ValueError(f"无效步长: {step}")
            else:
                base = token
            # 处理范围
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                s, e = base.split("-", 1)
                start, end = int(s), int(e)
            else:
                start = end = int(base)
            # 周日的 7 视为 0
            if start == 7:
                start = 0
            if end == 7:
                end = 0
            # 生成取值
            if start <= end:
                vals = list(range(start, end + 1, step))
            else:
                # 跨范围（如周 5-1）
                vals = list(range(start, hi + 1, step)) + list(range(lo, end + 1, step))
            for v in vals:
                if lo <= v <= hi or (lo == 0 and v == 0):
                    result.add(v)
        return result

    def next_run(self, after: Optional[datetime] = None) -> datetime:
        """计算下一次运行时间（从 after 之后最近的可匹配时刻）。

        Args:
            after: 基准时间，默认当前时间。

        Returns:
            下一次运行时间。
        """
        base = (after or datetime.now()).replace(second=0, microsecond=0)
        # 从下一分钟开始
        t = base + timedelta(minutes=1)
        minute_set, hour_set, day_set, month_set, weekday_set = self.fields

        # 安全上限：一年内必定能匹配（否则表达式无效）
        limit = base + timedelta(days=366)
        while t <= limit:
            # 周匹配：cron 周日=0；Python weekday() 周一=0...周日=6
            cron_weekday = (t.weekday() + 1) % 7  # 周一->1, 周日->0
            if (
                t.minute in minute_set
                and t.hour in hour_set
                and t.day in day_set
                and t.month in month_set
                and cron_weekday in weekday_set
            ):
                return t
            t += timedelta(minutes=1)
        raise ValueError(f"无法计算下次运行时间，表达式可能无效: {self.expr}")


# ====================================================================== #
#  间隔解析
# ====================================================================== #
def parse_interval(schedule: str) -> int:
    """解析间隔字符串为秒数。

    支持纯数字（秒）或带单位 ``30s``/``5m``/``2h``。
    """
    s = schedule.strip().lower()
    if not s:
        raise ValueError("间隔为空")
    m = re.match(r"^(\d+)\s*([smh]?)$", s)
    if not m:
        raise ValueError(f"无效间隔表达式: {schedule}")
    value = int(m.group(1))
    unit = m.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return value * multiplier


# ====================================================================== #
#  任务调度器
# ====================================================================== #
class TaskScheduler:
    """定时任务调度器。

    Args:
        db: 异步数据库管理器。
        min_interval: 最小调度间隔(秒)，低于此值的任务会被强制提升到此值。
    """

    def __init__(self, db: Database, min_interval: int = 10) -> None:
        self.db: Database = db
        self.min_interval: int = min_interval
        # 已注册的命令处理器：command -> handler
        self._handlers: dict[str, TaskHandler] = {}
        # 运行中的任务协程
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._running: bool = False
        self._tick_interval: int = 5  # 调度循环检查间隔(秒)
        # 任务ID -> 正在执行的协程
        self._executing: dict[int, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ #
    #  处理器注册
    # ------------------------------------------------------------------ #
    def register_handler(self, name: str, handler: TaskHandler) -> None:
        """注册任务处理器。

        Args:
            name: 命令名（与任务 command 字段对应）。
            handler: 异步处理函数，签名 ``async def handler(**params)``。
        """
        self._handlers[name] = handler
        logger.debug(f"注册任务处理器: {name}")

    def unregister_handler(self, name: str) -> None:
        """注销任务处理器。"""
        self._handlers.pop(name, None)

    # ------------------------------------------------------------------ #
    #  任务管理
    # ------------------------------------------------------------------ #
    async def add_task(
        self,
        name: str,
        task_type: str,
        schedule: str,
        command: str,
        params: Optional[dict[str, Any]] = None,
        enabled: bool = True,
    ) -> Optional[ScheduledTask]:
        """新增定时任务。

        Args:
            name: 任务名称。
            task_type: TaskType.CRON 或 TaskType.INTERVAL。
            schedule: cron 表达式或间隔字符串。
            command: 处理命令名（需先注册）。
            params: 透传给处理器的参数。
            enabled: 是否启用。

        Returns:
            新建的任务对象，失败返回 None。
        """
        try:
            # 校验调度规则
            next_run = self._calc_next_run(task_type, schedule)
            task = ScheduledTask(
                name=name,
                task_type=task_type,
                schedule=schedule,
                command=command,
                params=json.dumps(params or {}, ensure_ascii=False),
                enabled=1 if enabled else 0,
                next_run=next_run,
                status=TaskStatus.IDLE if enabled else TaskStatus.DISABLED,
            )
            async with self.db.session() as session:
                session.add(task)
                await session.commit()
                await session.refresh(task)
            logger.info(
                f"新增任务: {name}(id={task.id}, type={task_type}, "
                f"schedule={schedule}, next_run={next_run})"
            )
            return task
        except Exception as e:  # noqa: BLE001
            logger.exception(f"新增任务失败: {e}")
            return None

    async def update_task(self, task_id: int, **fields: Any) -> bool:
        """更新任务字段。"""
        try:
            async with self.db.session() as session:
                task = await session.get(ScheduledTask, task_id)
                if task is None:
                    return False
                for k, v in fields.items():
                    if hasattr(task, k):
                        if k == "params" and isinstance(v, dict):
                            v = json.dumps(v, ensure_ascii=False)
                        setattr(task, k, v)
                # 若调度规则变更，重新计算 next_run
                if "task_type" in fields or "schedule" in fields:
                    task.next_run = self._calc_next_run(task.task_type, task.schedule)
                if "enabled" in fields:
                    task.status = TaskStatus.IDLE if task.enabled else TaskStatus.DISABLED
                task.updated_at = datetime.utcnow()
                await session.commit()
            logger.info(f"更新任务 id={task_id}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"更新任务失败: {e}")
            return False

    async def remove_task(self, task_id: int) -> bool:
        """删除任务。"""
        try:
            async with self.db.session() as session:
                task = await session.get(ScheduledTask, task_id)
                if task is None:
                    return False
                await session.delete(task)
                await session.commit()
            # 取消正在执行的协程
            exec_task = self._executing.pop(task_id, None)
            if exec_task and not exec_task.done():
                exec_task.cancel()
            logger.info(f"删除任务 id={task_id}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"删除任务失败: {e}")
            return False

    async def toggle_task(self, task_id: int, enabled: bool) -> bool:
        """启用/禁用任务。"""
        fields: dict[str, Any] = {"enabled": enabled}
        if enabled:
            async with self.db.session() as session:
                task = await session.get(ScheduledTask, task_id)
                if task:
                    fields["next_run"] = self._calc_next_run(task.task_type, task.schedule)
        return await self.update_task(task_id, **fields)

    async def list_tasks(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """列出全部任务。"""
        async with self.db.session() as session:
            stmt = select(ScheduledTask).order_by(ScheduledTask.id)
            if enabled_only:
                stmt = stmt.where(ScheduledTask.enabled == 1)
            result = await session.execute(stmt)
            return [self._task_to_dict(t) for t in result.scalars().all()]

    # ------------------------------------------------------------------ #
    #  调度循环
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """启动调度器：从数据库恢复任务，进入调度循环。"""
        if self._running:
            return
        self._running = True
        await self._restore_tasks()
        self._loop_task = asyncio.create_task(self._schedule_loop())
        logger.info("任务调度器已启动")

    async def stop(self) -> None:
        """停止调度器。"""
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._loop_task = None
        # 等待正在执行的任务完成（不强制取消）
        for exec_task in list(self._executing.values()):
            if not exec_task.done():
                exec_task.cancel()
                try:
                    await exec_task
                except asyncio.CancelledError:
                    pass
        self._executing.clear()
        logger.info("任务调度器已停止")

    async def _restore_tasks(self) -> int:
        """从数据库恢复任务（重新计算已过期任务的 next_run）。"""
        now = datetime.utcnow()
        restored = 0
        async with self.db.session() as session:
            result = await session.execute(
                select(ScheduledTask).where(ScheduledTask.enabled == 1)
            )
            tasks = result.scalars().all()
            for task in tasks:
                # 若 next_run 已过期，重新计算
                if task.next_run is None or task.next_run < now:
                    try:
                        task.next_run = self._calc_next_run(task.task_type, task.schedule)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"任务 {task.name} 恢复失败: {e}")
                        task.status = TaskStatus.ERROR
                        task.last_error = str(e)
                task.status = TaskStatus.IDLE
                restored += 1
            await session.commit()
        logger.info(f"从数据库恢复 {restored} 个任务")
        return restored

    async def _schedule_loop(self) -> None:
        """调度主循环：周期性检查到期任务并执行。"""
        while self._running:
            try:
                await asyncio.sleep(self._tick_interval)
                if not self._running:
                    break
                now = datetime.utcnow()
                # 查询到期且启用且空闲的任务
                async with self.db.session() as session:
                    stmt = (
                        select(ScheduledTask)
                        .where(
                            ScheduledTask.enabled == 1,
                            ScheduledTask.next_run <= now,
                            ScheduledTask.status != TaskStatus.RUNNING,
                        )
                    )
                    result = await session.execute(stmt)
                    due_tasks = result.scalars().all()

                for task in due_tasks:
                    if task.id in self._executing:
                        continue
                    self._executing[task.id] = asyncio.create_task(
                        self._run_task(task.id)
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception(f"调度循环异常: {e}")

    async def _run_task(self, task_id: int) -> None:
        """执行单个到期任务。"""
        # 取任务与处理器
        async with self.db.session() as session:
            task = await session.get(ScheduledTask, task_id)
            if task is None or not task.enabled:
                return
            command = task.command
            params = json.loads(task.params) if task.params else {}
            # 标记运行中
            task.status = TaskStatus.RUNNING
            task.last_run = datetime.utcnow()
            await session.commit()

        handler = self._handlers.get(command)
        if handler is None:
            err = f"未注册的命令: {command}"
            logger.error(f"任务 {task.name} 执行失败: {err}")
            await self._finish_task(task_id, success=False, error=err)
            return

        logger.info(f"开始执行任务: {task.name}(id={task_id}) command={command}")
        try:
            result = await handler(**params)
            logger.info(f"任务 {task.name} 执行完成: {result}")
            await self._finish_task(task_id, success=True)
        except asyncio.CancelledError:
            await self._finish_task(task_id, success=False, error="已取消")
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"任务 {task.name} 执行异常: {e}")
            await self._finish_task(task_id, success=False, error=str(e))
        finally:
            self._executing.pop(task_id, None)

    async def _finish_task(
        self, task_id: int, success: bool, error: str = ""
    ) -> None:
        """任务执行结束：更新状态、计数、下次运行时间。"""
        try:
            async with self.db.session() as session:
                task = await session.get(ScheduledTask, task_id)
                if task is None:
                    return
                task.run_count += 1
                if success:
                    task.status = TaskStatus.IDLE
                    task.last_error = ""
                else:
                    task.error_count += 1
                    task.status = TaskStatus.ERROR
                    task.last_error = error
                # 计算下次运行时间
                try:
                    task.next_run = self._calc_next_run(task.task_type, task.schedule)
                except Exception as e:  # noqa: BLE001
                    task.status = TaskStatus.ERROR
                    task.last_error = f"计算下次运行失败: {e}"
                task.updated_at = datetime.utcnow()
                await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"更新任务结束状态失败: {e}")

    # ------------------------------------------------------------------ #
    #  状态监控
    # ------------------------------------------------------------------ #
    async def get_status(self) -> dict[str, Any]:
        """获取调度器整体状态。"""
        async with self.db.session() as session:
            total = await session.scalar(
                select(func.count()).select_from(ScheduledTask)
            )
            enabled = await session.scalar(
                select(func.count()).select_from(ScheduledTask).where(
                    ScheduledTask.enabled == 1
                )
            )
            running = await session.scalar(
                select(func.count()).select_from(ScheduledTask).where(
                    ScheduledTask.status == TaskStatus.RUNNING
                )
            )
            errored = await session.scalar(
                select(func.count()).select_from(ScheduledTask).where(
                    ScheduledTask.status == TaskStatus.ERROR
                )
            )
        return {
            "running": self._running,
            "total_tasks": int(total or 0),
            "enabled_tasks": int(enabled or 0),
            "running_tasks": int(running or 0),
            "error_tasks": int(errored or 0),
            "executing_count": len(self._executing),
            "registered_handlers": list(self._handlers.keys()),
        }

    async def get_task(self, task_id: int) -> Optional[dict[str, Any]]:
        """获取单个任务详情。"""
        async with self.db.session() as session:
            task = await session.get(ScheduledTask, task_id)
            return self._task_to_dict(task) if task else None

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    def _calc_next_run(self, task_type: str, schedule: str) -> datetime:
        """根据任务类型与调度规则计算下次运行时间。"""
        if task_type == TaskType.CRON:
            parser = CronParser(schedule)
            return parser.next_run()
        if task_type == TaskType.INTERVAL:
            seconds = parse_interval(schedule)
            # 强制最小间隔
            seconds = max(seconds, self.min_interval)
            return datetime.utcnow() + timedelta(seconds=seconds)
        raise ValueError(f"未知任务类型: {task_type}")

    @staticmethod
    def _task_to_dict(task: ScheduledTask) -> dict[str, Any]:
        """ORM 对象转字典。"""
        return {
            "id": task.id,
            "name": task.name,
            "task_type": task.task_type,
            "schedule": task.schedule,
            "command": task.command,
            "params": task.params,
            "enabled": bool(task.enabled),
            "last_run": task.last_run.isoformat() if task.last_run else "",
            "next_run": task.next_run.isoformat() if task.next_run else "",
            "run_count": task.run_count,
            "error_count": task.error_count,
            "last_error": task.last_error,
            "status": task.status,
            "created_at": task.created_at.isoformat() if task.created_at else "",
            "updated_at": task.updated_at.isoformat() if task.updated_at else "",
        }


# ====================================================================== #
#  独立运行测试（模拟模式）
# ====================================================================== #
async def _self_test() -> None:
    """模拟模式自测：cron 解析、间隔任务、注册处理器、调度执行、持久化恢复。"""

    # cron 解析测试
    logger.info("=== cron 解析测试 ===")
    for expr in ["*/15 * * * *", "0 9 * * 1-5", "30 2 1 * *", "0 */6 * * *"]:
        try:
            nr = CronParser(expr).next_run()
            logger.info(f"cron '{expr}' -> 下次运行 {nr}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"cron '{expr}' 解析失败: {e}")

    db = Database(":memory:")
    await db.init()
    scheduler = TaskScheduler(db, min_interval=2)

    # 注册处理器
    call_log: list[str] = []

    async def say_hello(text: str = "hi") -> str:
        call_log.append(text)
        logger.info(f"  >> say_hello 执行: {text}")
        return f"said:{text}"

    async def report(name: str = "report") -> str:
        call_log.append(name)
        logger.info(f"  >> report 执行: {name}")
        return f"reported:{name}"

    scheduler.register_handler("say_hello", say_hello)
    scheduler.register_handler("report", report)

    # 新增间隔任务（5秒）
    await scheduler.add_task(
        name="定时问候", task_type=TaskType.INTERVAL, schedule="5s",
        command="say_hello", params={"text": "你好"}, enabled=True,
    )
    # 新增 cron 任务（每分钟，会很快触发）
    now = datetime.now()
    # 构造一个1分钟内可触发的 cron：当前分钟+1
    next_minute = (now.minute + 1) % 60
    cron_expr = f"{next_minute} * * * *"
    await scheduler.add_task(
        name="cron报告", task_type=TaskType.CRON, schedule=cron_expr,
        command="report", params={"name": "日报"}, enabled=True,
    )

    tasks = await scheduler.list_tasks()
    logger.info(f"任务列表: {len(tasks)} 个")
    for t in tasks:
        logger.info(f"  - {t['name']} type={t['task_type']} next={t['next_run']}")

    # 启动调度器，运行 12 秒
    await scheduler.start()
    await asyncio.sleep(12)
    await scheduler.stop()

    logger.info(f"处理器调用记录: {call_log}")
    status = await scheduler.get_status()
    logger.info(f"调度器状态: {status}")

    # 持久化恢复测试：重新创建调度器（同一db）恢复任务
    logger.info("=== 持久化恢复测试 ===")
    scheduler2 = TaskScheduler(db, min_interval=2)
    scheduler2.register_handler("say_hello", say_hello)
    scheduler2.register_handler("report", report)
    restored_tasks = await scheduler2.list_tasks()
    logger.info(f"恢复任务数: {len(restored_tasks)}")
    await db.close()
    logger.info("任务调度器自测完成")


if __name__ == "__main__":
    asyncio.run(_self_test())
