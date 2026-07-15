"""
keyword AES 解密器 - 对应原软件 config.ini 中 [jizhang] 段的 keyword 字段

原软件（易语言）在 config.ini 的 [jizhang] 段保存了一个 keyword 字段，
该字段是 AES 加密后的十六进制字符串。运行时解密后包含：
  - 触发关键词列表 (trigger_words)
  - 银行/渠道名称白名单 (bank_whitelist)
  - 功能开关 (features)
  - 数据库加密密钥 (db_key)

不同实例 (c6801/c6802) 的 keyword 不同：c6801 的 keyword 较短，
c6802 的 keyword 更长（对应更多配置项）。

加密方式：AES-ECB + PKCS7 填充，密文以十六进制字符串存储。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Union

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from loguru import logger

# AES 块大小 (字节)
_BLOCK_SIZE = AES.block_size  # 16

# keyword 解密后默认返回的配置结构
_EMPTY_CONFIG: dict[str, Any] = {
    "trigger_words": [],
    "bank_whitelist": [],
    "db_key": "",
    "features": {},
}


class KeywordDecoder:
    """解密 config.ini 中 [jizhang] 段的 keyword 字段。

    对应原软件运行时对 keyword 的 AES 解密流程：
      1. 将十六进制字符串转换为字节串；
      2. 使用 AES-ECB 模式 + PKCS7 填充解密；
      3. 将解密后的明文解析为配置字典。

    Args:
        aes_key: AES 密钥 (16/24/32 字节)。不同实例使用不同密钥，
                 c6801 与 c6802 的密钥不同。
    """

    def __init__(self, aes_key: bytes) -> None:
        if not isinstance(aes_key, (bytes, bytearray)):
            raise TypeError("aes_key 必须为 bytes 类型")
        key_len = len(aes_key)
        if key_len not in (16, 24, 32):
            raise ValueError(
                f"AES 密钥长度必须为 16/24/32 字节, 当前为 {key_len} 字节"
            )
        self._aes_key: bytes = bytes(aes_key)

    # ------------------------------------------------------------------ #
    #  解密
    # ------------------------------------------------------------------ #
    def decrypt(self, hex_keyword: str) -> dict[str, Any]:
        """解密 keyword，返回配置字典。

        Args:
            hex_keyword: AES 加密的十六进制字符串（来自 config.ini 的 keyword 字段）。

        Returns:
            配置字典，结构为::

                {
                    "trigger_words": ["记账", "入账", ...],
                    "bank_whitelist": ["工商银行", "建设银行", "微信", "支付宝", ...],
                    "db_key": "数据库加密密钥",
                    "features": {"sync_enabled": True, "split_enabled": True, ...}
                }

            空字符串或解密失败时返回空配置（各字段为空默认值）。

        Raises:
            ValueError: 十六进制字符串非法或解密后数据格式无法识别时记录日志并返回空配置。
        """
        if not hex_keyword or not hex_keyword.strip():
            return {k: (list(v) if isinstance(v, list) else v) for k, v in _EMPTY_CONFIG.items()}

        hex_str = hex_keyword.strip()
        try:
            ciphertext = self._hex_to_bytes(hex_str)
        except ValueError as exc:
            logger.warning(f"keyword 十六进制解码失败: {exc}")
            return self._empty_config()

        try:
            cipher = AES.new(self._aes_key, AES.MODE_ECB)
            plaintext = unpad(cipher.decrypt(ciphertext), _BLOCK_SIZE)
        except (ValueError, KeyError) as exc:
            logger.warning(f"keyword AES 解密失败 (可能是密钥不匹配): {exc}")
            return self._empty_config()

        try:
            text = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            # 尝试 GBK 解码 (原软件为易语言，可能使用 GBK)
            try:
                text = plaintext.decode("gbk")
            except UnicodeDecodeError as exc:
                logger.warning(f"keyword 解密后文本解码失败: {exc}")
                return self._empty_config()

        return self._parse_plaintext(text)

    # ------------------------------------------------------------------ #
    #  加密
    # ------------------------------------------------------------------ #
    def encrypt(self, config: dict[str, Any]) -> str:
        """加密配置为 keyword 字段（反向操作）。

        将配置字典序列化为 JSON 明文，再 AES-ECB 加密，最后输出十六进制字符串。

        Args:
            config: 配置字典，应包含 trigger_words / bank_whitelist / db_key / features 等字段。

        Returns:
            AES 加密后的十六进制字符串（可直接写入 config.ini 的 keyword 字段）。
        """
        if not isinstance(config, dict):
            raise TypeError("config 必须为 dict 类型")

        plaintext = json.dumps(config, ensure_ascii=False).encode("utf-8")
        cipher = AES.new(self._aes_key, AES.MODE_ECB)
        ciphertext = cipher.encrypt(pad(plaintext, _BLOCK_SIZE))
        return ciphertext.hex()

    # ------------------------------------------------------------------ #
    #  加密/解密往返校验
    # ------------------------------------------------------------------ #
    def verify(self, hex_keyword: str) -> bool:
        """校验 keyword 是否可被当前密钥正确解密。

        Args:
            hex_keyword: AES 加密的十六进制字符串。

        Returns:
            是否解密成功。
        """
        result = self.decrypt(hex_keyword)
        # 空输入返回空配置不算成功
        if not hex_keyword or not hex_keyword.strip():
            return False
        return result != self._empty_config() or bool(result.get("trigger_words"))

    # ------------------------------------------------------------------ #
    #  内部方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _hex_to_bytes(hex_str: str) -> bytes:
        """十六进制字符串转字节串，容忍空白与大小写。"""
        cleaned = re.sub(r"\s", "", hex_str)
        if len(cleaned) % 2 != 0:
            raise ValueError(f"十六进制字符串长度为奇数: {len(cleaned)}")
        return bytes.fromhex(cleaned)

    @staticmethod
    def _empty_config() -> dict[str, Any]:
        """返回一份独立的空配置副本。"""
        return {
            "trigger_words": [],
            "bank_whitelist": [],
            "db_key": "",
            "features": {},
        }

    @staticmethod
    def _parse_plaintext(text: str) -> dict[str, Any]:
        """将解密后的明文解析为配置字典。

        支持两种格式：
          1. JSON 字符串（优先尝试）；
          2. 行式分段格式（兼容原软件风格），例如::

                [trigger_words]
                记账
                入账
                [bank_whitelist]
                工商银行
                建设银行
                [db_key]
                my_secret_key
                [features]
                sync_enabled=true
                split_enabled=1
        """
        result: dict[str, Any] = {
            "trigger_words": [],
            "bank_whitelist": [],
            "db_key": "",
            "features": {},
        }

        stripped = text.strip()
        if not stripped:
            return result

        # 尝试 JSON 解析
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    result["trigger_words"] = list(data.get("trigger_words", []))
                    result["bank_whitelist"] = list(data.get("bank_whitelist", []))
                    result["db_key"] = str(data.get("db_key", ""))
                    features = data.get("features", {})
                    if isinstance(features, dict):
                        result["features"] = dict(features)
                    return result
            except (json.JSONDecodeError, TypeError):
                pass  # 降级到行式解析

        # 行式分段解析
        return KeywordDecoder._parse_sectioned_text(text, result)

    @staticmethod
    def _parse_sectioned_text(
        text: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """解析行式分段格式的配置文本。"""
        current_section: Optional[str] = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # 段落标记 [section]
            section_match = re.match(r"^\[(.+)\]$", line)
            if section_match:
                current_section = section_match.group(1).strip().lower()
                continue

            if current_section is None:
                continue

            if current_section in ("trigger_words", "trigger", "keywords"):
                result["trigger_words"].append(line)
            elif current_section in ("bank_whitelist", "banks", "whitelist", "channels"):
                result["bank_whitelist"].append(line)
            elif current_section in ("db_key", "database_key", "dbkey"):
                result["db_key"] = line
            elif current_section in ("features", "feature", "switch", "switches"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    result["features"][k.strip()] = KeywordDecoder._parse_value(v.strip())
                else:
                    result["features"][line] = True
            # 未知段落忽略

        return result

    @staticmethod
    def _parse_value(value: str) -> Union[str, int, float, bool]:
        """将字符串值解析为合适的 Python 类型。"""
        low = value.lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        return value
