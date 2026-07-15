"""
安全模块 - 加密、许可证、E2EE 端到端加密、防重放等安全组件

子模块：
- :mod:`security.crypto`           AES 加密 / 配置加密
- :mod:`security.license`          许可证管理
- :mod:`security.e2ee`             E2EE 端到端加密客户端（对应原软件 e2eeE.com:8443）
- :mod:`security.anti_replay`      防重放攻击管理器（对应原软件 AntiReplay 组件）
- :mod:`security.firewall`         消息防火墙
- :mod:`security.keyword_decoder`  关键词解码
"""
from __future__ import annotations

from security.anti_replay import AntiReplayManager
from security.e2ee import E2EEClient, E2EEConnectionError, E2EEError, create_e2ee_client

__all__ = [
    "E2EEClient",
    "E2EEError",
    "E2EEConnectionError",
    "create_e2ee_client",
    "AntiReplayManager",
]
