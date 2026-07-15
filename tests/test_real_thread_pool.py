"""
RealThreadPool 单元测试

测试范围:
  - core/thread_pool.py : RealThreadPool 真实线程池实现

测试内容:
  - RealThreadPool 初始化与参数校验
  - submit() 提交任务并获取结果
  - map() 批量提交任务
  - shutdown() 关闭线程池
  - submit_async() 异步提交任务 (返回 asyncio.Future, 便于在协程中 await)
  - get_status() 状态查询
  - 默认 50 个工作线程
  - 异常传播
  - __del__ 析构自动关闭

注意: 测试需与现有 tests/test_thread_pool.py 共存 (后者测试 ThreadPool 降级方案 /
ThreadPoolManager 优先级线程池)。本文件专门测试 RealThreadPool 真实实现。
"""
from __future__ import annotations

import asyncio
import sys
import time
import threading
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from core.thread_pool import RealThreadPool


# ============================================================================
# 测试: 初始化
# ============================================================================
def test_init_default():
    """默认初始化: 50 个工作线程"""
    pool = RealThreadPool()
    try:
        assert pool._max_workers == 50
        assert pool._running is True
    finally:
        pool.shutdown()


def test_init_custom_workers():
    """自定义工作线程数"""
    pool = RealThreadPool(max_workers=4)
    try:
        assert pool._max_workers == 4
    finally:
        pool.shutdown()


def test_init_invalid_workers_zero():
    """max_workers=0 抛 ValueError"""
    with pytest.raises(ValueError):
        RealThreadPool(max_workers=0)


def test_init_invalid_workers_negative():
    """max_workers 负数抛 ValueError"""
    with pytest.raises(ValueError):
        RealThreadPool(max_workers=-1)


def test_init_thread_name_prefix():
    """自定义线程名前缀传入 ThreadPoolExecutor"""
    pool = RealThreadPool(max_workers=2, thread_name_prefix="TestPool")
    try:
        assert pool._running is True
    finally:
        pool.shutdown()


# ============================================================================
# 测试: submit 提交任务
# ============================================================================
def test_submit_returns_result():
    """submit 提交任务并获取结果"""
    pool = RealThreadPool(max_workers=2)
    try:
        future = pool.submit(lambda: 42)
        assert future.result() == 42
    finally:
        pool.shutdown()


def test_submit_with_args():
    """submit 传递位置参数"""
    pool = RealThreadPool(max_workers=2)
    try:
        future = pool.submit(lambda x, y: x + y, 10, 20)
        assert future.result() == 30
    finally:
        pool.shutdown()


def test_submit_with_kwargs():
    """submit 传递关键字参数"""
    pool = RealThreadPool(max_workers=2)
    try:
        future = pool.submit(lambda a, b: a * b, a=6, b=7)
        assert future.result() == 42
    finally:
        pool.shutdown()


def test_submit_runs_in_thread():
    """submit 在独立线程中执行"""
    pool = RealThreadPool(max_workers=2)
    try:
        main_thread = threading.current_thread()

        def get_thread():
            return threading.current_thread()

        future = pool.submit(get_thread)
        worker_thread = future.result()
        assert worker_thread is not main_thread
    finally:
        pool.shutdown()


def test_submit_exception_propagates():
    """submit 任务抛异常, future.result() 重新抛出"""
    pool = RealThreadPool(max_workers=2)
    try:
        def raise_error():
            raise ValueError("任务异常")

        future = pool.submit(raise_error)
        with pytest.raises(ValueError, match="任务异常"):
            future.result()
    finally:
        pool.shutdown()


def test_submit_multiple_tasks():
    """submit 多个任务并发执行"""
    pool = RealThreadPool(max_workers=4)
    try:
        futures = [pool.submit(lambda i=i: i * i) for i in range(10)]
        results = [f.result() for f in futures]
        assert results == [0, 1, 4, 9, 16, 25, 36, 49, 64, 81]
    finally:
        pool.shutdown()


def test_submit_concurrent_execution():
    """submit 多任务真正并发执行 (总时间小于串行)"""
    pool = RealThreadPool(max_workers=4)
    try:
        def sleep_task():
            time.sleep(0.2)
            return "done"

        start = time.monotonic()
        futures = [pool.submit(sleep_task) for _ in range(4)]
        results = [f.result() for f in futures]
        elapsed = time.monotonic() - start
        # 4 个任务 4 个线程并发, 应远小于 4*0.2=0.8s
        assert elapsed < 0.6, f"并发执行耗时过长: {elapsed}s"
        assert results == ["done"] * 4
    finally:
        pool.shutdown()


def test_submit_updates_stats():
    """submit 更新 submitted 统计"""
    pool = RealThreadPool(max_workers=2)
    try:
        pool.submit(lambda: 1).result()
        pool.submit(lambda: 2).result()
        status = pool.get_status()
        assert status["submitted"] == 2
        assert status["completed"] == 2
    finally:
        pool.shutdown()


