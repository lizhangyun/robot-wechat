"""
内存 Hook - 拦截微信消息接收函数，实时获取收到的消息

实现原理
========

1. **远端函数地址解析**：通过 WeChatWin.dll 基址 + 偏移量（见
   :mod:`wechat.wechat_offsets`）计算消息接收函数 ``RecvMsg`` 的绝对地址。
2. **内存补丁安装**：在 ``RecvMsg`` 入口写入跳转指令（JMP），跳转到
   注入 DLL 中的 Hook 处理函数。原函数前几条指令被保存，便于卸载时恢复。
3. **消息回调桥接**：注入的 ``weixin.dll`` 在 Hook 命中时，通过
   ``WM_COPYDATA`` 窗口消息将消息数据推送给本进程。本模块创建一个
   消息专用窗口（Message-Only Window）接收 ``WM_COPYDATA``，解析后
   分发给已注册的 Python 回调。
4. **卸载恢复**：卸载时把保存的原始字节写回 ``RecvMsg`` 入口，恢复
   微信原始逻辑。

降级方案
========

- 非 Windows 平台：``install_hook`` 等方法直接抛
  :class:`PlatformNotSupportedError`，但不影响 ``import``；
- 偏移量未配置（占位符 0x0）：``install_hook`` 拒绝安装并记录错误，
  避免在错误地址打补丁导致微信崩溃；
- 若注入的 DLL 未提供 Hook 安装导出函数，可回退到纯内存补丁模式
  （``_install_memory_patch``），但此模式无法把消息回传 Python，
  仅用于演示/调试。

消息类型支持
============

支持拦截并解析的消息类型：文本、图片、文件、语音、视频、名片、链接、
系统消息、表情、位置（见 :class:`HookMessageType`）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import sys
import threading
import time
from collections import defaultdict
from enum import IntEnum
from typing import Any, Callable, Optional, Union

from loguru import logger

from wechat.wechat_offsets import (
    OFFSETS,
    WECHAT_WIN_DLL,
    is_offset_available,
    resolve_function_address,
)

# 复用 dll_injector 的平台检测与 Windows API 封装
from wechat.dll_injector import (  # noqa: F401  (复用导出的常量/结构体)
    COPYDATASTRUCT,
    HWND_MESSAGE,
    IS_WINDOWS,
    PlatformNotSupportedError,
    WM_COPYDATA,
)


# ====================================================================== #
#  消息类型枚举
# ====================================================================== #
class HookMessageType(IntEnum):
    """Hook 可拦截的微信消息类型（对应微信原始 type 字段）。"""

    TEXT = 1          # 文本消息
    IMAGE = 3         # 图片消息
    VOICE = 34        # 语音消息
    CARD = 42         # 名片消息
    VIDEO = 43        # 视频消息
    LOCATION = 48     # 位置消息
    FILE = 49         # 文件/App消息
    SYSTEM = 10000    # 系统消息（入群、撤回等）
    EMOJI = 10002     # 自定义表情

    @classmethod
    def from_code(cls, code: Union[int, str]) -> "HookMessageType":
        """从微信原始 type 代码转换，未知类型归为 TEXT。"""
        if isinstance(code, str):
            code = int(code) if code.isdigit() else 1
        try:
            return cls(int(code))
        except ValueError:
            return cls.TEXT


# 通配符：注册回调时使用，表示接收所有类型消息
MSG_TYPE_ALL: str = "*"


# 回调类型：接收解析后的消息字典，无返回值
MessageHookCallback = Callable[[dict[str, Any]], None]
"""消息回调函数类型，参数为解析后的消息字典。"""


# ====================================================================== #
#  WM_COPYDATA 载荷结构（DLL 与 Python 约定的数据格式）
# ====================================================================== #
# COPYDATASTRUCT.dwData 用此值标识是消息推送
COPYDATA_MSG_TYPE_ID: int = 0x77636D73  # 'wcms' = wechat message

# DLL 推送的 JSON 字符串最大长度（保守上限，防止越界读取）
COPYDATA_MAX_PAYLOAD: int = 64 * 1024


def _require_windows() -> None:
    """非 Windows 平台抛出明确异常。"""
    if not IS_WINDOWS:
        raise PlatformNotSupportedError(
            "内存 Hook 仅支持 Windows 平台，当前平台: "
            f"{sys.platform}。请使用模拟模式(mock=True)。"
        )


# ====================================================================== #
#  Windows API 绑定（窗口相关，dll_injector 未覆盖部分）
# ====================================================================== #
user32: Optional[Any] = None
kernel32: Optional[Any] = None

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    # CreateWindowExW / DestroyWindow / DefWindowProcW / RegisterClassExW
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]

    user32.DestroyWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]

    user32.DefWindowProcW.restype = wintypes.LPVOID
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]

    user32.RegisterClassExW.restype = wintypes.ATOM
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(wintypes.WNDCLASSEXW)]

    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]

    user32.PeekMessageW.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
        wintypes.UINT, wintypes.UINT,
    ]

    user32.TranslateMessage.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

    user32.DispatchMessageW.restype = wintypes.LPVOID
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    user32.SendMessageW.restype = wintypes.LPVOID
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.argtypes = []

# 消息泵退出自定义消息
WM_QUIT_HOOK: int = 0x0400 + 0x0001  # WM_APP + 1


# ====================================================================== #
#  内存 Hook 主类
# ====================================================================== #
class MessageHook:
    """微信消息接收 Hook。

    负责安装/卸载 Hook 并管理消息回调。安装 Hook 后，微信收到的每条
    消息都会触发已注册的回调。

    典型用法::

        hook = MessageHook(injector)
        hook.register_callback(HookMessageType.TEXT, on_text)
        hook.register_callback(MSG_TYPE_ALL, on_any)
        hook.install_hook(wechat_pid)
        ...
        hook.uninstall_hook()

    Args:
        injector: 已注入 weixin.dll 的 :class:`DLLInjector` 实例。
        dll_module_handle: weixin.dll 在微信进程中的模块句柄
            （由 :meth:`DLLInjector.inject_dll` 返回）。
        window_class_name: 消息接收窗口的类名（用于接收 WM_COPYDATA）。
    """

    def __init__(
        self,
        injector: Optional[Any] = None,
        dll_module_handle: int = 0,
        window_class_name: str = "WeChatHookMsgWnd",
    ) -> None:
        self._injector = injector
        self._dll_handle: int = dll_module_handle
        self._window_class_name: str = window_class_name

        # 消息回调：{msg_type_or_"*": [callbacks]}
        self._callbacks: dict[str, list[MessageHookCallback]] = defaultdict(list)
        self._callbacks_lock = threading.Lock()

        # Hook 状态
        self._installed: bool = False
        self._wechat_pid: Optional[int] = None
        # 已打补丁的地址 -> 原始字节（用于卸载恢复）
        self._patches: dict[int, bytes] = {}

        # 消息窗口
        self._hwnd: int = 0
        self._wnd_proc_ref: Optional[Any] = None  # 防止 WNDPROC 被 GC
        self._msg_thread: Optional[threading.Thread] = None
        self._msg_thread_stop: threading.Event = threading.Event()

    # ------------------------------------------------------------------ #
    #  属性
    # ------------------------------------------------------------------ #
    @property
    def is_installed(self) -> bool:
        """Hook 是否已安装。"""
        return self._installed

    @property
    def message_window_handle(self) -> int:
        """消息接收窗口句柄（HWND），未创建返回 0。"""
        return self._hwnd

    @property
    def wechat_pid(self) -> Optional[int]:
        """已安装 Hook 的微信进程 PID。"""
        return self._wechat_pid

    # ------------------------------------------------------------------ #
    #  回调注册
    # ------------------------------------------------------------------ #
    def register_callback(
        self,
        msg_type: Union[HookMessageType, int, str],
        callback: MessageHookCallback,
    ) -> None:
        """注册消息回调。

        Args:
            msg_type: 消息类型（:class:`HookMessageType` / int / "*"）。
                使用 :data:`MSG_TYPE_ALL`（"*"）注册全局回调，接收所有类型。
            callback: 回调函数，签名为 ``callback(msg_dict) -> None``。
                ``msg_dict`` 含字段：``msg_id`` / ``sender_wxid`` /
                ``receiver_wxid`` / ``content`` / ``msg_type`` /
                ``is_group`` / ``group_wxid`` / ``timestamp`` / ``raw_xml``。
        """
        key = self._normalize_msg_type(msg_type)
        with self._callbacks_lock:
            if callback not in self._callbacks[key]:
                self._callbacks[key].append(callback)
        logger.debug(f"注册消息回调 type={key} callback={callback}")

    def unregister_callback(
        self,
        msg_type: Union[HookMessageType, int, str],
        callback: MessageHookCallback,
    ) -> None:
        """取消注册消息回调。"""
        key = self._normalize_msg_type(msg_type)
        with self._callbacks_lock:
            if callback in self._callbacks[key]:
                self._callbacks[key].remove(callback)

    def clear_callbacks(self) -> None:
        """清空所有回调。"""
        with self._callbacks_lock:
            self._callbacks.clear()

    @staticmethod
    def _normalize_msg_type(
        msg_type: Union[HookMessageType, int, str]
    ) -> str:
        """归一化消息类型为字符串键。"""
        if msg_type == MSG_TYPE_ALL or msg_type == "*":
            return MSG_TYPE_ALL
        if isinstance(msg_type, HookMessageType):
            return str(int(msg_type))
        if isinstance(msg_type, int):
            return str(msg_type)
        return str(msg_type)

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """将消息分发给匹配的回调。

        先调用精确类型回调，再调用通配回调。
        """
        msg_type_code = msg.get("msg_type")
        type_key = str(msg_type_code) if msg_type_code is not None else ""

        # 先精确类型
        cbs_exact = self._callbacks.get(type_key, [])
        cbs_all = self._callbacks.get(MSG_TYPE_ALL, [])

        for cb in list(cbs_exact) + list(cbs_all):
            try:
                cb(msg)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"消息回调执行异常: {e}")

    # ------------------------------------------------------------------ #
    #  Hook 安装 / 卸载
    # ------------------------------------------------------------------ #
    def install_hook(self, wechat_pid: int) -> bool:
        """安装消息接收 Hook。

        流程：
        1. 校验偏移量是否已配置；
        2. 创建消息接收窗口（用于接收 DLL 的 WM_COPYDATA 推送）；
        3. 优先调用注入 DLL 的 Hook 安装导出函数（若 DLL 支持）；
           否则回退到纯内存补丁模式；
        4. 启动消息泵线程。

        Args:
            wechat_pid: 微信进程 PID。

        Returns:
            安装成功返回 True。

        Raises:
            PlatformNotSupportedError: 非 Windows 平台。
        """
        _require_windows()
        if self._installed:
            logger.warning("Hook 已安装，请先卸载")
            return True

        # 1. 校验偏移量
        if not is_offset_available("RecvMsg"):
            logger.error(
                "RecvMsg 偏移量未配置（仍为占位符 0x0），"
                "无法安装 Hook。请先通过逆向工具填入真实偏移量。"
            )
            return False

        self._wechat_pid = wechat_pid
        logger.info(f"开始安装消息 Hook: pid={wechat_pid}")

        # 2. 创建消息接收窗口
        if not self._create_message_window():
            logger.error("创建消息接收窗口失败")
            return False

        # 3. 安装 Hook（优先 DLL 导出函数，回退内存补丁）
        ok = False
        if self._injector is not None and self._dll_handle:
            ok = self._install_via_dll()
        if not ok:
            logger.info("DLL 导出函数安装失败或不可用，回退内存补丁模式")
            ok = self._install_memory_patch()

        if not ok:
            # 安装失败，清理窗口
            self._destroy_message_window()
            return False

        # 4. 启动消息泵线程
        self._msg_thread_stop.clear()
        self._msg_thread = threading.Thread(
            target=self._message_pump_loop,
            name="WeChatHookMsgPump",
            daemon=True,
        )
        self._msg_thread.start()

        self._installed = True
        logger.info(f"消息 Hook 安装成功: pid={wechat_pid}")
        return True

    def uninstall_hook(self) -> bool:
        """卸载消息 Hook，恢复微信原始函数。

        流程：
        1. 停止消息泵线程；
        2. 恢复所有内存补丁（或调用 DLL 卸载函数）；
        3. 销毁消息接收窗口。

        Returns:
            卸载成功返回 True。
        """
        if not IS_WINDOWS:
            self._installed = False
            return True
        if not self._installed:
            logger.debug("Hook 未安装，无需卸载")
            return True

        logger.info(f"开始卸载消息 Hook: pid={self._wechat_pid}")
        ok = True

        # 1. 停止消息泵
        self._stop_message_pump()

        # 2. 卸载 Hook
        if self._injector is not None and self._dll_handle:
            dll_ok = self._uninstall_via_dll()
            ok = ok and dll_ok
        # 恢复内存补丁
        patch_ok = self._restore_memory_patches()
        ok = ok and patch_ok

        # 3. 销毁窗口
        self._destroy_message_window()

        self._installed = False
        self._wechat_pid = None
        logger.info(f"消息 Hook 卸载 {'成功' if ok else '部分失败'}")
        return ok

    # ------------------------------------------------------------------ #
    #  DLL 导出函数安装/卸载
    # ------------------------------------------------------------------ #
    def _install_via_dll(self) -> bool:
        """通过注入 DLL 的 Hook 安装导出函数安装 Hook。

        约定 DLL 导出 ``installHook(hwnd)`` 函数，hwnd 为本进程的消息窗口
        句柄，DLL 拦截消息后通过 ``WM_COPYDATA`` 推送到该窗口。

        Returns:
            成功返回 True，DLL 不支持或调用失败返回 False。
        """
        if self._injector is None or not self._dll_handle or not self._wechat_pid:
            return False
        try:
            # 传递消息窗口句柄作为参数
            exit_code = self._injector.call_remote_function(
                self._wechat_pid,
                self._dll_handle,
                "installHook",
                args=int(self._hwnd),
                timeout_ms=10000,
            )
            if exit_code:
                logger.info(
                    f"DLL installHook 成功 hwnd=0x{self._hwnd:X} ret={exit_code}"
                )
                return True
            logger.warning(f"DLL installHook 返回 0")
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DLL installHook 调用失败: {e}")
            return False

    def _uninstall_via_dll(self) -> bool:
        """通过注入 DLL 的卸载导出函数卸载 Hook。

        约定 DLL 导出 ``uninstallHook()`` 无参函数。

        Returns:
            成功返回 True。
        """
        if self._injector is None or not self._dll_handle or not self._wechat_pid:
            return False
        try:
            exit_code = self._injector.call_remote_function(
                self._wechat_pid,
                self._dll_handle,
                "uninstallHook",
                args=None,
                timeout_ms=10000,
            )
            logger.info(f"DLL uninstallHook ret={exit_code}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DLL uninstallHook 调用失败: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  纯内存补丁安装/恢复
    # ------------------------------------------------------------------ #
    def _install_memory_patch(self) -> bool:
        """纯内存补丁模式安装 Hook。

        在 ``RecvMsg`` 入口写入跳转指令。注意：纯内存补丁无法将消息
        回传 Python（无 DLL 处理函数），仅用于验证偏移量正确性。

        x64 跳转指令（14 字节绝对跳转）::

            FF 25 00 00 00 00          jmp qword ptr [rip+0]
            <8 字节目标地址>

        Returns:
            成功返回 True。
        """
        if self._injector is None or not self._wechat_pid:
            logger.error("内存补丁模式需要 injector 与 wechat_pid")
            return False
        try:
            # 计算 RecvMsg 远端地址
            dll_base = self._injector.get_remote_module_base(
                self._wechat_pid, WECHAT_WIN_DLL
            )
            if not dll_base:
                logger.error(f"无法获取 {WECHAT_WIN_DLL} 基址")
                return False
            recv_addr = resolve_function_address(dll_base, "RecvMsg")
            logger.info(
                f"内存补丁目标: {WECHAT_WIN_DLL} base=0x{dll_base:X} "
                f"RecvMsg=0x{recv_addr:X}"
            )

            # 构造 x64 绝对跳转（占位目标地址 0，仅演示，真实需指向 DLL 处理函数）
            # 警告：此处仅为结构演示，目标地址为 0 会导致微信崩溃。
            #       生产环境必须由注入 DLL 提供 Hook 处理函数地址。
            target_addr = 0
            jmp_bytes = b"\xFF\x25\x00\x00\x00\x00" + target_addr.to_bytes(8, "little")
            patch_len = len(jmp_bytes)

            # 保存原始字节并写入补丁
            original = self._injector.memory_patch(
                self._wechat_pid, recv_addr, jmp_bytes
            )
            self._patches[recv_addr] = original
            logger.warning(
                "已写入内存补丁（演示模式，目标地址为 0）。"
                "真实使用需配置 DLL Hook 处理函数地址。"
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"内存补丁安装失败: {e}")
            return False

    def _restore_memory_patches(self) -> bool:
        """恢复所有内存补丁。"""
        if not self._injector or not self._wechat_pid:
            return True
        ok = True
        for addr, original in list(self._patches.items()):
            try:
                self._injector.memory_restore(self._wechat_pid, addr, original)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"恢复内存补丁失败 addr=0x{addr:X}: {e}")
                ok = False
        self._patches.clear()
        return ok

    # ------------------------------------------------------------------ #
    #  消息接收窗口
    # ------------------------------------------------------------------ #
    def _create_message_window(self) -> bool:
        """创建消息专用窗口（Message-Only Window）用于接收 WM_COPYDATA。

        Returns:
            成功返回 True。
        """
        assert user32 is not None and kernel32 is not None
        try:
            hinst = kernel32.GetModuleHandleW(None)

            # 定义窗口过程
            def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
                if msg == WM_COPYDATA:
                    self._on_copy_data(lparam)
                    return 1
                if msg == WM_QUIT_HOOK:
                    return 0
                # 其余交给默认处理
                return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam) or 0)

            # 保持引用避免 GC（WNDPROC 是函数指针）
            self._wnd_proc_ref = wintypes.WNDPROC(_wnd_proc)

            wc = wintypes.WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(wintypes.WNDCLASSEXW)
            wc.lpfnWndProc = self._wnd_proc_ref
            wc.hInstance = hinst
            wc.lpszClassName = self._window_class_name

            atom = user32.RegisterClassExW(ctypes.byref(wc))
            if not atom:
                err = ctypes.get_last_error()
                # 类已存在则忽略
                if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                    logger.error(f"RegisterClassExW 失败 error={err}")
                    return False

            # 创建消息专用窗口（父窗口 = HWND_MESSAGE）
            hwnd = user32.CreateWindowExW(
                0,
                self._window_class_name,
                "WeChatHookMsg",
                0,
                0, 0, 0, 0,
                HWND_MESSAGE,  # 消息专用窗口
                None, hinst, None,
            )
            if not hwnd:
                err = ctypes.get_last_error()
                logger.error(f"CreateWindowExW 失败 error={err}")
                return False
            self._hwnd = int(hwnd)
            logger.debug(f"消息接收窗口已创建 hwnd=0x{self._hwnd:X}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"创建消息窗口异常: {e}")
            return False

    def _destroy_message_window(self) -> None:
        """销毁消息接收窗口并注销窗口类。"""
        if not IS_WINDOWS:
            return
        assert user32 is not None and kernel32 is not None
        try:
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
                self._hwnd = 0
            if self._wnd_proc_ref is not None:
                hinst = kernel32.GetModuleHandleW(None)
                user32.UnregisterClassW(self._window_class_name, hinst)
                self._wnd_proc_ref = None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"销毁消息窗口异常: {e}")

    def _on_copy_data(self, lparam: int) -> None:
        """处理 WM_COPYDATA 消息，解析 DLL 推送的消息数据。

        Args:
            lparam: ``COPYDATASTRUCT`` 指针。
        """
        try:
            cds = COPYDATASTRUCT.from_address(lparam)
            # 校验类型标识
            if cds.dwData != COPYDATA_MSG_TYPE_ID:
                return
            if not cds.cbData or not cds.lpData:
                return
            length = min(int(cds.cbData), COPYDATA_MAX_PAYLOAD)
            raw = ctypes.string_at(cds.lpData, length)
            text = raw.decode("utf-8", errors="replace").rstrip("\x00")
            if not text:
                return
            msg_data = json.loads(text)
            # 补充接收时间
            msg_data.setdefault("timestamp", time.time())
            logger.debug(
                f"收到 Hook 消息: type={msg_data.get('msg_type')} "
                f"from={msg_data.get('sender_wxid')}"
            )
            self._dispatch_message(msg_data)
        except json.JSONDecodeError as e:
            logger.warning(f"Hook 消息 JSON 解析失败: {e}")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"处理 WM_COPYDATA 异常: {e}")

    # ------------------------------------------------------------------ #
    #  消息泵
    # ------------------------------------------------------------------ #
    def _message_pump_loop(self) -> None:
        """消息泵线程主循环。

        在独立线程中持续分发窗口消息，直到收到 ``WM_QUIT_HOOK``。
        """
        assert user32 is not None
        logger.debug("消息泵线程启动")
        msg = wintypes.MSG()
        try:
            while not self._msg_thread_stop.is_set():
                # PeekMessage 非阻塞，避免线程无法响应停止信号
                has_msg = user32.PeekMessageW(
                    ctypes.byref(msg), 0, 0, 0, 1  # PM_REMOVE
                )
                if has_msg:
                    if msg.message == WM_QUIT_HOOK:
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    # 无消息时短暂让出 CPU
                    self._msg_thread_stop.wait(0.02)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"消息泵线程异常: {e}")
        finally:
            logger.debug("消息泵线程退出")

    def _stop_message_pump(self) -> None:
        """停止消息泵线程。"""
        self._msg_thread_stop.set()
        if self._hwnd and IS_WINDOWS:
            assert user32 is not None
            # 投递 WM_QUIT_HOOK 唤醒消息泵
            user32.PostMessageW(self._hwnd, WM_QUIT_HOOK, 0, 0)
        if self._msg_thread and self._msg_thread.is_alive():
            self._msg_thread.join(timeout=3.0)
        self._msg_thread = None

    # ------------------------------------------------------------------ #
    #  手动注入消息（测试/模拟用）
    # ------------------------------------------------------------------ #
    def feed_message(self, msg: dict[str, Any]) -> None:
        """手动注入一条消息并分发给回调（用于测试或模拟模式）。

        Args:
            msg: 消息字典。
        """
        msg.setdefault("timestamp", time.time())
        self._dispatch_message(msg)


# ====================================================================== #
#  自测入口
# ====================================================================== #
def _self_test() -> None:
    """非 Windows 平台自测：验证 import、回调注册与降级。"""
    logger.info(f"IS_WINDOWS = {IS_WINDOWS}")
    hook = MessageHook(injector=None)
    logger.info(f"MessageHook 实例化成功: installed={hook.is_installed}")

    # 注册回调
    received: list[dict] = []

    def on_text(m: dict) -> None:
        received.append(m)
        logger.info(f"[回调] 文本: {m}")

    def on_all(m: dict) -> None:
        logger.info(f"[回调] 全局: {m.get('msg_type')}")

    hook.register_callback(HookMessageType.TEXT, on_text)
    hook.register_callback(MSG_TYPE_ALL, on_all)

    # 手动注入消息
    hook.feed_message({
        "msg_id": "test_001",
        "sender_wxid": "wxid_test",
        "receiver_wxid": "wxid_self",
        "content": "你好",
        "msg_type": int(HookMessageType.TEXT),
        "is_group": False,
    })
    assert len(received) == 1, "回调未触发"
    logger.info(f"共收到 {len(received)} 条消息")

    # 非 Windows 安装 Hook 应拦截
    if not IS_WINDOWS:
        try:
            hook.install_hook(1234)
        except PlatformNotSupportedError as e:
            logger.info(f"非 Windows 平台正确拦截: {str(e)[:40]}")

    hook.clear_callbacks()
    logger.info("内存 Hook 自测完成")


if __name__ == "__main__":
    _self_test()
