#!/usr/bin/env python3
"""
统一测试运行器

不依赖 pytest, 自己实现简单的测试运行器:
  - 遍历所有 test_*.py 文件, 执行所有 test_ 开头的函数
  - 统计通过 / 失败 / 跳过数量
  - 输出详细错误信息 (含完整 traceback)
  - 支持命令行参数: --verbose, --module <name>
  - 最后输出汇总报告

用法:
  python tests/run_all.py                    # 运行所有测试 (简洁模式)
  python tests/run_all.py --verbose          # 运行所有测试 (详细模式)
  python tests/run_all.py --module database  # 只运行 test_database 模块
  python tests/run_all.py --module database,security  # 运行多个模块
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 抑制 loguru 日志输出 (减少测试运行时的噪音)
try:
    from loguru import logger as _loguru_logger
    _loguru_logger.remove()
    _loguru_logger.add(sys.stderr, level="WARNING")
except ImportError:
    pass


# ============================================================================
# 数据结构
# ============================================================================
@dataclass
class TestResult:
    """单个测试结果"""
    module_name: str
    test_name: str
    passed: bool
    skipped: bool = False
    error: Optional[str] = None
    traceback_str: Optional[str] = None
    duration: float = 0.0


@dataclass
class TestSummary:
    """测试汇总"""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0  # 模块导入错误等
    results: list[TestResult] = field(default_factory=list)
    total_duration: float = 0.0


# ============================================================================
# 颜色输出 (终端支持时)
# ============================================================================
class Colors:
    """终端颜色"""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @classmethod
    def enabled(cls) -> bool:
        """判断是否启用颜色"""
        return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _color(text: str, color: str) -> str:
    """添加颜色"""
    if Colors.enabled():
        return f"{color}{text}{Colors.RESET}"
    return text


# ============================================================================
# 测试发现与执行
# ============================================================================
def discover_test_modules(tests_dir: Path, module_filter: Optional[list[str]] = None) -> list[str]:
    """
    发现所有 test_*.py 测试模块

    Args:
        tests_dir: 测试目录
        module_filter: 模块名过滤列表 (不含 test_ 前缀和 .py 后缀)

    Returns:
        模块名列表 (如 ["test_database", "test_config"])
    """
    modules = []
    for py_file in sorted(tests_dir.glob("test_*.py")):
        module_name = py_file.stem  # 如 test_database
        if module_filter:
            # 检查是否在过滤列表中 (支持 test_database 或 database 两种写法)
            short_name = module_name.replace("test_", "")
            if module_name not in module_filter and short_name not in module_filter:
                continue
        modules.append(module_name)
    return modules


def discover_test_functions(module) -> list[str]:
    """
    发现模块中所有 test_ 开头的函数

    Args:
        module: 已导入的模块对象

    Returns:
        函数名列表 (按定义顺序)
    """
    test_funcs = []
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("test_"):
            test_funcs.append(name)
    return test_funcs


def run_test_function(module_name: str, test_name: str, func) -> TestResult:
    """
    执行单个测试函数

    支持同步和异步 (async) 测试函数。

    Args:
        module_name: 模块名
        test_name: 测试函数名
        func: 测试函数对象

    Returns:
        测试结果
    """
    start_time = time.monotonic()
    result = TestResult(module_name=module_name, test_name=test_name, passed=False)

    try:
        if asyncio.iscoroutinefunction(func):
            # 异步测试函数
            asyncio.run(func())
        else:
            # 同步测试函数
            func()
        result.passed = True
    except AssertionError as e:
        result.passed = False
        result.error = str(e) or "断言失败"
        result.traceback_str = traceback.format_exc()
    except Exception as e:
        result.passed = False
        result.error = f"{type(e).__name__}: {e}"
        result.traceback_str = traceback.format_exc()
    finally:
        result.duration = time.monotonic() - start_time

    return result


def run_module_tests(module_name: str, verbose: bool) -> list[TestResult]:
    """
    运行单个模块的所有测试

    Args:
        module_name: 模块名 (如 test_database)
        verbose: 是否输出详细信息

    Returns:
        测试结果列表
    """
    results = []

    # 导入模块
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        # 模块导入失败
        error_tb = traceback.format_exc()
        if verbose:
            print(_color(f"  [导入错误] {module_name}: {e}", Colors.RED))
            print(error_tb)
        results.append(TestResult(
            module_name=module_name,
            test_name="<module_import>",
            passed=False,
            error=f"模块导入失败: {type(e).__name__}: {e}",
            traceback_str=error_tb,
        ))
        return results

    # 发现测试函数
    test_names = discover_test_functions(module)

    if not test_names:
        if verbose:
            print(_color(f"  [跳过] {module_name}: 无测试函数", Colors.YELLOW))
        return results

    if verbose:
        print(f"\n{_color(f'[{module_name}]', Colors.BOLD)} ({len(test_names)} 个测试)")

    # 执行每个测试
    for test_name in test_names:
        func = getattr(module, test_name)
        result = run_test_function(module_name, test_name, func)

        if verbose:
            status_str = _color("通过", Colors.GREEN) if result.passed else _color("失败", Colors.RED)
            duration_str = f"{result.duration:.3f}s"
            print(f"  {status_str} {test_name} ({duration_str})")
            if not result.passed and result.error:
                print(f"       {_color('错误:', Colors.RED)} {result.error}")
                if result.traceback_str:
                    # 只打印最后几行 traceback
                    tb_lines = result.traceback_str.strip().split("\n")
                    for line in tb_lines:
                        print(f"       {line}")

        results.append(result)

    return results


# ============================================================================
# 主函数
# ============================================================================
def main():
    """主入口"""
    parser = argparse.ArgumentParser(
        description="robot3-replica 统一测试运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python tests/run_all.py                    运行所有测试 (简洁模式)
  python tests/run_all.py --verbose          运行所有测试 (详细模式)
  python tests/run_all.py --module database  只运行 test_database 模块
  python tests/run_all.py --module database,security  运行多个模块
        """,
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="输出详细信息 (含每个测试的执行结果和错误堆栈)",
    )
    parser.add_argument(
        "--module", "-m",
        type=str,
        default=None,
        help="只运行指定模块 (逗号分隔, 如 database,security)",
    )
    args = parser.parse_args()

    # 设置测试目录
    tests_dir = Path(__file__).resolve().parent
    project_root = tests_dir.parent

    # 将项目根目录和测试目录加入 sys.path
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))

    # 解析模块过滤
    module_filter = None
    if args.module:
        module_filter = [m.strip() for m in args.module.split(",")]

    # 发现测试模块
    modules = discover_test_modules(tests_dir, module_filter)

    if not modules:
        print(_color("未找到匹配的测试模块", Colors.YELLOW))
        return 1

    # 打印标题
    print()
    print(_color("=" * 60, Colors.BOLD))
    print(_color(f"  robot3-replica 测试运行器", Colors.BOLD))
    print(f"  项目根目录: {project_root}")
    print(f"  测试模块数: {len(modules)}")
    if module_filter:
        print(f"  模块过滤: {', '.join(module_filter)}")
    print(_color("=" * 60, Colors.BOLD))
    print(f"  模式: {_color('详细', Colors.CYAN) if args.verbose else _color('简洁', Colors.CYAN)}")
    print(_color("=" * 60, Colors.BOLD))

    # 执行所有测试
    summary = TestSummary()
    overall_start = time.monotonic()

    for module_name in modules:
        results = run_module_tests(module_name, args.verbose)
        summary.results.extend(results)

    summary.total_duration = time.monotonic() - overall_start

    # 统计
    for r in summary.results:
        summary.total += 1
        if r.passed:
            summary.passed += 1
        elif r.skipped:
            summary.skipped += 1
        elif r.test_name == "<module_import>":
            summary.errors += 1
        else:
            summary.failed += 1

    # 简洁模式下输出每个测试的结果
    if not args.verbose:
        print()
        for r in summary.results:
            if r.passed:
                symbol = _color(".", Colors.GREEN)
            elif r.skipped:
                symbol = _color("S", Colors.YELLOW)
            elif r.test_name == "<module_import>":
                symbol = _color("E", Colors.RED)
            else:
                symbol = _color("F", Colors.RED)
            print(symbol, end="", flush=True)
        print()

    # 输出失败详情 (简洁模式下也需要)
    if not args.verbose and summary.failed > 0:
        print()
        print(_color("失败详情:", Colors.RED))
        for r in summary.results:
            if not r.passed and r.test_name != "<module_import>":
                print(f"\n  {_color('FAIL', Colors.RED)} {r.module_name}::{r.test_name}")
                if r.error:
                    print(f"       {r.error}")
                if r.traceback_str:
                    for line in r.traceback_str.strip().split("\n")[-5:]:
                        print(f"       {line}")

    # 输出导入错误
    if summary.errors > 0:
        print()
        print(_color("导入错误:", Colors.RED))
        for r in summary.results:
            if r.test_name == "<module_import>":
                print(f"\n  {_color('ERROR', Colors.RED)} {r.module_name}")
                if r.error:
                    print(f"       {r.error}")
                if r.traceback_str:
                    for line in r.traceback_str.strip().split("\n")[-3:]:
                        print(f"       {line}")

    # 汇总报告
    print()
    print(_color("=" * 60, Colors.BOLD))
    print(_color("  测试汇总报告", Colors.BOLD))
    print(_color("=" * 60, Colors.BOLD))

    # 按模块分组统计
    module_stats = {}
    for r in summary.results:
        if r.module_name not in module_stats:
            module_stats[r.module_name] = {"passed": 0, "failed": 0, "errors": 0, "total": 0}
        module_stats[r.module_name]["total"] += 1
        if r.passed:
            module_stats[r.module_name]["passed"] += 1
        elif r.test_name == "<module_import>":
            module_stats[r.module_name]["errors"] += 1
        else:
            module_stats[r.module_name]["failed"] += 1

    for mod_name, stats in module_stats.items():
        status = _color("全部通过", Colors.GREEN) if stats["failed"] == 0 and stats["errors"] == 0 \
            else _color("有失败", Colors.RED)
        print(f"  {mod_name:.<40s} {stats['passed']}/{stats['total']} {status}")

    print(_color("-" * 60, Colors.BOLD))
    pass_color = Colors.GREEN if summary.failed == 0 and summary.errors == 0 else Colors.RED
    print(f"  总计:     {summary.total}")
    print(f"  {_color('通过:', Colors.GREEN)}     {summary.passed}")
    print(f"  {_color('失败:', Colors.RED)}     {summary.failed}")
    if summary.errors > 0:
        print(f"  {_color('错误:', Colors.RED)}     {summary.errors}")
    print(f"  耗时:     {summary.total_duration:.2f}s")

    # 最终状态
    print()
    if summary.failed == 0 and summary.errors == 0:
        print(_color("  *** 所有测试通过 ***", Colors.GREEN))
        return 0
    else:
        print(_color(f"  *** {summary.failed + summary.errors} 个测试失败 ***", Colors.RED))
        return 1


if __name__ == "__main__":
    sys.exit(main())
