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
    jizhang_keyword: str = ""  # AES加密的功能配置 (legacy 字段名, 与 keyword 同义)

    # keyword: AES 加密的记账功能配置 (十六进制字符串)
    # 解密后包含: 触发关键词列表、银行白名单、功能开关、数据库加密密钥
    # 不同实例(c6801/c6802)的 keyword 不同
    keyword: str = ""

    # jizhang_configs: 绑定的记账配置ID列表 (如 ["c1", "c12"])
    # 对应 data/app/jizhang_c1/, data/app/jizhang_c12/ 等独立记账配置目录
    jizhang_configs: list[str] = Field(default_factory=list)

    # 消息配置
    msg_split_enabled: bool = True
    msg_max_lines: int = 70  # 消息最多行数 (原软件默认70)
    msg_sleep_sec: float = 1.0  # 发送间隔(秒)

    # AckMessage 确认机制配置
    ack_timeout: float = 5.0  # ACK 等待超时(秒)
    ack_max_retries: int = 3  # ACK 最大重试次数

    # 线程配置
    thread_post_count: int = 50

    # 缓存与消息队列开关
    cache_enabled: bool = True  # 缓存开关
    mq_enabled: bool = True  # 消息队列开关

    # 数据库
    db_path: Path = Path("")

    @classmethod
    def from_ini(cls, ini_path: Path) -> "InstanceConfig":
        """从INI文件加载配置 (兼容原软件格式)"""
        config = configparser.ConfigParser(interpolation=None)
        config.read(ini_path, encoding="gbk")

        instance_id = ini_path.parent.name

        # keyword: 优先读取 jizhang.keyword, 同时赋给 jizhang_keyword 和 keyword
        keyword_value = config.get("jizhang", "keyword", fallback="")

        # 读取绑定的记账配置ID列表
        configs_str = config.get("jizhang", "configs", fallback="")
        jizhang_configs = [
            s.strip() for s in configs_str.split(",") if s.strip()
        ] if configs_str else []

        # ACK 配置
        ack_timeout = config.getfloat("ack", "timeout", fallback=5.0)
        ack_max_retries = config.getint("ack", "max_retries", fallback=3)

        # 缓存与消息队列
        cache_enabled = config.getboolean("cache", "enabled", fallback=True)
        mq_enabled = config.getboolean("mq", "enabled", fallback=True)

        return cls(
            instance_id=instance_id,
            display_name=instance_id,
            jizhang_enabled=config.getboolean("jizhang", "enabled", fallback=True),
            jizhang_domain=config.get("jizhang", "domain", fallback=""),
            jizhang_keyword=keyword_value,
            keyword=keyword_value,
            jizhang_configs=jizhang_configs,
            msg_split_enabled=config.getboolean("msg_split", "status", fallback=True),
            msg_max_lines=config.getint("msg", "消息最多行数", fallback=70),
            msg_sleep_sec=config.getfloat("sleep_time", "sec", fallback=1.0),
            ack_timeout=ack_timeout,
            ack_max_retries=ack_max_retries,
            thread_post_count=config.getint("thread", "post", fallback=50),
            cache_enabled=cache_enabled,
            mq_enabled=mq_enabled,
            db_path=settings.db_dir / f"{instance_id}_data.db",
        )

    def save_ini(self, ini_path: Path):
        """保存为INI文件 (兼容原软件格式)"""
        config = configparser.ConfigParser(interpolation=None)

        # keyword 值: 优先使用 jizhang_keyword, 其次使用 keyword
        keyword_value = self.jizhang_keyword or self.keyword

        config["jizhang"] = {
            "enabled": str(self.jizhang_enabled),
            "keyword": keyword_value,
            "domain": self.jizhang_domain,
        }
        # 保存绑定的记账配置ID列表
        if self.jizhang_configs:
            config["jizhang"]["configs"] = ",".join(self.jizhang_configs)

        config["msg_split"] = {"status": "1" if self.msg_split_enabled else "0"}
        config["msg"] = {"消息最多行数": str(self.msg_max_lines)}
        config["sleep_time"] = {"sec": str(self.msg_sleep_sec)}
        config["ack"] = {
            "timeout": str(self.ack_timeout),
            "max_retries": str(self.ack_max_retries),
        }
        config["cache"] = {"enabled": "1" if self.cache_enabled else "0"}
        config["mq"] = {"enabled": "1" if self.mq_enabled else "0"}
        config["thread"] = {"post": str(self.thread_post_count)}

        ini_path.parent.mkdir(parents=True, exist_ok=True)
        with open(ini_path, "w", encoding="gbk") as f:
            config.write(f)

    def to_dict(self) -> dict:
        return self.model_dump()
