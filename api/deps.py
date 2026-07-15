"""
API 依赖注入 - 提供引擎、WebSocket、数据库等依赖

优先从 app.state 读取 (便于测试替换), 否则使用全局单例
"""
from __future__ import annotations

from fastapi import Request

from core.engine import CoreEngine, engine as _global_engine
from core.websocket_manager import WebSocketManager, ws_manager as _global_ws
from database.manager import DatabaseManager, db_manager as _global_db


def get_engine(request: Request) -> CoreEngine:
    """获取核心引擎"""
    return getattr(request.app.state, "engine", None) or _global_engine


def get_ws_manager(request: Request) -> WebSocketManager:
    """获取 WebSocket 管理器"""
    return getattr(request.app.state, "ws_manager", None) or _global_ws


def get_db(request: Request) -> DatabaseManager:
    """获取数据库管理器"""
    return getattr(request.app.state, "db", None) or _global_db
