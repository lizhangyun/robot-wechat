"""
QQ 自动化模块 - 对应原软件 qq.dll

提供 QQ NT (Windows) 的 Hook 客户端，与 :mod:`wechat` 模块结构对齐：

- :mod:`qq.qq_offsets`       QQ NT 版本偏移量表
- :mod:`qq.qq_hook_interface` QQ Hook 抽象接口、API 命令枚举、消息 Hook
- :mod:`qq.qq_client`        QQ 客户端实现（模拟模式 + 真实 Hook 模式）

模拟模式（``QQClient(mock=True)``）不依赖真实 QQ，使用内置模拟数据，
适合开发与单测；真实 Hook 模式（``RealQQClient``）通过 DLL 注入
``qq.dll`` 到 ``QQ.exe`` 进程实现自动化，仅 Windows 可用。
"""
from __future__ import annotations

from qq.qq_hook_interface import (
    QQAPICommand,
    QQHookCallback,
    QQHookInterface,
    QQMessage,
    QQMessageCallback,
    QQMessageHook,
)
from qq.qq_offsets import (
    OFFSETS,
    QQ_EXE,
    QQ_VERSION,
    QQ_WIN_DLL,
    QQ_WINDOW_CLASS,
    get_offset,
    is_offset_available,
    resolve_function_address,
    validate_offsets,
)
from qq.qq_client import (
    QQClient,
    RealQQClient,
    create_qq_client,
    create_real_qq_client,
    find_qq_process,
    find_qq_window,
    is_qq_running,
)

__all__ = [
    # 接口与类型
    "QQHookInterface",
    "QQAPICommand",
    "QQMessage",
    "QQMessageHook",
    "QQMessageCallback",
    "QQHookCallback",
    # 客户端
    "QQClient",
    "RealQQClient",
    "create_qq_client",
    "create_real_qq_client",
    # 进程/窗口查找
    "is_qq_running",
    "find_qq_process",
    "find_qq_window",
    # 偏移量
    "OFFSETS",
    "QQ_VERSION",
    "QQ_EXE",
    "QQ_WIN_DLL",
    "QQ_WINDOW_CLASS",
    "get_offset",
    "is_offset_available",
    "resolve_function_address",
    "validate_offsets",
]
