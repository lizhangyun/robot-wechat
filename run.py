#!/usr/bin/env python3
"""
机器人3 复刻版 - 主启动脚本

功能:
  - 初始化配置和目录
  - 启动 FastAPI 服务器 (uvicorn)
  - 信号处理 (优雅关闭)
  - 命令行参数 (--host, --port, --mock)

用法:
  python run.py                      # 默认 0.0.0.0:3000
  python run.py --host 127.0.0.1 --port 8080
  python run.py --mock               # Mock 模式 (无真实微信时模拟)
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path
from typing import Optional

# 确保项目根目录在 sys.path 中 (支持直接 python run.py 运行)
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import uvicorn
from loguru import logger

from config.settings import settings


def setup_logging(level: str = "INFO") -> None:
    """配置 loguru 日志 (控制台 + 文件)"""
    logger.remove()  # 移除默认 handler
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    # 控制台
    logger.add(
        sys.stderr,
        level=level,
        format=log_format,
        colorize=True,
        backtrace=True,
        diagnose=True,
    )
    # 文件 (按大小滚动)
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(settings.log_dir / "robot3_{time}.log"),
            level=level,
            format=log_format,
            rotation=f"{settings.log_max_size_mb} MB",
            retention=f"{settings.log_retention_days} days",
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"配置文件日志失败, 仅使用控制台输出: {exc}")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description=f"{settings.app_name} - 微信自动化机器人 (复刻版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python run.py                    # 默认配置启动\n"
               "  python run.py --port 8080        # 指定端口\n"
               "  python run.py --mock             # Mock 模拟模式\n",
    )
    parser.add_argument("--host", default=settings.api_host,
                        help=f"监听地址 (默认: {settings.api_host})")
    parser.add_argument("--port", type=int, default=settings.api_port,
                        help=f"监听端口 (默认: {settings.api_port})")
    parser.add_argument("--mock", action="store_true",
                        help="Mock 模式: 无真实微信时模拟消息收发")
    parser.add_argument("--log-level", default=settings.log_level,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help=f"日志级别 (默认: {settings.log_level})")
    parser.add_argument("--reload", action="store_true",
                        help="开发模式热重载 (仅调试用)")
    parser.add_argument("--workers", type=int, default=1,
                        help="工作进程数 (默认: 1)")
    return parser.parse_args(argv)


def setup_signal_handlers(server: uvicorn.Server) -> None:
    """注册信号处理器 (优雅关闭)"""

    def handle_signal(signum: int, frame) -> None:
        sig_name = signal.Signals(signum).name
        logger.warning(f"收到信号 {sig_name}, 开始优雅关闭...")
        # 触发 uvicorn 的退出
        server.should_exit = True

    # Windows 仅支持 SIGINT / SIGBREAK
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            # 某些信号在非主线程或特定平台不可用
            pass
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, handle_signal)
        except (ValueError, OSError):
            pass


def run(host: str, port: int, mock: bool, log_level: str,
        reload: bool = False, workers: int = 1) -> None:
    """启动服务"""
    # 1. 确保目录存在
    settings.ensure_dirs()

    # 2. Mock 模式提示
    if mock:
        logger.warning("已启用 Mock 模式: 将模拟微信消息收发, 不连接真实微信")
        # Mock 模式下自动创建一个示例实例 (便于测试)
        _ensure_mock_instance()

    # 3. 创建应用 (reload 模式下需用字符串导入)
    if reload:
        logger.info("开发模式 (reload) 启动, workers 强制为 1")
        workers = 1
        uvicorn.run(
            "api.server:app",
            host=host,
            port=port,
            reload=True,
            log_level=log_level.lower(),
            factory=False,
        )
    else:
        # 生产模式: 显式创建 app 并设置信号处理
        from api.server import create_app
        app = create_app(mock=mock)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level.lower(),
            workers=workers,
            access_log=True,
        )
        server = uvicorn.Server(config)
        setup_signal_handlers(server)
        logger.info(f"启动服务: http://{host}:{port} (workers={workers})")
        server.run()


def _ensure_mock_instance() -> None:
    """Mock 模式下预创建示例实例 (异步, 延迟到引擎启动后)"""
    async def _create() -> None:
        try:
            from core.engine import engine
            await engine.start()
            instances = await engine.list_instances()
            if not instances:
                logger.info("Mock 模式: 创建示例实例 'demo'")
                await engine.create_instance("demo", "演示实例", "wxid_demo")
                await engine.start_instance("demo")
            # 停止引擎, 由 server 的 lifespan 重新接管
            await engine.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Mock 预创建实例失败 (可忽略): {exc}")

    try:
        asyncio.run(_create())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Mock 预创建跳过: {exc}")


def main(argv: Optional[list[str]] = None) -> int:
    """主入口"""
    args = parse_args(argv)
    setup_logging(args.log_level)
    logger.info("=" * 60)
    logger.info(f"{settings.app_name} v{settings.app_version}")
    logger.info(f"工作目录: {_ROOT}")
    logger.info(f"数据目录: {settings.data_dir}")
    logger.info("=" * 60)
    try:
        run(
            host=args.host,
            port=args.port,
            mock=args.mock,
            log_level=args.log_level,
            reload=args.reload,
            workers=args.workers,
        )
    except KeyboardInterrupt:
        logger.info("用户中断, 退出")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"启动失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
