"""
线程池管理 - 对应原软件的线程管理模块
使用 concurrent.futures.ThreadPoolExecutor 实现工作线程池, 并通过优先级队列
实现任务的优先级调度。

设计说明:
- submit_task() 将任务包装为带优先级的条目放入优先级堆
- 调度线程( dispatcher )按优先级取出任务后提交给 ThreadPoolExecutor
- 通过信号量限制在途任务数 <= max_workers, 保障高优先级任务优先得到执行
- 提供 async 适配方法 submit_async(), 便于在 async/await 代码中使用
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Union

from loguru import logger


@dataclass(order=True)
class _PrioritizedTask:
    """优先级任务条目

    heapq 按元组比较: 先比较 priority (越小越优先), 再比较 seq (入队顺序, FIFO 兜底)。
    future / fn / args / kwargs 不参与比较。
    """
    priority: int
    seq: int
    future: Future = field(compare=False)
    fn: Callable[..., Any] = field(compare=False)
    args: tuple = field(default_factory=tuple, compare=False)
    kwargs: dict = field(default_factory=dict, compare=False)


class ThreadPoolManager:
    """线程池管理器"""

    def __init__(
        self,
        max_workers: int = 50,
        *,
        thread_name_prefix: str = "robot3-worker",
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers 必须大于 0")
        self._max_workers: int = max_workers
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        # 优先级堆 + 保护锁 / 条件变量
        self._heap: list[_PrioritizedTask] = []
        self._lock: threading.Lock = threading.Lock()
        self._not_empty: threading.Condition = threading.Condition(self._lock)
        # 限制在途任务数, 保持优先级语义
        self._slots: threading.Semaphore = threading.Semaphore(max_workers)
        # 单调递增序列号, 用于 FIFO 兜底排序
        self._counter: itertools.count = itertools.count()
        self._running: bool = True
        # 统计指标
        self._submitted: int = 0
        self._completed: int = 0
        self._failed: int = 0
        # 调度线程
        self._dispatcher: threading.Thread = threading.Thread(
            target=self._dispatch_loop,
            name=f"{thread_name_prefix}-dispatcher",
            daemon=True,
        )
        self._dispatcher.start()
        logger.info(f"线程池管理器已初始化: max_workers={max_workers}")

    # ------------------------------------------------------------------
    # 任务提交
    # ------------------------------------------------------------------
    def submit_task(
        self,
        fn: Callable[..., Any],
        *args: Any,
        priority: int = 0,
        **kwargs: Any,
    ) -> Future:
        """提交任务

        Args:
            fn: 可调用对象
            *args: 位置参数
            priority: 优先级, 数值越小越优先 (默认 0)
            **kwargs: 关键字参数

        Returns:
            concurrent.futures.Future, 可用于获取结果 / 异常
        """
        if not self._running:
            raise RuntimeError("线程池已关闭, 无法提交任务")
        fut: Future = Future()
        seq = next(self._counter)
        task = _PrioritizedTask(
            priority=priority,
            seq=seq,
            future=fut,
            fn=fn,
            args=args,
            kwargs=kwargs,
        )
        with self._not_empty:
            heapq.heappush(self._heap, task)
            self._submitted += 1
            self._not_empty.notify()
        logger.debug(f"任务已提交: priority={priority}, seq={seq}, fn={getattr(fn, '__name__', fn)}")
        return fut

    async def submit_async(
        self,
        fn: Callable[..., Any],
        *args: Any,
        priority: int = 0,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        **kwargs: Any,
    ) -> asyncio.Future:
        """提交任务并返回 asyncio.Future, 便于在协程中 await

        适用于在线程池中执行同步阻塞函数 (如网络请求), 并在事件循环中等待结果。
        """
        loop = loop or asyncio.get_running_loop()
        aio_fut: asyncio.Future = loop.create_future()
        task_fut = self.submit_task(fn, *args, priority=priority, **kwargs)

        def _on_done(f: Future) -> None:
            try:
                exc = f.exception()
            except Exception as e:  # noqa: BLE001
                exc = e
            if exc is not None:
                loop.call_soon_threadsafe(aio_fut.set_exception, exc)
            else:
                loop.call_soon_threadsafe(aio_fut.set_result, f.result())

        task_fut.add_done_callback(_on_done)
        return aio_fut

    # ------------------------------------------------------------------
    # 调度循环 (守护线程)
    # ------------------------------------------------------------------
    def _dispatch_loop(self) -> None:
        """从优先级堆取出任务并提交给 ThreadPoolExecutor 执行"""
        while self._running:
            task = self._pop_task()
            if task is None:
                continue
            # 占用一个在途槽位, 控制并发与优先级
            self._slots.acquire()
            try:
                self._executor.submit(self._run_task, task)
            except RuntimeError:
                # 执行器已关闭
                self._slots.release()
                task.future.set_exception(RuntimeError("线程池执行器已关闭"))
                break

    def _pop_task(self) -> Optional[_PrioritizedTask]:
        """阻塞等待并弹出优先级最高的任务"""
        with self._not_empty:
            while not self._heap and self._running:
                self._not_empty.wait(timeout=0.5)
            if not self._heap:
                return None
            return heapq.heappop(self._heap)

    def _run_task(self, task: _PrioritizedTask) -> None:
        """实际执行任务并回填 Future"""
        start_ts: Optional[float] = None
        try:
            start_ts = time.monotonic()
            result = task.fn(*task.args, **task.kwargs)
            task.future.set_result(result)
            with self._lock:
                self._completed += 1
        except Exception as e:  # noqa: BLE001
            task.future.set_exception(e)
            with self._lock:
                self._failed += 1
            logger.exception(f"线程池任务执行失败: {e}")
        finally:
            self._slots.release()
            if start_ts is not None:
                logger.debug(
                    f"任务完成: fn={getattr(task.fn, '__name__', task.fn)}, "
                    f"耗时={time.monotonic() - start_ts:.3f}s"
                )

    # ------------------------------------------------------------------
    # 状态与关闭
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """获取线程池状态"""
        with self._lock:
            queued = len(self._heap)
            submitted = self._submitted
            completed = self._completed
            failed = self._failed
        in_flight = self._max_workers - self._slots._value  # type: ignore[attr-defined]
        return {
            "max_workers": self._max_workers,
            "queued": queued,
            "in_flight": max(in_flight, 0),
            "submitted": submitted,
            "completed": completed,
            "failed": failed,
            "running": self._running,
        }

    def shutdown(self, wait: bool = True) -> None:
        """关闭线程池"""
        logger.info("正在关闭线程池...")
        self._running = False
        with self._not_empty:
            self._not_empty.notify_all()
        self._executor.shutdown(wait=wait)
        logger.info(
            f"线程池已关闭: 提交={self._submitted}, 完成={self._completed}, 失败={self._failed}"
        )

    def __del__(self) -> None:
        try:
            if self._running:
                self.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


# ============================================================================
# 真实线程池 (基于 concurrent.futures.ThreadPoolExecutor)
# ============================================================================
class RealThreadPool:
    """基于 ``concurrent.futures.ThreadPoolExecutor`` 的真实线程池。

    对应原软件线程池 (每实例 50 个工作线程, 函数: AppendThread / CreateThread /
    BindThreadPool)。与 :class:`ThreadPoolManager` 相比, 本类提供更简洁的 submit /
    map / shutdown 接口, 直接映射标准库 ThreadPoolExecutor 语义, 不带优先级调度。

    用法::

        pool = RealThreadPool(max_workers=50)
        fut = pool.submit(sum, [1, 2, 3])
        print(fut.result())

        results = list(pool.map(lambda x: x * x, range(10)))
        pool.shutdown()
    """

    def __init__(
        self,
        max_workers: int = 50,
        *,
        thread_name_prefix: str = "robot3-real-worker",
    ) -> None:
        """初始化真实线程池。

        Args:
            max_workers: 工作线程数 (默认 50, 与原软件每实例 50 工作线程一致)
            thread_name_prefix: 工作线程名前缀

        Raises:
            ValueError: max_workers 非正数
        """
        if max_workers <= 0:
            raise ValueError("max_workers 必须大于 0")
        self._max_workers: int = max_workers
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._running: bool = True
        # 统计指标
        self._submitted: int = 0
        self._completed: int = 0
        self._failed: int = 0
        self._lock: threading.Lock = threading.Lock()
        logger.info(f"RealThreadPool 已初始化: max_workers={max_workers}")

    # ------------------------------------------------------------------
    # 任务提交
    # ------------------------------------------------------------------
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """提交单个任务到线程池 (对应原软件 AppendThread / CreateThread)。

        Args:
            fn: 可调用对象
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            concurrent.futures.Future, 可用于获取结果 / 异常

        Raises:
            RuntimeError: 线程池已关闭
        """
        if not self._running:
            raise RuntimeError("线程池已关闭, 无法提交任务")
        fut = self._executor.submit(self._wrap, fn, args, kwargs)
        with self._lock:
            self._submitted += 1
        logger.debug(
            f"RealThreadPool 任务已提交: fn={getattr(fn, '__name__', fn)}"
        )
        return fut

    def map(
        self,
        fn: Callable[..., Any],
        iterable: Iterable[Any],
        *,
        timeout: Optional[float] = None,
    ) -> Iterable[Any]:
        """批量提交任务并按输入顺序返回结果迭代器 (对应原软件 BindThreadPool)。

        Args:
            fn: 可调用对象, 接收 iterable 中的每个元素
            iterable: 可迭代对象
            timeout: 每个结果的最长等待时间 (秒)

        Returns:
            结果迭代器 (按输入顺序)

        Raises:
            RuntimeError: 线程池已关闭
        """
        if not self._running:
            raise RuntimeError("线程池已关闭, 无法提交任务")
        items = list(iterable)
        with self._lock:
            self._submitted += len(items)
        logger.debug(
            f"RealThreadPool 批量任务已提交: fn={getattr(fn, '__name__', fn)}, "
            f"count={len(items)}"
        )
        return self._executor.map(fn, items, timeout=timeout)

    def _wrap(
        self,
        fn: Callable[..., Any],
        args: tuple,
        kwargs: dict,
    ) -> Any:
        """任务包装: 执行并更新统计指标。"""
        start_ts: Optional[float] = None
        try:
            start_ts = time.monotonic()
            result = fn(*args, **kwargs)
            with self._lock:
                self._completed += 1
            return result
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._failed += 1
            logger.exception(f"RealThreadPool 任务执行失败: {e}")
            raise
        finally:
            if start_ts is not None:
                logger.debug(
                    f"RealThreadPool 任务完成: fn={getattr(fn, '__name__', fn)}, "
                    f"耗时={time.monotonic() - start_ts:.3f}s"
                )

    # ------------------------------------------------------------------
    # async 适配
    # ------------------------------------------------------------------
    async def submit_async(
        self,
        fn: Callable[..., Any],
        *args: Any,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        **kwargs: Any,
    ) -> asyncio.Future:
        """提交任务并返回 asyncio.Future, 便于在协程中 await。

        适用于在线程池中执行同步阻塞函数 (如网络请求), 并在事件循环中等待结果。
        """
        loop = loop or asyncio.get_running_loop()
        aio_fut: asyncio.Future = loop.create_future()
        task_fut = self.submit(fn, *args, **kwargs)

        def _on_done(f: Future) -> None:
            try:
                exc = f.exception()
            except Exception as e:  # noqa: BLE001
                exc = e
            if exc is not None:
                loop.call_soon_threadsafe(aio_fut.set_exception, exc)
            else:
                loop.call_soon_threadsafe(aio_fut.set_result, f.result())

        task_fut.add_done_callback(_on_done)
        return aio_fut

    # ------------------------------------------------------------------
    # 状态与关闭
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """获取线程池状态。"""
        with self._lock:
            submitted = self._submitted
            completed = self._completed
            failed = self._failed
        return {
            "backend": "real",
            "max_workers": self._max_workers,
            "submitted": submitted,
            "completed": completed,
            "failed": failed,
            "running": self._running,
        }

    def shutdown(self, wait: bool = True) -> None:
        """关闭线程池 (对应原软件线程池销毁)。"""
        if not self._running:
            return
        logger.info("正在关闭 RealThreadPool...")
        self._running = False
        self._executor.shutdown(wait=wait)
        with self._lock:
            submitted = self._submitted
            completed = self._completed
            failed = self._failed
        logger.info(
            f"RealThreadPool 已关闭: 提交={submitted}, 完成={completed}, 失败={failed}"
        )

    def __del__(self) -> None:
        try:
            if self._running:
                self.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


# ============================================================================
# 工厂函数: 根据配置选择线程池实现
# ============================================================================
def create_thread_pool(
    config: Optional[dict] = None,
) -> Union[ThreadPoolManager, "RealThreadPool"]:
    """根据配置创建线程池。

    配置示例::

        # 真实线程池 (默认, 基于 ThreadPoolExecutor)
        {"backend": "real", "max_workers": 50}

        # 优先级线程池 (基于 ThreadPoolManager)
        {"backend": "priority", "max_workers": 50}

    Args:
        config: 配置字典, 默认创建 max_workers=50 的 RealThreadPool

    Returns:
        RealThreadPool 或 ThreadPoolManager
    """
    config = config or {}
    backend = config.get("backend", "real")
    max_workers = int(config.get("max_workers", 50))
    thread_name_prefix = config.get(
        "thread_name_prefix", "robot3-worker"
    )

    if backend in ("priority", "async"):
        logger.info(f"创建优先级线程池 (ThreadPoolManager): max_workers={max_workers}")
        return ThreadPoolManager(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )

    # 默认 / real
    real_prefix = config.get(
        "thread_name_prefix", "robot3-real-worker"
    )
    logger.info(f"创建真实线程池 (RealThreadPool): max_workers={max_workers}")
    return RealThreadPool(max_workers=max_workers, thread_name_prefix=real_prefix)


__all__ = ["ThreadPoolManager", "RealThreadPool", "create_thread_pool"]
