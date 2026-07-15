"""
全局配置 - 对应原软件 data/config.ini
"""
from pydantic_settings import BaseSettings
from pathlib import Path
from typing import Optional
import os


class Settings(BaseSettings):
    # === 基础配置 ===
    app_name: str = "机器人3-复刻版"
    app_version: str = "1.0.0"
    app_id: str = "robot3_replica"

    # === 路径配置 ===
    base_dir: Path = Path(__file__).parent.parent
    data_dir: Path = base_dir / "data"
    log_dir: Path = base_dir / "logs"
    web_dir: Path = base_dir / "web"

    # === 数据库配置 ===
    db_dir: Path = data_dir / "db"
    main_db_name: str = "data.db"
    users_db_name: str = "users.db"
    appdata_db_name: str = "appdata.db"
    db_encrypt_key: str = ""  # 留空则不加密

    # === HTTP API 配置 ===
    api_host: str = "0.0.0.0"
    api_port: int = 3000

    # === 微信配置 ===
    wechat_version: str = "3.9.12.56"
    wechat_path: str = ""  # 微信安装路径
    wechat_hook_dll: str = ""  # Hook DLL路径
    msg_max_lines: int = 70  # 消息最多行数
    msg_sleep_sec: float = 1.0  # 发送间隔(秒)
    msg_split_enabled: bool = True  # 长消息分片

    # === 线程配置 ===
    thread_pool_size: int = 50  # 工作线程数

    # === 后端配置 ===
    backend_url: str = ""  # 后端API地址
    backend_token: str = ""  # 后端认证Token

    # === 安全配置 ===
    license_id: str = ""  # 许可证ID (对应 run.vef)
    ip_whitelist_enabled: bool = False
    ip_whitelist: list[str] = []

    # === 日志配置 ===
    log_level: str = "INFO"
    log_to_file: bool = True
    log_max_size_mb: int = 10
    log_retention_days: int = 30

    # === 更新配置 ===
    update_repo: str = ""  # 更新仓库地址
    auto_update: bool = False

    class Config:
        env_prefix = "ROBOT3_"
        env_file = ".env"

    def ensure_dirs(self):
        """确保所有必要目录存在"""
        for d in [self.data_dir, self.log_dir, self.db_dir, self.web_dir]:
            d.mkdir(parents=True, exist_ok=True)


# 全局单例
settings = Settings()
