"""
FastAPI 主服务器

职责:
  - 创建 FastAPI app, 配置 CORS
  - 挂载所有路由 (message / contact / group / instance)
  - 全局 WebSocket 端点 /ws (实时消息推送)
  - 静态文件服务 (挂载 web 目录)
  - 启动时初始化数据库和核心引擎
  - 全局异常处理
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from api.routes import contact, group, instance, message
from config.settings import settings
from core.engine import CoreEngine, engine
from core.websocket_manager import WebSocketManager, ws_manager

# 挂载的依赖 (便于 run.py / 测试访问)
_engine: CoreEngine = engine
_ws_manager: WebSocketManager = ws_manager


def create_app(mock: bool = False) -> FastAPI:
    """
    创建并配置 FastAPI 应用

    Args:
        mock: 是否启用 mock 模式 (无真实微信时模拟消息收发)
    """
    # 配置 mock 模式
    _engine.mock = mock

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """应用生命周期: 启动/关闭"""
        # ---- 启动 ----
        settings.ensure_dirs()
        logger.info(f"启动 {settings.app_name} v{settings.app_version} (mock={mock})")
        # 注入到 app.state 供依赖使用
        app.state.engine = _engine
        app.state.ws_manager = _ws_manager
        app.state.db = _engine.db
        await _engine.start()
        logger.info(f"HTTP API 服务就绪: http://{settings.api_host}:{settings.api_port}")
        yield
        # ---- 关闭 ----
        logger.info("应用关闭中...")
        await _engine.stop()
        logger.info("应用已停止")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="微信自动化机器人 - 复刻版 HTTP API",
        lifespan=lifespan,
    )

    # ---- CORS ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- 全局异常处理 ----
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(f"未处理的异常: {request.method} {request.url.path} -> {exc}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "内部服务器错误", "detail": str(exc)},
        )

    # ---- 健康检查 ----
    @app.get("/api/health", tags=["系统"])
    async def health() -> dict:
        return {
            "status": "ok",
            "app": settings.app_name,
            "version": settings.app_version,
            "mock": mock,
        }

    # ---- 仪表盘统计 ----
    @app.get("/api/dashboard", tags=["系统"])
    async def dashboard() -> dict:
        return await _engine.dashboard_stats()

    # ---- 挂载业务路由 ----
    app.include_router(message.router)
    app.include_router(contact.router)
    app.include_router(group.router)
    app.include_router(instance.router)

    # ---- 全局 WebSocket /ws ----
    @app.websocket("/ws")
    async def global_ws(websocket: WebSocket) -> None:
        """
        全局 WebSocket 端点 - 推送所有实例的实时消息

        客户端可发送 {"type":"ping"} 心跳, 服务端回复 {"type":"pong"}
        """
        client = await _ws_manager.connect(websocket, instance_id=None)
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"全局 WebSocket 异常: {exc}")
        finally:
            _ws_manager.disconnect(client)

    # ---- 静态文件服务 (Web 管理界面) ----
    web_dir = settings.web_dir
    if web_dir.exists():
        app.mount("/web", StaticFiles(directory=str(web_dir), html=True), name="web")
        logger.info(f"已挂载 Web 静态目录: {web_dir}")

        # 根路径重定向到 Web 界面
        @app.get("/", include_in_schema=False)
        async def root_redirect() -> dict:
            return {
                "message": f"{settings.app_name} 已运行",
                "web": "/web/index.html",
                "docs": "/docs",
                "health": "/api/health",
            }
    else:
        logger.warning(f"Web 目录不存在: {web_dir}, 跳过静态文件挂载")

        @app.get("/", include_in_schema=False)
        async def root() -> dict:
            return {
                "message": f"{settings.app_name} 已运行",
                "docs": "/docs",
                "health": "/api/health",
            }

    return app


# 默认应用实例 (供 uvicorn 直接引用)
app = create_app()
