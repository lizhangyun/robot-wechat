"""
功能模块 - 自动回复、记账、群管理、脚本引擎等业务组件

子模块：
- :mod:`modules.auto_reply`        自动回复
- :mod:`modules.bookkeeping`       记账
- :mod:`modules.group_manager`     群管理
- :mod:`modules.jizhang_config`    记账配置
- :mod:`modules.message_splitter`  消息分片
- :mod:`modules.task_scheduler`    任务调度
- :mod:`modules.script_engine`     嵌入式 JS 脚本引擎（对应原软件 node.dll）
"""
from __future__ import annotations

from modules.script_engine import ScriptEngine, create_script_engine

__all__ = [
    "ScriptEngine",
    "create_script_engine",
]