# ============================================================================
# 测试: map 批量提交
# ============================================================================
def test_map_basic():
    """map 批量提交并按顺序返回结果"""
    pool = RealThreadPool(max_workers=4)
    try:
        results = list(pool.map(lambda x: x * 2, [1, 2, 3, 4, 5]))
        assert results == [2, 4, 6, 8, 10]
    finally:
        pool.shutdown()


def test_map_empty():
    """map 空输入返回空"""
    pool = RealThreadPool(max_workers=2)
    try:
        results = list(pool.map(lambda x: x, []))
        assert results == []
    finally:
        pool.shutdown()


def test_map_preserves_order():
    """map 保持输入顺序"""
    pool = RealThreadPool(max_workers=4)
    try:
        inputs = [5, 3, 1, 4, 2]
        results = list(pool.map(lambda x: x * 10, inputs))
        assert results == [50, 30, 10, 40, 20]
    finally:
        pool.shutdown()


def test_map_large_batch():
    """map 大批量任务"""
    pool = RealThreadPool(max_workers=4)
    try:
        n = 100
        results = list(pool.map(lambda x: x + 1, range(n)))
        assert results == list(range(1, n + 1))
    finally:
        pool.shutdown()


def test_map_with_string_transform():
    """map 字符串变换"""
    pool = RealThreadPool(max_workers=2)
    try:
        results = list(pool.map(str.upper, ["hello", "world", "test"]))
        assert results == ["HELLO", "WORLD", "TEST"]
    finally:
        pool.shutdown()


def test_map_updates_stats():
    """map 更新 submitted 统计"""
    pool = RealThreadPool(max_workers=2)
    try:
        list(pool.map(lambda x: x, range(5)))
        status = pool.get_status()
        assert status["submitted"] == 5
    finally:
        pool.shutdown()


# ============================================================================
# 测试: submit_async 异步提交
# ============================================================================
def test_submit_async_returns_awaitable():
    """submit_async 返回 asyncio.Future, 可 await 获取结果"""
    pool = RealThreadPool(max_workers=2)
    try:
        async def _run_test():
            # submit_async 是 async def, await 得到 asyncio.Future
            aio_fut = await pool.submit_async(lambda: 42)
            # 再 await Future 得到结果
            result = await aio_fut
            return result

        assert asyncio.run(_run_test()) == 42
    finally:
        pool.shutdown()


def test_submit_async_with_args():
    """submit_async 传递参数"""
    pool = RealThreadPool(max_workers=2)
    try:
        async def _run_test():
            aio_fut = await pool.submit_async(lambda x, y: x * y, 6, 7)
            return await aio_fut

        assert asyncio.run(_run_test()) == 42
    finally:
        pool.shutdown()


def test_submit_async_multiple():
    """submit_async 多次异步提交并发等待"""
    pool = RealThreadPool(max_workers=4)
    try:
        async def _run_test():
            # 收集所有 asyncio.Future
            futs = [await pool.submit_async(lambda i=i: i * 2) for i in range(5)]
            results = await asyncio.gather(*futs)
            return results

        results = asyncio.run(_run_test())
        assert results == [0, 2, 4, 6, 8]
    finally:
        pool.shutdown()


def test_submit_async_exception_propagates():
    """submit_async 任务异常, await 时重新抛出"""
    pool = RealThreadPool(max_workers=2)
    try:
        async def _run_test():
            def raise_err():
                raise RuntimeError("异步任务异常")

            aio_fut = await pool.submit_async(raise_err)
            return await aio_fut  # await Future, 应抛出异常

        with pytest.raises(RuntimeError, match="异步任务异常"):
            asyncio.run(_run_test())
    finally:
        pool.shutdown()


def test_submit_async_concurrent_sleep():
    """submit_async 多任务并发执行 sleep"""
    pool = RealThreadPool(max_workers=4)
    try:
        async def _run_test():
            def sleep_task():
                time.sleep(0.2)
                return "done"

            futs = [await pool.submit_async(sleep_task) for _ in range(4)]
            start = time.monotonic()
            results = await asyncio.gather(*futs)
            elapsed = time.monotonic() - start
            return results, elapsed

        results, elapsed = asyncio.run(_run_test())
        assert results == ["done"] * 4
        # 4 个任务 4 线程并发, 应远小于 4*0.2=0.8s
        assert elapsed < 0.6, f"并发耗时过长: {elapsed}s"
    finally:
        pool.shutdown()


# ============================================================================
# 测试: shutdown 关闭
# ============================================================================
def test_shutdown_basic():
    """shutdown 关闭线程池, _running 置 False"""
    pool = RealThreadPool(max_workers=2)
    pool.shutdown()
    assert pool._running is False


def test_shutdown_idempotent():
    """重复 shutdown 不报错 (已关闭直接返回)"""
    pool = RealThreadPool(max_workers=2)
    pool.shutdown()
    pool.shutdown()  # 第二次不抛异常


