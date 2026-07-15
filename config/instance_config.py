"""
实例配置 - 对应原软件 data/app/c680X/config.ini
每个机器人实例有独立配置和数据库
"""
from pydantic import BaseModel, Field
from pathlib import Path
from typing import Optional
import configparser
import json
from config.settings import settings


class InstanceConfig(BaseModel):
    """单个机器人实例的配置"""
    instance_id: str = ""  # 如 c6801, c6802
    display_name: str = ""

    # 微信账号
    wxid: str = ""  # 当前登录的微信ID

    # 记账模块配置
    jizhang_enabled: bool = True
    jizhang_domain: str = ""  # 后端API域名
    jizhang_keyword: str = ""  # AES加密的功能配置

    # 消息配置
    msg_split_enabled: bool = True
    msg_max_lines: int = 70
    msg_sleep_sec: float = 1.0

    # 线程配置
    thread_post_count: int = 50

    # 数据库
    db_path: Path = Path("")

    @classmethod
    def from_ini(cls, ini_path: Path) -> "InstanceConfig":
        """从INI文件加载配置 (兼容原软件格式)"""
        config = configparser.ConfigParser()
        config.read(ini_path, encoding="gbk")

        instance_id = ini_path.parent.name

        return cls(
            instance_id=instance_id,
            display_name=instance_id,
            jizhang_enabled=config.getboolean("jizhang", "enabled", fallback=True),
            jizhang_domain=config.get("jizhang", "domain", fallback=""),
            jizhang_keyword=config.get("jizhang", "keyword", fallback=""),
            msg_split_enabled=config.getboolean("msg_split", "status", fallback=True),
            msg_max_lines=config.getint("msg", "消息最多行数", fallback=70),
            msg_sleep_sec=config.getfloat("sleep_time", "sec", fallback=1.0),
            thread_post_count=config.getint("thread", "post", fallback=50),
            db_path=settings.db_dir / f"{instance_id}_data.db",
        )

    def save_ini(self, ini_path: Path):
        """保存为INI文件 (兼容原软件格式)"""
        config = configparser.ConfigParser()

        config["jizhang"] = {
            "enabled": str(self.jizhang_enabled),
            "keyword": self.jizhang_keyword,
            "domain": self.jizhang_domain,
        }
        config["msg_split"] = {"status": "1" if self.msg_split_enabled else "0"}
        config["msg"] = {"消息最多行数": str(self.msg_max_lines)}
        config["sleep_time"] = {"sec": str(self.msg_sleep_sec)}
        config["thread"] = {"post": str(self.thread_post_count)}

        ini_path.parent.mkdir(parents=True, exist_ok=True)
        with open(ini_path, "w", encoding="gbk") as f:
            config.write(f)

    def to_dict(self) -> dict:
        return self.model_dump()
