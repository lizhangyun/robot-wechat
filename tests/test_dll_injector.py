"""
DLL 注入器单元测试

测试范围:
  - wechat/dll_injector.py : DLL 注入器 (在 Linux 上测试降级行为和错误处理)

测试内容:
  - 异常类 PlatformNotSupportedError / InjectionError
  - find_wechat_process() 在非 Windows 环境的行为 (返回 None 或抛异常)
  - find_wechat_window() 在非 Windows 环境抛 PlatformNotSupportedError
  - DLLInjector 类的初始化
  - inject_dll() / eject_dll() 在非 Windows 环境抛 PlatformNotSupportedError
  - close_handle() / remote_free() 在非 Windows 环境的安全降级 (静默返回)

所有测试在 Linux 环境运行, 验证降级行为不崩溃。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from wechat.dll_injector import (
    DLLInjector,
    InjectionError,
    PlatformNotSupportedError,
    IS_WINDOWS,
    find_wechat_process,
    find_wechat_window,
    find_all_wechat_processes,
)


# ============================================================================
# 测试: 异常类定义
# ============================================================================
def test_platform_not_supported_error_is_runtime_error():
    """PlatformNotSupportedError 应为 RuntimeError 子类"""
    assert issubclass(PlatformNotSupportedError, RuntimeError)


def test_injection_error_is_runtime_error():
    """InjectionError 应为 RuntimeError 子类"""
    assert issubclass(InjectionError, RuntimeError)


def test_platform_not_supported_error_message():
    """PlatformNotSupportedError 可携带消息"""
    err = PlatformNotSupportedError("仅支持 Windows")
    assert "仅支持 Windows" in str(err)


def test_injection_error_message():
    """InjectionError 可携带消息"""
    err = InjectionError("注入失败")
    assert "注入失败" in str(err)


def test_two_errors_are_distinct():
    """两种异常类型互不相同"""
    assert PlatformNotSupportedError is not InjectionError


# ============================================================================
# 测试: IS_WINDOWS 平台标志
# ============================================================================
def test_is_windows_is_bool():
    """IS_WINDOWS 应为布尔值"""
    assert isinstance(IS_WINDOWS, bool)


# ============================================================================
# 测试: find_wechat_process (非 Windows 降级行为)
# ============================================================================
def test_find_wechat_process_non_windows():
    """非 Windows 环境下 find_wechat_process 返回 None 或抛 PlatformNotSupportedError。

    - 若安装了 psutil, 走 psutil 路径, 未找到微信进程返回 None;
    - 若未安装 psutil, 回退到 Toolhelp32, 调用 _require_windows 抛异常。
    """
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")

    try:
        result = find_wechat_process()
    except PlatformNotSupportedError:
        # 未安装 psutil 时预期抛此异常
        return
    # 安装了 psutil 时预期返回 None (Linux 上无微信进程)
    assert result is None, f"非 Windows 应返回 None, 实际: {result}"


def test_find_wechat_process_custom_name():
    """find_wechat_process 支持自定义进程名"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")

    try:
        result = find_wechat_process("CustomProcess.exe")
    except PlatformNotSupportedError:
        return
    assert result is None


def test_find_all_wechat_processes_non_windows():
    """非 Windows 环境下 find_all_wechat_processes 返回空列表或抛异常"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")

    try:
        result = find_all_wechat_processes()
    except PlatformNotSupportedError:
        return
    assert isinstance(result, list)
    assert result == [], f"非 Windows 应返回空列表, 实际: {result}"


# ============================================================================
# 测试: find_wechat_window (非 Windows 必抛异常)
# ============================================================================
def test_find_wechat_window_non_windows_raises():
    """非 Windows 环境下 find_wechat_window 必须抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")

    with pytest.raises(PlatformNotSupportedError):
        find_wechat_window()


