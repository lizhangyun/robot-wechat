"""
线程池单元测试

测试范围:
  - core/thread_pool.py : ThreadPoolManager 优先级线程池

测试内容:
  - 任务提交和执行
  - 优先级队列 (high 先于 low)
  - 并发执行
  - 状态获取
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from concurrent.futures import Future

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.thread_pool import ThreadPoolManager


# ============================================================================
# 测试: 任务提交和执行
# ============================================================================
def test_submit_task():
    """测试任务提交和执行"""
    pool = ThreadPoolManager(max_workers=4)
    try:
        # 提交简单任务
        fut = pool.submit_task(sum, [1, 2, 3, 4, 5])
        result = fut.result(timeout=5.0)
        assert result == 15, f"任务结果不正确: {result}"

        # 提交带参数的任务
        fut2 = pool.submit_task(lambda x, y: x * y, 6, 7)
        result2 = fut2.result(timeout=5.0)
        assert result2 == 42, f"带参数任务结果不正确: {result2}"

        # 提交无返回值任务
        fut3 = pool.submit_task(lambda: None)
        fut3.result(timeout=5.0)  # 不抛异常即成功
    finally:
        pool.shutdown(wait=True)


def test_submit_task_with_kwargs():
    """测试带关键字参数的任务提交"""
    pool = ThreadPoolManager(max_workers=2)
    try:
        def task(a, b, c=10):
            return a + b + c

        fut = pool.submit_task(task, 1, 2, c=30)
        result = fut.result(timeout=5.0)
        assert result == 33, f"关键字参数任务结果不正确: {result}"
    finally:
        pool.shutdown(wait=True)


def test_submit_task_exception():
    """测试任务执行异常"""
    pool = ThreadPoolManager(max_workers=2)
    try:
        def failing_task():
            raise ValueError("任务执行失败测试")

        fut = pool.submit_task(failing_task)
        try:
            fut.result(timeout=5.0)
            assert False, "应抛出异常"
        except ValueError as e:
            assert "任务执行失败测试" in str(e)
    finally:
        pool.shutdown(wait=True)


def test_submit_after_shutdown():
    """测试关闭后提交任务应抛异常"""
    pool = ThreadPoolManager(max_workers=2)
    pool.shutdown(wait=True)
    try:
        pool.submit_task(sum, [1, 2])
        assert False, "关闭后提交应抛出 RuntimeError"
    except RuntimeError:
        pass  # 预期行为


# ============================================================================
# 测试: 优先级队列
# ============================================================================
def test_priority_queue():
    """测试优先级队列 (high 先于 low)"""
    # 使用单线程, 确保按优先级顺序执行
    pool = ThreadPoolManager(max_workers=1)
    try:
        execution_order = []
        import threading
        lock = threading.Lock()

        def make_task(name):
            def task():
                with lock:
                    execution_order.append(name)
                return name
            return task

        # 先提交一个阻塞任务占住唯一的 worker
        barrier = threading.Event()

        def blocker():
            barrier.wait(timeout=5.0)
            return "blocker_done"

        block_fut = pool.submit_task(blocker, priority=0)

        # 等待阻塞任务开始执行
        time.sleep(0.3)

        # 按优先级从高到低提交任务 (高优先级先提交, 避免竞态)
        pool.submit_task(make_task("high_1"), priority=1)
        # 短暂等待, 让 dispatcher 弹出 high_1 并阻塞在 slot 上
        time.sleep(0.1)
        # 此时 dispatcher 已持有 high_1 并等待 slot, 剩余任务进入堆
        pool.submit_task(make_task("high_2"), priority=1)
        pool.submit_task(make_task("mid_1"), priority=5)
        pool.submit_task(make_task("low_1"), priority=10)

        # 确保所有任务都在堆中
        time.sleep(0.1)

        # 释放阻塞
        barrier.set()
        block_fut.result(timeout=5.0)

        # 等待所有任务完成
        time.sleep(1.0)

        # 验证高优先级任务先于低优先级执行
        # high_1 和 high_2 (priority=1) 应在 mid_1 (priority=5) 之前
        # mid_1 应在 low_1 (priority=10) 之前
        high_indices = [i for i, name in enumerate(execution_order) if name.startswith("high")]
        mid_indices = [i for i, name in enumerate(execution_order) if name.startswith("mid")]
        low_indices = [i for i, name in enumerate(execution_order) if name.startswith("low")]

        assert len(high_indices) == 2, f"应有2个高优先级任务: {execution_order}"
        assert len(mid_indices) == 1, f"应有1个中优先级任务: {execution_order}"
        assert len(low_indices) == 1, f"应有1个低优先级任务: {execution_order}"

        if high_indices and mid_indices:
            assert max(high_indices) < min(mid_indices), \
                f"高优先级应在中等优先级之前: {execution_order}"
        if mid_indices and low_indices:
            assert max(mid_indices) < min(low_indices), \
                f"中等优先级应在低优先级之前: {execution_order}"
    finally:
        pool.shutdown(wait=True)


def test_priority_same_fifo():
    """测试相同优先级按 FIFO 顺序执行"""
    pool = ThreadPoolManager(max_workers=1)
    try:
        import threading
        execution_order = []
        lock = threading.Lock()

        def make_task(name):
            def task():
                with lock:
                    execution_order.append(name)
                return name
            return task

        barrier = threading.Event()

        def blocker():
            barrier.wait(timeout=5.0)
            return "done"

        pool.submit_task(blocker, priority=0)
        time.sleep(0.2)

        # 相同优先级, 按 FIFO
        for i in range(5):
            pool.submit_task(make_task(f"task_{i}"), priority=0)

        barrier.set()
        time.sleep(1.0)

        # 验证 FIFO 顺序 (排除 blocker)
        non_blocker = [name for name in execution_order if name.startswith("task_")]
        for i in range(len(non_blocker) - 1):
            expected_current = int(non_blocker[i].split("_")[1])
            expected_next = int(non_blocker[i + 1].split("_")[1])
            assert expected_current < expected_next, \
                f"FIFO顺序错误: {non_blocker}"
    finally:
        pool.shutdown(wait=True)


# ============================================================================
# 测试: 并发执行
# ============================================================================
def test_concurrent_tasks():
    """测试并发执行"""
    pool = ThreadPoolManager(max_workers=4)
    try:
        import threading
        active_count = 0
        max_active = 0
        lock = threading.Lock()

        def concurrent_task(task_id):
            nonlocal active_count, max_active
            with lock:
                active_count += 1
                if active_count > max_active:
                    max_active = active_count
            time.sleep(0.1)  # 模拟耗时操作
            with lock:
                active_count -= 1
            return task_id

        # 提交 4 个并发任务
        futures = [pool.submit_task(concurrent_task, i, priority=0) for i in range(4)]

        # 等待全部完成
        results = [f.result(timeout=5.0) for f in futures]

        # 验证结果
        assert sorted(results) == [0, 1, 2, 3]

        # 验证有并发执行 (max_active >= 2)
        assert max_active >= 2, f"并发数过低: {max_active}, 预期 >= 2"
    finally:
        pool.shutdown(wait=True)


def test_concurrent_many_tasks():
    """测试大量任务并发执行"""
    pool = ThreadPoolManager(max_workers=8)
    try:
        # 提交 50 个任务
        futures = [pool.submit_task(lambda x: x * x, i, priority=0) for i in range(50)]
        results = [f.result(timeout=10.0) for f in futures]

        assert len(results) == 50
        for i in range(50):
            assert results[i] == i * i, f"任务 {i} 结果不正确: {results[i]}"
    finally:
        pool.shutdown(wait=True)


# ============================================================================
# 测试: 状态获取
# ============================================================================
def test_pool_status():
    """测试状态获取"""
    pool = ThreadPoolManager(max_workers=4)
    try:
        # 初始状态
        status = pool.get_status()
        assert status["max_workers"] == 4
        assert status["running"] is True
        assert status["submitted"] == 0
        assert status["completed"] == 0
        assert status["failed"] == 0

        # 提交任务后
        fut = pool.submit_task(sum, [1, 2, 3])
        fut.result(timeout=5.0)

        # 等待状态更新
        time.sleep(0.3)

        status = pool.get_status()
        assert status["submitted"] >= 1, f"提交数不正确: {status}"
        assert status["completed"] >= 1, f"完成数不正确: {status}"
    finally:
        pool.shutdown(wait=True)


def test_pool_status_after_shutdown():
    """测试关闭后状态"""
    pool = ThreadPoolManager(max_workers=2)
    pool.submit_task(sum, [1, 2, 3]).result(timeout=5.0)
    time.sleep(0.2)
    pool.shutdown(wait=True)

    status = pool.get_status()
    assert status["running"] is False
    assert status["submitted"] >= 1
    assert status["completed"] >= 1


def test_pool_invalid_workers():
    """测试无效的工作线程数"""
    try:
        ThreadPoolManager(max_workers=0)
        assert False, "max_workers=0 应抛出 ValueError"
    except ValueError:
        pass

    try:
        ThreadPoolManager(max_workers=-1)
        assert False, "max_workers=-1 应抛出 ValueError"
    except ValueError:
        pass


# ============================================================================
# 测试: async 适配
# ============================================================================
def test_submit_async():
    """测试 async 适配方法"""
    import asyncio

    pool = ThreadPoolManager(max_workers=2)
    try:
        async def _run():
            # submit_async 返回 asyncio.Future
            aio_fut = await pool.submit_async(sum, [1, 2, 3, 4], priority=0)
            result = await asyncio.wait_for(aio_fut, timeout=5.0)
            assert result == 10, f"async 任务结果不正确: {result}"

        asyncio.run(_run())
    finally:
        pool.shutdown(wait=True)