def test_shutdown_wait_true():
    """shutdown(wait=True) 等待所有任务完成"""
    pool = RealThreadPool(max_workers=2)
    future = pool.submit(lambda: time.sleep(0.1))
    pool.shutdown(wait=True)
    # 关闭后任务应已完成
    assert future.done()


def test_shutdown_after_submit():
    """提交任务后关闭, 结果仍可获取"""
    pool = RealThreadPool(max_workers=2)
    future = pool.submit(lambda: "result")
    pool.shutdown()
    assert future.result() == "result"


def test_submit_after_shutdown_raises():
    """shutdown 后 submit 抛 RuntimeError"""
    pool = RealThreadPool(max_workers=2)
    pool.shutdown()
    with pytest.raises(RuntimeError):
        pool.submit(lambda: 1)


def test_map_after_shutdown_raises():
    """shutdown 后 map 抛 RuntimeError"""
    pool = RealThreadPool(max_workers=2)
    pool.shutdown()
    with pytest.raises(RuntimeError):
        list(pool.map(lambda x: x, [1, 2, 3]))


# ============================================================================
# 测试: get_status 状态查询
# ============================================================================
def test_get_status_active():
    """活跃状态查询"""
    pool = RealThreadPool(max_workers=4)
    try:
        status = pool.get_status()
        assert status["backend"] == "real"
        assert status["max_workers"] == 4
        assert status["running"] is True
        assert status["submitted"] == 0
        assert status["completed"] == 0
        assert status["failed"] == 0
    finally:
        pool.shutdown()


def test_get_status_after_shutdown():
    """关闭后状态查询"""
    pool = RealThreadPool(max_workers=4)
    pool.shutdown()
    status = pool.get_status()
    assert status["running"] is False
    assert status["max_workers"] == 4


def test_get_status_default_workers():
    """默认 50 个工作线程"""
    pool = RealThreadPool()
    try:
        status = pool.get_status()
        assert status["max_workers"] == 50
    finally:
        pool.shutdown()


def test_get_status_after_tasks():
    """执行任务后状态统计更新"""
    pool = RealThreadPool(max_workers=2)
    try:
        pool.submit(lambda: 1).result()
        pool.submit(lambda: 2).result()
        status = pool.get_status()
        assert status["submitted"] == 2
        assert status["completed"] == 2
        assert status["failed"] == 0
    finally:
        pool.shutdown()


def test_get_status_failed_count():
    """失败任务计入 failed 统计"""
    pool = RealThreadPool(max_workers=2)
    try:
        def fail():
            raise ValueError("失败")

        fut = pool.submit(fail)
        with pytest.raises(ValueError):
            fut.result()
        status = pool.get_status()
        assert status["submitted"] == 1
        assert status["failed"] == 1
    finally:
        pool.shutdown()


# ============================================================================
# 测试: 默认 50 个工作线程
# ============================================================================
def test_default_50_workers():
    """默认创建 50 个工作线程"""
    pool = RealThreadPool()
    try:
        assert pool._max_workers == 50
        status = pool.get_status()
        assert status["max_workers"] == 50
    finally:
        pool.shutdown()


# ============================================================================
# 测试: __del__ 析构自动关闭
# ============================================================================
def test_del_auto_shutdown():
    """对象析构时自动关闭线程池"""
    pool = RealThreadPool(max_workers=2)
    pool_id = id(pool)
    # 触发析构
    del pool
    # 不抛异常即通过 (析构调用 shutdown(wait=False))


# ============================================================================
# 测试: 高级场景
# ============================================================================
def test_nested_submit():
    """嵌套提交任务 (任务内再提交任务)"""
    pool = RealThreadPool(max_workers=4)
    try:
        def outer():
            inner_future = pool.submit(lambda: "inner")
            return inner_future.result()

        future = pool.submit(outer)
        assert future.result() == "inner"
    finally:
        pool.shutdown()


def test_long_running_task():
    """长时间运行任务"""
    pool = RealThreadPool(max_workers=2)
    try:
        def long_task():
            time.sleep(0.3)
            return "completed"

        future = pool.submit(long_task)
        # 等待完成
        assert future.result(timeout=5) == "completed"
    finally:
        pool.shutdown()


def test_task_count_exceeds_workers():
    """任务数超过工作线程数"""
    pool = RealThreadPool(max_workers=2)
    try:
        # 10 个任务, 仅 2 个线程
        futures = [pool.submit(lambda i=i: i) for i in range(10)]
        results = [f.result() for f in futures]
        assert results == list(range(10))
    finally:
        pool.shutdown()


def test_mixed_submit_and_map():
    """混合使用 submit 和 map"""
    pool = RealThreadPool(max_workers=4)
    try:
        # 先 submit
        f1 = pool.submit(lambda: 100)
        # 再 map
        map_results = list(pool.map(lambda x: x * 2, [1, 2, 3]))
        assert f1.result() == 100
        assert map_results == [2, 4, 6]
    finally:
        pool.shutdown()