def test_find_wechat_window_default_args():
    """find_wechat_window 使用默认参数在非 Windows 抛异常"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")

    with pytest.raises(PlatformNotSupportedError):
        find_wechat_window(class_name="WeChatMainWndForPC", title_keyword="微信")


# ============================================================================
# 测试: DLLInjector 初始化
# ============================================================================
def test_dll_injector_init():
    """DLLInjector 可正常实例化 (不依赖 Windows)"""
    injector = DLLInjector()
    assert injector is not None
    # 内部状态初始化
    assert injector._injected == {}
    # 静态方法绑定
    assert hasattr(DLLInjector, "find_wechat_process")
    assert hasattr(DLLInjector, "find_wechat_window")


def test_dll_injector_find_wechat_process_static():
    """DLLInjector.find_wechat_process 作为静态方法可用"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    try:
        result = DLLInjector.find_wechat_process()
    except PlatformNotSupportedError:
        return
    assert result is None


def test_dll_injector_find_wechat_window_static():
    """DLLInjector.find_wechat_window 作为静态方法可用"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    with pytest.raises(PlatformNotSupportedError):
        DLLInjector.find_wechat_window()


# ============================================================================
# 测试: inject_dll / eject_dll (非 Windows 抛异常)
# ============================================================================
def test_inject_dll_non_windows_raises():
    """非 Windows 环境下 inject_dll 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")

    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.inject_dll(1234, "/tmp/fake.dll")


def test_inject_dll_non_windows_raises_before_file_check():
    """非 Windows 环境下 inject_dll 应先抛 PlatformNotSupportedError, 而非 InjectionError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")

    injector = DLLInjector()
    # 即使文件不存在, 也应先因平台不符抛 PlatformNotSupportedError
    with pytest.raises(PlatformNotSupportedError):
        injector.inject_dll(1234, "/nonexistent/path/fake.dll")


def test_eject_dll_non_windows_raises():
    """非 Windows 环境下 eject_dll 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")

    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.eject_dll(1234, "weixin.dll")


def test_open_process_non_windows_raises():
    """非 Windows 环境下 open_process 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.open_process(1234)


def test_remote_alloc_non_windows_raises():
    """非 Windows 环境下 remote_alloc 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.remote_alloc(0, 1024)


def test_remote_write_non_windows_raises():
    """非 Windows 环境下 remote_write 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.remote_write(0, 0, b"data")


def test_remote_read_non_windows_raises():
    """非 Windows 环境下 remote_read 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.remote_read(0, 0, 10)


def test_find_remote_module_non_windows_raises():
    """非 Windows 环境下 find_remote_module 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.find_remote_module(1234, "WeChatWin.dll")


def test_get_remote_module_base_non_windows_raises():
    """非 Windows 环境下 get_remote_module_base 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.get_remote_module_base(1234, "WeChatWin.dll")


def test_call_remote_function_non_windows_raises():
    """非 Windows 环境下 call_remote_function 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.call_remote_function(1234, 0x1000, "init", args=None)


def test_call_remote_thread_non_windows_raises():
    """非 Windows 环境下 call_remote_thread 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.call_remote_thread(1234, 0x1000, None)


def test_memory_patch_non_windows_raises():
    """非 Windows 环境下 memory_patch 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.memory_patch(1234, 0x1000, b"\x90\x90")


def test_memory_restore_non_windows_raises():
    """非 Windows 环境下 memory_restore 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    with pytest.raises(PlatformNotSupportedError):
        injector.memory_restore(1234, 0x1000, b"\x90\x90")


# ============================================================================
# 测试: 非 Windows 安全降级方法 (静默返回, 不抛异常)
# ============================================================================
def test_close_handle_non_windows_no_raise():
    """非 Windows 环境下 close_handle 静默返回 (不抛异常)"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    # 不应抛异常
    injector.close_handle(0)
    injector.close_handle(12345)


def test_remote_free_non_windows_no_raise():
    """非 Windows 环境下 remote_free 静默返回 (不抛异常)"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    injector = DLLInjector()
    injector.remote_free(0, 0x1000, 0)
    injector.remote_free(12345, 0x2000, 100)


# ============================================================================
# 测试: 注入信息缓存机制 (不依赖平台)
# ============================================================================
def test_injected_cache_initially_empty():
    """DLLInjector 的注入缓存初始为空"""
    injector = DLLInjector()
    assert injector._injected == {}
    assert len(injector._injected) == 0


def test_find_dll_path_empty_cache():
    """_find_dll_path 在空缓存时返回 None"""
    injector = DLLInjector()
    assert injector._find_dll_path(1234, 0x1000) is None
