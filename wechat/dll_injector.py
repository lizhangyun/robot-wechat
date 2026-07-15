"""
DLL 注入器 - 通过 CreateRemoteThread + LoadLibrary 实现远程 DLL 注入

将 weixin.dll 注入到微信进程，实现微信自动化。仅在 Windows 平台可用；
非 Windows 平台 ``import`` 不会报错，但调用注入相关方法时会抛出
:class:`PlatformNotSupportedError`。

技术原理
========

注入流程（对应原软件 ``weixin.dll`` 的加载机制）::

    1. OpenProcess          打开目标微信进程，获取进程句柄
    2. VirtualAllocEx       在目标进程地址空间分配一块内存
    3. WriteProcessMemory   写入 DLL 绝对路径字符串（UTF-16）
    4. GetProcAddress       获取 kernel32!LoadLibraryW 的地址
    5. CreateRemoteThread   在目标进程创建远程线程，
                            线程函数 = LoadLibraryW，参数 = DLL 路径指针
    6. WaitForSingleObject  等待远程线程结束（即 LoadLibraryW 返回）
    7. GetExitCodeThread    取线程退出码 = LoadLibraryW 返回值 = DLL 模块句柄

卸载流程类似，把 LoadLibraryW 换成 FreeLibrary 即可。

进程查找
========

- 优先使用 ``psutil``（若已安装）按进程名查找微信 PID；
- 无 ``psutil`` 时回退到 Windows ``CreateToolhelp32Snapshot`` 进程快照枚举。

窗口查找
========

- 使用 ``user32.FindWindowW`` 按窗口类名 "WeChatMainWndForPC" 查找微信主窗口；
- 找不到时通过 ``EnumWindows`` 遍历顶层窗口，按标题包含 "微信" 匹配。

调用注入 DLL 中的导出函数
========================

由于 ``CreateRemoteThread`` 仅支持传递单个 ``LPVOID`` 参数，对于多参数的
导出函数（如 ``api(cmd_id, json_data)``），需要：
1. 将多个参数打包到一个结构体中；
2. 在目标进程分配内存写入结构体（含字符串数据）；
3. 以结构体指针作为单一线程参数调用导出函数。

本模块的 :meth:`DLLInjector.call_remote_function` 即按此方式实现，
支持传入单个整型指针参数或一段 ``bytes`` 载荷（自动分配并写入远端内存，
返回远端地址作为线程参数）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from loguru import logger

from wechat.wechat_offsets import WECHAT_EXE, WECHAT_VERSION

# psutil 为可选依赖，缺失时回退到 Windows API 枚举进程
try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


# ====================================================================== #
#  平台检测
# ====================================================================== #
IS_WINDOWS: bool = sys.platform == "win32"
"""是否运行在 Windows 平台。"""


class PlatformNotSupportedError(RuntimeError):
    """当前平台不支持 DLL 注入（仅 Windows 可用）。"""


class InjectionError(RuntimeError):
    """DLL 注入/卸载过程中发生的错误。"""


def _require_windows() -> None:
    """检查运行平台，非 Windows 抛出明确异常。

    Raises:
        PlatformNotSupportedError: 当前非 Windows 平台。
    """
    if not IS_WINDOWS:
        raise PlatformNotSupportedError(
            "DLL 注入仅支持 Windows 平台，当前平台: "
            f"{sys.platform}。请在 Windows 环境运行，"
            "或使用模拟模式(WeChatClient(mock=True))。"
        )


# ====================================================================== #
#  Windows API 常量
# ====================================================================== #
if IS_WINDOWS:
    # --- 进程访问权限 ---
    PROCESS_CREATE_THREAD = 0x0002
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_OPERATION = 0x0008
    PROCESS_VM_READ = 0x0010
    PROCESS_VM_WRITE = 0x0020
    PROCESS_ALL_ACCESS = (
        PROCESS_CREATE_THREAD
        | PROCESS_QUERY_INFORMATION
        | PROCESS_VM_OPERATION
        | PROCESS_VM_READ
        | PROCESS_VM_WRITE
    )

    # --- 内存分配 ---
    MEM_COMMIT = 0x00001000
    MEM_RESERVE = 0x00002000
    MEM_RELEASE = 0x8000
    PAGE_READWRITE = 0x04
    PAGE_EXECUTE_READWRITE = 0x40

    # --- 等待 ---
    INFINITE = 0xFFFFFFFF
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    STILL_ACTIVE = 259

    # --- 窗口消息 ---
    WM_COPYDATA = 0x004A

    # --- Toolhelp32 ---
    TH32CS_SNAPPROCESS = 0x00000002
    TH32CS_SNAPMODULE = 0x00000008
    TH32CS_SNAPMODULE32 = 0x00000010

    # --- 窗口查找 ---
    HWND_MESSAGE = wintypes.HWND(-3)  # 消息专用窗口父句柄
else:
    # 非 Windows 平台提供占位常量，避免 import 阶段 NameError
    PROCESS_ALL_ACCESS = 0
    PROCESS_QUERY_LIMITED_INFORMATION = 0
    MEM_COMMIT = 0
    MEM_RESERVE = 0
    MEM_RELEASE = 0
    PAGE_READWRITE = 0
    PAGE_EXECUTE_READWRITE = 0
    INFINITE = 0
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0
    STILL_ACTIVE = 0
    WM_COPYDATA = 0
    TH32CS_SNAPPROCESS = 0
    TH32CS_SNAPMODULE = 0
    TH32CS_SNAPMODULE32 = 0
    HWND_MESSAGE = 0  # type: ignore[assignment]


# ====================================================================== #
#  Windows 结构体定义
# ====================================================================== #
# 这些 ctypes.Structure 仅依赖 ctypes.wintypes 中的类型别名，在所有平台
# 均可定义（ctypes.wintypes 是纯 Python 模块）；实际使用需在 Windows 上。
class PROCESSENTRY32W(ctypes.Structure):
    """进程快照条目（Process32FirstW/NextW 使用）。"""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    """模块快照条目（Module32FirstW/NextW 使用）。"""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


class COPYDATASTRUCT(ctypes.Structure):
    """WM_COPYDATA 消息的载荷结构。"""

    _fields_ = [
        ("dwData", ctypes.c_size_t),   # 自定义类型标识
        ("cbData", wintypes.DWORD),    # 数据长度
        ("lpData", ctypes.c_void_p),   # 数据指针
    ]



# ====================================================================== #
#  Windows API ctypes 绑定
# ====================================================================== #
# 仅在 Windows 下加载并设置函数签名；非 Windows 置 None，调用时由
# _require_windows() 兜底报错。
kernel32: Optional[Any] = None
user32: Optional[Any] = None

if IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]

    # --- 进程/线程/内存 ---
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    kernel32.VirtualAllocEx.restype = wintypes.LPVOID
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD
    ]

    kernel32.VirtualFreeEx.restype = wintypes.BOOL
    kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD
    ]

    kernel32.VirtualProtectEx.restype = wintypes.BOOL
    kernel32.VirtualProtectEx.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.WriteProcessMemory.restype = wintypes.BOOL
    kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]

    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]

    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.SECURITY_ATTRIBUTES),
        ctypes.c_size_t, wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    kernel32.GetExitCodeThread.restype = wintypes.BOOL
    kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]

    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.GetLastError.argtypes = []

    # --- Toolhelp32 进程/模块枚举 ---
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]

    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]

    kernel32.Module32NextW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]

    # --- 窗口 ---
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]

    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [wintypes.WNDENUMPROC, wintypes.LPARAM]

    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]

    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]

    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]

    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]


# ====================================================================== #
#  进程查找
# ====================================================================== #
def find_wechat_process(process_name: str = WECHAT_EXE) -> Optional[int]:
    """查找微信进程 PID。

    优先使用 psutil（跨平台、更稳定），缺失时回退到 Windows
    ``CreateToolhelp32Snapshot`` 进程快照枚举。

    Args:
        process_name: 进程名（默认 ``WeChat.exe``），大小写不敏感。

    Returns:
        微信进程 PID；未找到返回 None。

    Raises:
        PlatformNotSupportedError: 非 Windows 且未安装 psutil 时。
    """
    name_lower = process_name.lower()

    # 优先 psutil
    if psutil is not None:
        try:
            for proc in psutil.process_iter(["name", "pid"]):
                pname = (proc.info.get("name") or "").lower()
                if name_lower in pname:
                    pid = proc.info.get("pid")
                    if pid:
                        logger.debug(f"通过 psutil 找到微信进程: pid={pid} name={pname}")
                        return int(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug(f"psutil 进程枚举异常: {e}")
        # psutil 可用但未找到，直接返回（不回退到 Windows API，避免重复）
        return None

    # 无 psutil，回退 Windows Toolhelp32
    _require_windows()
    assert kernel32 is not None
    try:
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot:
            logger.error("CreateToolhelp32Snapshot 失败")
            return None
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return None
            while True:
                exe_name = entry.szExeFile.lower()
                if name_lower in exe_name:
                    logger.debug(
                        f"通过 Toolhelp32 找到微信进程: pid={entry.th32ProcessID} "
                        f"name={exe_name}"
                    )
                    return int(entry.th32ProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Toolhelp32 进程枚举异常: {e}")
    return None


def find_all_wechat_processes(process_name: str = WECHAT_EXE) -> list[int]:
    """查找所有匹配的微信进程 PID（多开场景）。

    Args:
        process_name: 进程名关键字。

    Returns:
        PID 列表（可能为空）。
    """
    name_lower = process_name.lower()
    pids: list[int] = []
    if psutil is not None:
        try:
            for proc in psutil.process_iter(["name", "pid"]):
                pname = (proc.info.get("name") or "").lower()
                if name_lower in pname and proc.info.get("pid"):
                    pids.append(int(proc.info["pid"]))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"psutil 枚举异常: {e}")
        return pids

    _require_windows()
    assert kernel32 is not None
    try:
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot:
            return pids
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return pids
            while True:
                if name_lower in entry.szExeFile.lower():
                    pids.append(int(entry.th32ProcessID))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Toolhelp32 枚举异常: {e}")
    return pids


# ====================================================================== #
#  窗口查找
# ====================================================================== #
# 微信主窗口类名（3.9.x 版本）
WECHAT_WINDOW_CLASS: str = "WeChatMainWndForPC"
# 微信登录窗口类名
WECHAT_LOGIN_WINDOW_CLASS: str = "WeChatLoginWndForPC"


def find_wechat_window(
    class_name: str = WECHAT_WINDOW_CLASS,
    title_keyword: str = "微信",
) -> Optional[int]:
    """查找微信主窗口句柄（HWND）。

    优先按窗口类名 ``WeChatMainWndForPC`` 查找（最准确）；
    找不到时通过 ``EnumWindows`` 遍历所有顶层窗口，按标题包含
    ``title_keyword`` 匹配。

    Args:
        class_name: 微信窗口类名。
        title_keyword: 标题关键字（回退匹配用）。

    Returns:
        窗口句柄（int）；未找到返回 None。

    Raises:
        PlatformNotSupportedError: 非 Windows 平台。
    """
    _require_windows()
    assert user32 is not None
    try:
        # 1. 按类名精确查找
        hwnd = user32.FindWindowW(class_name, None)
        if hwnd:
            logger.debug(f"按类名找到微信窗口: hwnd={hwnd} class={class_name}")
            return int(hwnd)

        # 2. 回退：枚举顶层窗口按标题匹配
        found_hwnd: list[int] = []

        def _enum_proc(hwnd: int, lparam: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            if title_keyword and title_keyword in title:
                # 仅取可见窗口
                if user32.IsWindowVisible(hwnd):
                    found_hwnd.append(int(hwnd))
                    return False  # 找到即停止枚举
            return True

        # WNDENUMPROC 必须保持引用，避免被 GC
        proc = wintypes.WNDENUMPROC(_enum_proc)
        user32.EnumWindows(proc, 0)
        if found_hwnd:
            logger.debug(f"按标题找到微信窗口: hwnd={found_hwnd[0]} title~={title_keyword}")
            return found_hwnd[0]
    except Exception as e:  # noqa: BLE001
        logger.exception(f"查找微信窗口异常: {e}")
    return None


def get_window_pid(hwnd: int) -> Optional[int]:
    """根据窗口句柄获取所属进程 PID。

    Args:
        hwnd: 窗口句柄。

    Returns:
        PID；失败返回 None。
    """
    _require_windows()
    assert user32 is not None
    try:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            return int(pid.value)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"获取窗口 PID 异常: {e}")
    return None


# ====================================================================== #
#  DLL 注入器
# ====================================================================== #
class DLLInjector:
    """DLL 注入器：负责将 Hook DLL 注入/卸载到微信进程，并调用其中导出函数。

    典型用法::

        injector = DLLInjector()
        pid = injector.find_wechat_process()
        if pid:
            handle = injector.inject_dll(pid, r"C:\\path\\to\\weixin.dll")
            injector.call_remote_function(pid, handle, "init", args=(hwnd,))
            ...
            injector.eject_dll(pid, "weixin.dll")

    所有方法仅在 Windows 平台可用，非 Windows 调用会抛
    :class:`PlatformNotSupportedError`。
    """

    # 便于外部静态调用（兼容模块级函数风格）
    find_wechat_process = staticmethod(find_wechat_process)
    find_wechat_window = staticmethod(find_wechat_window)

    def __init__(self) -> None:
        # 已注入 DLL 信息缓存：{(pid, dll_name): {"handle": int, "path": str}}
        self._injected: dict[tuple[int, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  进程打开
    # ------------------------------------------------------------------ #
    def open_process(self, pid: int, access: int = PROCESS_ALL_ACCESS) -> int:
        """打开目标进程并返回句柄。

        Args:
            pid: 目标进程 PID。
            access: 访问权限标志位。

        Returns:
            进程句柄。

        Raises:
            InjectionError: 打开失败。
        """
        _require_windows()
        assert kernel32 is not None
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            err = kernel32.GetLastError()
            raise InjectionError(
                f"OpenProcess 失败 pid={pid} error={err}（可能权限不足，"
                "请以管理员身份运行）"
            )
        return int(handle)

    def close_handle(self, handle: int) -> None:
        """关闭句柄。"""
        if not IS_WINDOWS:
            return
        assert kernel32 is not None
        if handle:
            kernel32.CloseHandle(handle)

    # ------------------------------------------------------------------ #
    #  远程内存读写
    # ------------------------------------------------------------------ #
    def remote_alloc(
        self, process_handle: int, size: int,
        protection: int = PAGE_READWRITE,
    ) -> int:
        """在目标进程分配内存。

        Args:
            process_handle: 进程句柄。
            size: 分配字节数。
            protection: 内存保护属性。

        Returns:
            远端内存地址。

        Raises:
            InjectionError: 分配失败。
        """
        _require_windows()
        assert kernel32 is not None
        addr = kernel32.VirtualAllocEx(
            process_handle, None, size, MEM_COMMIT | MEM_RESERVE, protection
        )
        if not addr:
            err = kernel32.GetLastError()
            raise InjectionError(f"VirtualAllocEx 失败 size={size} error={err}")
        return int(addr)

    def remote_free(self, process_handle: int, address: int, size: int = 0) -> None:
        """释放目标进程内存。

        Args:
            process_handle: 进程句柄。
            address: 远端内存地址。
            size: 释放大小（MEM_RELEASE 时应为 0）。
        """
        if not IS_WINDOWS:
            return
        assert kernel32 is not None
        kernel32.VirtualFreeEx(process_handle, address, size, MEM_RELEASE)

    def remote_write(self, process_handle: int, address: int, data: bytes) -> int:
        """向目标进程内存写入数据。

        Args:
            process_handle: 进程句柄。
            address: 远端写入地址。
            data: 待写入字节串。

        Returns:
            实际写入字节数。

        Raises:
            InjectionError: 写入失败。
        """
        _require_windows()
        assert kernel32 is not None
        written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            process_handle, address, data, len(data), ctypes.byref(written)
        )
        if not ok:
            err = kernel32.GetLastError()
            raise InjectionError(f"WriteProcessMemory 失败 error={err}")
        return int(written.value)

    def remote_read(self, process_handle: int, address: int, size: int) -> bytes:
        """从目标进程内存读取数据。

        Args:
            process_handle: 进程句柄。
            address: 远端读取地址。
            size: 读取字节数。

        Returns:
            读取到的字节串。

        Raises:
            InjectionError: 读取失败。
        """
        _require_windows()
        assert kernel32 is not None
        buf = (ctypes.c_byte * size)()
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            process_handle, address, buf, size, ctypes.byref(read)
        )
        if not ok:
            err = kernel32.GetLastError()
            raise InjectionError(f"ReadProcessMemory 失败 error={err}")
        return bytes(buf[: read.value])

    # ------------------------------------------------------------------ #
    #  DLL 注入 / 卸载
    # ------------------------------------------------------------------ #
    def inject_dll(self, pid: int, dll_path: str) -> int:
        """将 DLL 注入目标进程。

        通过 ``CreateRemoteThread + LoadLibraryW`` 实现：

        1. 打开进程；
        2. 分配内存并写入 DLL 绝对路径（UTF-16）；
        3. 获取 ``LoadLibraryW`` 地址；
        4. 创建远程线程执行 ``LoadLibraryW(dll_path)``；
        5. 等待线程结束，取退出码作为 DLL 模块句柄。

        Args:
            pid: 目标进程 PID。
            dll_path: DLL 绝对路径（建议绝对路径，避免远端工作目录不一致）。

        Returns:
            DLL 在目标进程中的模块句柄（HMODULE）。

        Raises:
            InjectionError: 注入失败。
            PlatformNotSupportedError: 非 Windows 平台。
        """
        _require_windows()
        assert kernel32 is not None

        dll_path = os.path.abspath(dll_path)
        if not os.path.isfile(dll_path):
            raise InjectionError(f"DLL 文件不存在: {dll_path}")
        logger.info(f"开始注入 DLL: pid={pid} dll={dll_path}")

        process_handle = 0
        remote_path_addr = 0
        thread_handle = 0
        try:
            process_handle = self.open_process(pid)
            # 写入 DLL 路径（宽字符）
            path_bytes = (dll_path + "\0").encode("utf-16-le")
            remote_path_addr = self.remote_alloc(
                process_handle, len(path_bytes)
            )
            self.remote_write(process_handle, remote_path_addr, path_bytes)

            # 获取 LoadLibraryW 地址
            # 在 64 位 Windows 上，kernel32 在所有进程加载基址一致（ASLR 仅每次启动变），
            # 因此本进程 LoadLibraryW 地址可直接用于远端线程。
            k32 = kernel32.GetModuleHandleW("kernel32.dll")
            load_library_addr = kernel32.GetProcAddress(k32, b"LoadLibraryW")
            if not load_library_addr:
                raise InjectionError("无法获取 LoadLibraryW 地址")

            logger.debug(
                f"LoadLibraryW 地址=0x{load_library_addr:X} "
                f"远端路径地址=0x{remote_path_addr:X}"
            )

            # 创建远程线程
            thread_handle = kernel32.CreateRemoteThread(
                process_handle, None, 0, load_library_addr,
                remote_path_addr, 0, None
            )
            if not thread_handle:
                err = kernel32.GetLastError()
                raise InjectionError(f"CreateRemoteThread 失败 error={err}")

            # 等待线程结束
            wait = kernel32.WaitForSingleObject(thread_handle, 30000)
            if wait == WAIT_TIMEOUT:
                raise InjectionError("远程线程执行超时（30s）")

            # 取退出码 = LoadLibraryW 返回值 = HMODULE
            exit_code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeThread(thread_handle, ctypes.byref(exit_code)):
                raise InjectionError("GetExitCodeThread 失败")
            module_handle = int(exit_code.value)
            if not module_handle:
                raise InjectionError(
                    "LoadLibraryW 返回 0，DLL 加载失败"
                    "（可能位数不匹配/依赖缺失/被微信拒绝）"
                )

            dll_name = os.path.basename(dll_path).lower()
            with self._lock:
                self._injected[(pid, dll_name)] = {
                    "handle": module_handle,
                    "path": dll_path,
                }
            logger.info(
                f"DLL 注入成功: pid={pid} dll={dll_name} hModule=0x{module_handle:X}"
            )
            return module_handle
        finally:
            if thread_handle:
                kernel32.CloseHandle(thread_handle)
            if remote_path_addr and process_handle:
                self.remote_free(process_handle, remote_path_addr)
            if process_handle:
                self.close_handle(process_handle)

    def eject_dll(self, pid: int, dll_name: str) -> bool:
        """从目标进程卸载已注入的 DLL。

        通过 ``CreateRemoteThread + FreeLibrary`` 实现。会先尝试从缓存
        取模块句柄，失败则枚举目标进程模块查找。

        Args:
            pid: 目标进程 PID。
            dll_name: DLL 文件名（不含路径，大小写不敏感）。

        Returns:
            卸载成功返回 True。

        Raises:
            InjectionError: 卸载失败。
            PlatformNotSupportedError: 非 Windows 平台。
        """
        _require_windows()
        assert kernel32 is not None

        dll_name = os.path.basename(dll_name).lower()
        logger.info(f"开始卸载 DLL: pid={pid} dll={dll_name}")

        # 1. 优先用缓存句柄
        cached = self._injected.get((pid, dll_name))
        module_handle = cached["handle"] if cached else 0
        # 2. 缓存无则枚举模块查找
        if not module_handle:
            module_handle = self.find_remote_module(pid, dll_name) or 0
        if not module_handle:
            logger.warning(f"未找到已注入的 DLL: {dll_name}，可能已卸载")
            return True

        process_handle = 0
        thread_handle = 0
        try:
            process_handle = self.open_process(pid)
            k32 = kernel32.GetModuleHandleW("kernel32.dll")
            free_library_addr = kernel32.GetProcAddress(k32, b"FreeLibrary")
            if not free_library_addr:
                raise InjectionError("无法获取 FreeLibrary 地址")

            thread_handle = kernel32.CreateRemoteThread(
                process_handle, None, 0, free_library_addr,
                module_handle, 0, None
            )
            if not thread_handle:
                err = kernel32.GetLastError()
                raise InjectionError(f"CreateRemoteThread 失败 error={err}")

            wait = kernel32.WaitForSingleObject(thread_handle, 30000)
            if wait == WAIT_TIMEOUT:
                raise InjectionError("FreeLibrary 远程线程超时")

            exit_code = wintypes.DWORD(0)
            kernel32.GetExitCodeThread(thread_handle, ctypes.byref(exit_code))
            success = bool(exit_code.value)

            with self._lock:
                self._injected.pop((pid, dll_name), None)
            if success:
                logger.info(f"DLL 卸载成功: pid={pid} dll={dll_name}")
            else:
                logger.warning(f"DLL 卸载返回 0: pid={pid} dll={dll_name}")
            return success
        finally:
            if thread_handle:
                kernel32.CloseHandle(thread_handle)
            if process_handle:
                self.close_handle(process_handle)

    # ------------------------------------------------------------------ #
    #  模块查找
    # ------------------------------------------------------------------ #
    def find_remote_module(self, pid: int, dll_name: str) -> Optional[int]:
        """在目标进程中查找已加载模块的句柄。

        使用 ``CreateToolhelp32Snapshot(TH32CS_SNAPMODULE)`` 枚举模块。

        Args:
            pid: 目标进程 PID。
            dll_name: DLL 文件名（大小写不敏感）。

        Returns:
            模块句柄；未找到返回 None。
        """
        _require_windows()
        assert kernel32 is not None
        dll_name = os.path.basename(dll_name).lower()
        try:
            snapshot = kernel32.CreateToolhelp32Snapshot(
                TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
            )
            if not snapshot:
                return None
            try:
                entry = MODULEENTRY32W()
                entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
                if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
                    return None
                while True:
                    if dll_name == entry.szModule.lower():
                        return int(entry.hModule or 0) or None
                    if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                        break
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"枚举远端模块异常: {e}")
        return None

    def get_remote_module_base(self, pid: int, dll_name: str) -> Optional[int]:
        """获取目标进程中指定模块的基址。

        Args:
            pid: 目标进程 PID。
            dll_name: DLL 文件名。

        Returns:
            模块基址；未找到返回 None。
        """
        _require_windows()
        assert kernel32 is not None
        dll_name = os.path.basename(dll_name).lower()
        try:
            snapshot = kernel32.CreateToolhelp32Snapshot(
                TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
            )
            if not snapshot:
                return None
            try:
                entry = MODULEENTRY32W()
                entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
                if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
                    return None
                while True:
                    if dll_name == entry.szModule.lower():
                        # modBaseAddr 是指针，取其整数值
                        addr = ctypes.cast(
                            entry.modBaseAddr, ctypes.c_void_p
                        ).value
                        return int(addr) if addr else None
                    if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                        break
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"获取模块基址异常: {e}")
        return None

    # ------------------------------------------------------------------ #
    #  远程函数调用
    # ------------------------------------------------------------------ #
    def call_remote_function(
        self,
        pid: int,
        dll_handle: int,
        func_name: str,
        args: Optional[Union[int, bytes, Sequence[Any]]] = None,
        timeout_ms: int = 30000,
    ) -> int:
        """调用注入 DLL 中的导出函数。

        由于 ``CreateRemoteThread`` 仅支持单个 ``LPVOID`` 参数，本方法支持
        三种 ``args`` 形式：

        - ``None``：无参调用（如 ``uninstall()``）；
        - ``int``：直接作为单指针参数（如 ``init(hwnd)``，hwnd 即句柄整数）；
        - ``bytes``：作为载荷写入远端内存，以载荷远端地址作为参数
          （用于传递结构体/字符串，如 ``api(cmd, json)`` 打包后的结构）。

        导出函数地址通过 *本地* 加载同一 DLL 计算相对偏移，再加上
        远端模块基址（``dll_handle``）得到（要求本进程与目标进程加载
        同一 DLL 且基址一致——对未开启基址重定位的 DLL 成立）。
        所需的 DLL 本地路径从注入时缓存中按 ``pid`` 与 ``dll_handle`` 反查。

        Args:
            pid: 目标进程 PID。
            dll_handle: 注入时返回的 DLL 模块句柄（即远端基址）。
            func_name: 导出函数名（如 ``init`` / ``api`` / ``loadWindow``）。
            args: 函数参数，见上文说明。
            timeout_ms: 等待远程线程超时（毫秒）。

        Returns:
            远程线程退出码（即导出函数返回值的低 32 位）。

        Raises:
            InjectionError: 调用失败或无法解析函数地址。
            PlatformNotSupportedError: 非 Windows 平台。
        """
        _require_windows()
        assert kernel32 is not None

        if not dll_handle:
            raise InjectionError("无效的 DLL 模块句柄")

        # 1. 从缓存反查 DLL 本地路径（按 handle 匹配）
        dll_path = self._find_dll_path(pid, dll_handle)
        if not dll_path:
            raise InjectionError(
                f"无法找到 handle=0x{dll_handle:X} 对应的 DLL 路径，"
                "请确保已通过 inject_dll 注入该 DLL"
            )

        # 2. 解析远端函数绝对地址
        func_addr = self._resolve_remote_func_addr(
            pid, dll_path, dll_handle, func_name
        )

        # 3. 规整参数
        if args is None:
            arg: Optional[Union[int, bytes]] = None
        elif isinstance(args, int):
            arg = args
        elif isinstance(args, (bytes, bytearray)):
            arg = bytes(args)
        else:
            # 其他序列类型暂不支持（多参数需调用方自行打包为 bytes）
            raise InjectionError(
                f"不支持的参数类型: {type(args).__name__}，"
                "请传入 int（指针）、bytes（载荷）或 None"
            )

        # 4. 创建远程线程调用
        return self.call_remote_thread(pid, func_addr, arg, timeout_ms)

    def _find_dll_path(self, pid: int, dll_handle: int) -> Optional[str]:
        """根据 PID 与模块句柄从缓存反查 DLL 本地路径。"""
        with self._lock:
            for (c_pid, _c_name), info in self._injected.items():
                if c_pid == pid and info.get("handle") == dll_handle:
                    return info.get("path")
        return None

    def _resolve_remote_func_addr(
        self, pid: int, dll_path: str, dll_handle: int, func_name: str
    ) -> int:
        """解析注入 DLL 中导出函数的远端绝对地址。

        通过本地加载同一 DLL，计算导出函数的 RVA
        (本地函数地址 - 本地基址)，再加远端模块基址得到远端地址。

        Args:
            pid: 目标进程 PID（未直接使用，保留以备扩展）。
            dll_path: DLL 本地路径（用于本地加载）。
            dll_handle: 远端模块句柄/基址。
            func_name: 导出函数名。

        Returns:
            远端函数绝对地址。

        Raises:
            InjectionError: 解析失败。
        """
        _require_windows()
        assert kernel32 is not None
        try:
            local_dll = ctypes.WinDLL(dll_path)  # type: ignore[attr-defined]
        except OSError as e:
            raise InjectionError(f"本地加载 DLL 失败: {dll_path} ({e})")

        # 本地基址
        local_base = kernel32.GetModuleHandleW(os.path.basename(dll_path))
        if not local_base:
            # WinDLL 加载后句柄即模块基址
            local_base = ctypes.cast(local_dll._handle, ctypes.c_void_p).value  # type: ignore[attr-defined]
        if not local_base:
            raise InjectionError("无法获取本地 DLL 基址")

        local_func = kernel32.GetProcAddress(local_base, func_name.encode("ascii"))
        if not local_func:
            raise InjectionError(
                f"无法获取本地导出函数: {func_name}（DLL 未导出该函数？）"
            )

        rva = int(local_func) - int(local_base)
        remote_func = int(dll_handle) + rva
        logger.debug(
            f"解析导出函数 {func_name}: 本地基址=0x{int(local_base):X} "
            f"RVA=0x{rva:X} 远端基址=0x{int(dll_handle):X} "
            f"远端地址=0x{remote_func:X}"
        )
        return int(remote_func)

    def call_remote_thread(
        self,
        pid: int,
        func_addr: int,
        arg: Optional[Union[int, bytes]] = None,
        timeout_ms: int = 30000,
    ) -> int:
        """在目标进程创建远程线程执行指定地址的函数。

        这是 :meth:`call_remote_function` 的底层实现，已解析好函数地址后调用。

        Args:
            pid: 目标进程 PID。
            func_addr: 远端函数绝对地址。
            arg: 线程参数。``int`` 直接作为指针；``bytes`` 写入远端内存后
                以远端地址作为参数；``None`` 传 0。
            timeout_ms: 等待超时（毫秒）。

        Returns:
            线程退出码（函数返回值低 32 位）。

        Raises:
            InjectionError: 调用失败。
        """
        _require_windows()
        assert kernel32 is not None

        process_handle = 0
        remote_arg_addr = 0
        thread_handle = 0
        need_free_arg = False
        try:
            process_handle = self.open_process(pid)

            # 处理参数
            arg_value = 0
            if isinstance(arg, int):
                arg_value = arg
            elif isinstance(arg, (bytes, bytearray)):
                payload = bytes(arg)
                remote_arg_addr = self.remote_alloc(
                    process_handle, max(len(payload), 1)
                )
                self.remote_write(process_handle, remote_arg_addr, payload)
                arg_value = remote_arg_addr
                need_free_arg = True
            # None -> 0

            thread_handle = kernel32.CreateRemoteThread(
                process_handle, None, 0, func_addr, arg_value, 0, None
            )
            if not thread_handle:
                err = kernel32.GetLastError()
                raise InjectionError(f"CreateRemoteThread 失败 error={err}")

            wait = kernel32.WaitForSingleObject(thread_handle, timeout_ms)
            if wait == WAIT_TIMEOUT:
                raise InjectionError(f"远程线程执行超时（{timeout_ms}ms）")

            exit_code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeThread(thread_handle, ctypes.byref(exit_code)):
                raise InjectionError("GetExitCodeThread 失败")
            return int(exit_code.value)
        finally:
            if thread_handle:
                kernel32.CloseHandle(thread_handle)
            if need_free_arg and remote_arg_addr and process_handle:
                self.remote_free(process_handle, remote_arg_addr)
            if process_handle:
                self.close_handle(process_handle)

    # ------------------------------------------------------------------ #
    #  内存补丁
    # ------------------------------------------------------------------ #
    def memory_patch(
        self, pid: int, address: int, data: bytes, protect: bool = True
    ) -> bytes:
        """对目标进程内存打补丁（写入指定地址），返回原始字节以便恢复。

        Args:
            pid: 目标进程 PID。
            address: 远端写入地址。
            data: 补丁字节。
            protect: 是否先修改内存保护属性为可读写再恢复。

        Returns:
            被覆盖的原始字节（长度与 data 相同）。

        Raises:
            InjectionError: 操作失败。
        """
        _require_windows()
        assert kernel32 is not None
        process_handle = 0
        try:
            process_handle = self.open_process(pid)
            # 保存原始字节
            original = self.remote_read(process_handle, address, len(data))
            # 修改保护属性
            old_protect = wintypes.DWORD(0)
            if protect:
                kernel32.VirtualProtectEx(
                    process_handle, address, len(data),
                    PAGE_EXECUTE_READWRITE, ctypes.byref(old_protect),
                )
            # 写入补丁
            self.remote_write(process_handle, address, data)
            # 恢复保护属性
            if protect:
                tmp = wintypes.DWORD(0)
                kernel32.VirtualProtectEx(
                    process_handle, address, len(data),
                    old_protect.value, ctypes.byref(tmp),
                )
            logger.debug(
                f"内存补丁: pid={pid} addr=0x{address:X} "
                f"len={len(data)} 原始={original.hex()}"
            )
            return original
        finally:
            if process_handle:
                self.close_handle(process_handle)

    def memory_restore(self, pid: int, address: int, original: bytes) -> None:
        """恢复先前 :meth:`memory_patch` 覆盖的内存。

        Args:
            pid: 目标进程 PID。
            address: 远端地址。
            original: :meth:`memory_patch` 返回的原始字节。
        """
        _require_windows()
        assert kernel32 is not None
        process_handle = 0
        try:
            process_handle = self.open_process(pid)
            old_protect = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(
                process_handle, address, len(original),
                PAGE_EXECUTE_READWRITE, ctypes.byref(old_protect),
            )
            self.remote_write(process_handle, address, original)
            tmp = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(
                process_handle, address, len(original),
                old_protect.value, ctypes.byref(tmp),
            )
            logger.debug(f"内存恢复: pid={pid} addr=0x{address:X} len={len(original)}")
        finally:
            if process_handle:
                self.close_handle(process_handle)


# ====================================================================== #
#  自测入口
# ====================================================================== #
def _self_test() -> None:
    """非 Windows 平台自测：验证 import 与平台检测。"""
    logger.info(f"IS_WINDOWS = {IS_WINDOWS}")
    logger.info(f"微信版本 = {WECHAT_VERSION}")
    injector = DLLInjector()
    logger.info(f"DLLInjector 实例化成功: {injector}")
    if not IS_WINDOWS:
        try:
            injector.inject_dll(0, "fake.dll")
        except PlatformNotSupportedError as e:
            logger.info(f"非 Windows 平台正确拦截: {e}")


if __name__ == "__main__":
    _self_test()
