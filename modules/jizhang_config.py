"""
记账多配置管理 - 对应原软件 data/app/jizhang_c1/, data/app/jizhang_c12/ 等多配置目录

原软件支持多套独立记账配置并行运行：
  - data/app/jizhang_c1/   : 记账配置 1
  - data/app/jizhang_c12/  : 记账配置 12
  - 每套配置有独立的 config.ini (GBK 编码) 和 keyword (AES 加密)
  - jizhang_c1 和 jizhang_c12 可能对应不同的记账场景或群组集合

后端同步地址 (domain) 按实例不同：
  - c6801 后端: http://jacn1.huoxing111.com/6802cishi/
  - c6802 后端: https://jizhang105.tztz.eu.org/6802cishi/
  - URL 路径 /6802cishi/ 为固定后缀
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# 独立运行支持：将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger

from config.gbk_config import GBKConfigParser
from security.keyword_decoder import KeywordDecoder

# 固定后端路径后缀 (原软件所有记账后端共用)
_BACKEND_PATH_SUFFIX = "/6802cishi/"

# 默认触发关键词
_DEFAULT_TRIGGER_WORDS: list[str] = ["记账"]

# 默认银行白名单 (为空表示不限制)
_DEFAULT_BANK_WHITELIST: list[str] = []


@dataclass
class JizhangConfig:
    """单套记账配置。

    对应原软件 ``data/app/jizhang_{config_id}/config.ini`` 解析后的配置。

    Attributes:
        config_id: 配置标识（如 "c1", "c12"）。
        trigger_words: 触发关键词列表（来自 keyword 解密后的 trigger_words 字段）。
        bank_whitelist: 银行/渠道名称白名单（仅允许白名单中的渠道记账）。
        domain: 后端 API 地址（如 ``http://jacn1.huoxing111.com/6802cishi/``）。
        enabled: 是否启用此配置。
        db_key: 数据库加密密钥（来自 keyword 解密后的 db_key 字段）。
        features: 功能开关字典（来自 keyword 解密后的 features 字段）。
        keyword_hex: 原始 keyword 十六进制字符串（AES 加密）。
        instance_id: 关联的机器人实例 ID（如 "c6801", "c6802"）。
    """

    config_id: str = ""
    trigger_words: list[str] = field(default_factory=lambda: list(_DEFAULT_TRIGGER_WORDS))
    bank_whitelist: list[str] = field(default_factory=lambda: list(_DEFAULT_BANK_WHITELIST))
    domain: str = ""
    enabled: bool = True
    db_key: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    keyword_hex: str = ""
    instance_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "config_id": self.config_id,
            "trigger_words": list(self.trigger_words),
            "bank_whitelist": list(self.bank_whitelist),
            "domain": self.domain,
            "enabled": self.enabled,
            "db_key": self.db_key,
            "features": dict(self.features),
            "keyword_hex": self.keyword_hex,
            "instance_id": self.instance_id,
        }

    def to_keyword_config(self) -> dict[str, Any]:
        """转为 KeywordDecoder 可加密的配置字典。"""
        return {
            "trigger_words": list(self.trigger_words),
            "bank_whitelist": list(self.bank_whitelist),
            "db_key": self.db_key,
            "features": dict(self.features),
        }


class JizhangConfigManager:
    """管理多套记账配置。

    对应原软件 ``data/app/`` 目录下 ``jizhang_c1/``, ``jizhang_c12/`` 等
    独立记账配置目录的加载与管理。

    Args:
        base_path: 配置根目录（默认 ``data/app``）。
        aes_key: AES 解密密钥。不同实例使用不同密钥；
                 传入空 bytes 时跳过 keyword 解密。
    """

    def __init__(
        self,
        base_path: str = "data/app",
        aes_key: Optional[bytes] = None,
    ) -> None:
        self.base_path: Path = Path(base_path)
        self._aes_key: Optional[bytes] = aes_key
        self._decoder: Optional[KeywordDecoder] = None
        if aes_key and len(aes_key) in (16, 24, 32):
            self._decoder = KeywordDecoder(aes_key)
        self._cache: dict[str, JizhangConfig] = {}

    # ------------------------------------------------------------------ #
    #  加载
    # ------------------------------------------------------------------ #
    def load_config(self, config_id: str) -> JizhangConfig:
        """加载指定配置。

        从 ``{base_path}/jizhang_{config_id}/config.ini`` 读取 GBK 编码的 INI 文件，
        解析 [jizhang] 段的 keyword 字段（AES 解密后提取触发词、白名单等）。

        Args:
            config_id: 配置标识（如 "c1", "c12"）。

        Returns:
            解析后的 :class:`JizhangConfig`。文件不存在时返回默认配置。
        """
        if config_id in self._cache:
            return self._cache[config_id]

        config_dir = self.base_path / f"jizhang_{config_id}"
        ini_path = config_dir / "config.ini"

        if not ini_path.exists():
            logger.warning(f"记账配置文件不存在: {ini_path}")
            return JizhangConfig(config_id=config_id, enabled=False)

        # 使用 GBK 配置解析器读取
        parser = GBKConfigParser()
        parser.read(str(ini_path), encoding="gbk")

        # 读取基础字段
        domain = parser.get("jizhang", "domain", fallback="") or ""
        enabled_str = parser.get("jizhang", "enabled", fallback="true")
        enabled = str(enabled_str).strip().lower() in ("true", "1", "yes", "on")
        keyword_hex = parser.get("jizhang", "keyword", fallback="") or ""

        # 从实例目录名推断 instance_id (如 jizhang_c1 的父目录可能包含 c6801)
        instance_id = parser.get("jizhang", "instance_id", fallback="") or ""

        # 解密 keyword 提取配置
        trigger_words = list(_DEFAULT_TRIGGER_WORDS)
        bank_whitelist: list[str] = []
        db_key = ""
        features: dict[str, Any] = {}

        if self._decoder and keyword_hex:
            try:
                decoded = self._decoder.decrypt(keyword_hex)
                if decoded.get("trigger_words"):
                    trigger_words = list(decoded["trigger_words"])
                if decoded.get("bank_whitelist"):
                    bank_whitelist = list(decoded["bank_whitelist"])
                if decoded.get("db_key"):
                    db_key = str(decoded["db_key"])
                if decoded.get("features"):
                    features = dict(decoded["features"])
                logger.debug(
                    f"记账配置 {config_id} keyword 解密成功: "
                    f"{len(trigger_words)} 触发词, {len(bank_whitelist)} 白名单"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"记账配置 {config_id} keyword 解密失败: {exc}")
        else:
            # 无解密器或无 keyword，尝试从 INI 直接读取白名单 (兼容格式)
            raw_banks = parser.get("jizhang", "bank_whitelist", fallback="")
            if raw_banks:
                bank_whitelist = [
                    b.strip() for b in raw_banks.split(",") if b.strip()
                ]
            raw_triggers = parser.get("jizhang", "trigger_words", fallback="")
            if raw_triggers:
                trigger_words = [
                    t.strip() for t in raw_triggers.split(",") if t.strip()
                ]

        # 确保 domain 以固定后缀结尾
        if domain and not domain.endswith(_BACKEND_PATH_SUFFIX):
            domain = domain.rstrip("/") + _BACKEND_PATH_SUFFIX

        config = JizhangConfig(
            config_id=config_id,
            trigger_words=trigger_words,
            bank_whitelist=bank_whitelist,
            domain=domain,
            enabled=enabled,
            db_key=db_key,
            features=features,
            keyword_hex=keyword_hex,
            instance_id=instance_id,
        )

        self._cache[config_id] = config
        logger.info(
            f"已加载记账配置 {config_id}: enabled={enabled}, "
            f"domain={domain or '(无)'}, triggers={trigger_words}"
        )
        return config

    def load_all(self) -> dict[str, JizhangConfig]:
        """加载所有记账配置。

        扫描 ``{base_path}`` 下所有 ``jizhang_*`` 目录，逐个加载配置。

        Returns:
            ``{config_id: JizhangConfig}`` 字典。
        """
        result: dict[str, JizhangConfig] = {}

        if not self.base_path.exists():
            logger.debug(f"记账配置根目录不存在: {self.base_path}")
            return result

        for item in sorted(self.base_path.iterdir()):
            if not item.is_dir():
                continue
            if not item.name.startswith("jizhang_"):
                continue
            # 提取 config_id: jizhang_c1 -> c1
            config_id = item.name[len("jizhang_"):]
            if not config_id:
                continue
            try:
                config = self.load_config(config_id)
                result[config_id] = config
            except Exception as exc:  # noqa: BLE001
                logger.error(f"加载记账配置 {config_id} 失败: {exc}")

        logger.info(f"共加载 {len(result)} 套记账配置: {list(result.keys())}")
        return result

    # ------------------------------------------------------------------ #
    #  保存
    # ------------------------------------------------------------------ #
    def save_config(self, config_id: str, config: JizhangConfig) -> None:
        """保存配置。

        将配置写入 ``{base_path}/jizhang_{config_id}/config.ini`` (GBK 编码)。
        若有解密器，则将 trigger_words / bank_whitelist / db_key / features
        加密为 keyword 字段。

        Args:
            config_id: 配置标识。
            config: 配置对象。
        """
        config_dir = self.base_path / f"jizhang_{config_id}"
        config_dir.mkdir(parents=True, exist_ok=True)
        ini_path = config_dir / "config.ini"

        parser = GBKConfigParser()

        # 加密 keyword (若有解密器)
        keyword_hex = config.keyword_hex
        if self._decoder:
            try:
                keyword_hex = self._decoder.encrypt(config.to_keyword_config())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"记账配置 {config_id} keyword 加密失败: {exc}")

        parser.set("jizhang", "enabled", "1" if config.enabled else "0")
        parser.set("jizhang", "domain", config.domain)
        parser.set("jizhang", "keyword", keyword_hex)
        if config.instance_id:
            parser.set("jizhang", "instance_id", config.instance_id)
        # 兼容格式：同时写入明文白名单和触发词（供无解密器时读取）
        if config.bank_whitelist:
            parser.set("jizhang", "bank_whitelist", ",".join(config.bank_whitelist))
        if config.trigger_words:
            parser.set("jizhang", "trigger_words", ",".join(config.trigger_words))

        parser.save(str(ini_path), encoding="gbk")

        # 更新缓存
        config.keyword_hex = keyword_hex
        self._cache[config_id] = config
        logger.info(f"已保存记账配置 {config_id} -> {ini_path}")

    # ------------------------------------------------------------------ #
    #  创建
    # ------------------------------------------------------------------ #
    def create_config(
        self, config_id: str, **kwargs: Any
    ) -> JizhangConfig:
        """创建新配置。

        Args:
            config_id: 配置标识。
            **kwargs: :class:`JizhangConfig` 的字段值（如 domain, trigger_words 等）。

        Returns:
            新创建的配置对象（已保存到磁盘）。
        """
        config = JizhangConfig(config_id=config_id)
        # 应用传入的字段
        for key, value in kwargs.items():
            if hasattr(config, key):
                if key in ("trigger_words", "bank_whitelist"):
                    setattr(config, key, list(value))
                elif key == "features":
                    setattr(config, key, dict(value))
                else:
                    setattr(config, key, value)

        # 确保 domain 格式正确
        if config.domain and not config.domain.endswith(_BACKEND_PATH_SUFFIX):
            config.domain = config.domain.rstrip("/") + _BACKEND_PATH_SUFFIX

        self.save_config(config_id, config)
        return config

    # ------------------------------------------------------------------ #
    #  删除
    # ------------------------------------------------------------------ #
    def delete_config(self, config_id: str) -> bool:
        """删除配置目录。

        Args:
            config_id: 配置标识。

        Returns:
            是否删除成功。
        """
        import shutil

        config_dir = self.base_path / f"jizhang_{config_id}"
        if not config_dir.exists():
            return False

        try:
            shutil.rmtree(str(config_dir))
            self._cache.pop(config_id, None)
            logger.info(f"已删除记账配置 {config_id}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"删除记账配置 {config_id} 失败: {exc}")
            return False

    # ------------------------------------------------------------------ #
    #  缓存管理
    # ------------------------------------------------------------------ #
    def clear_cache(self) -> None:
        """清空内存缓存，下次 load 时重新读取磁盘。"""
        self._cache.clear()

    def list_config_ids(self) -> list[str]:
        """列出所有可用的配置 ID（扫描磁盘）。"""
        if not self.base_path.exists():
            return []
        ids: list[str] = []
        for item in sorted(self.base_path.iterdir()):
            if item.is_dir() and item.name.startswith("jizhang_"):
                cid = item.name[len("jizhang_"):]
                if cid:
                    ids.append(cid)
        return ids
