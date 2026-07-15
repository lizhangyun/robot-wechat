"""
加密工具模块 - 对应原软件 keyword 字段加密与 run.vef 许可证签名
提供 AES 配置加密/解密、密钥生成、RSA 签名验证
"""
from __future__ import annotations

import base64
import json
import secrets
import uuid
from pathlib import Path
from typing import Any, Optional, Union

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes
from Crypto.Signature import pkcs1_15
from Crypto.Util.Padding import pad, unpad
from loguru import logger

from config.settings import settings

# AES 密钥派生所用的盐与迭代次数 (与原软件保持一致的派生方式)
_KDF_SALT = b"robot3_replica_salt_v1"
_KDF_ITERATIONS = 100_000
# AES 块大小 (字节)
_BLOCK_SIZE = AES.block_size  # 16


def _derive_key(secret: str) -> bytes:
    """根据主密钥派生 32 字节 AES-256 密钥"""
    secret = secret or settings.license_id or settings.app_id
    return PBKDF2(secret.encode("utf-8"), _KDF_SALT, dkLen=32, count=_KDF_ITERATIONS)


class CryptoUtils:
    """
    加密工具集合

    - encrypt_config / decrypt_config: 对应原软件 instance config 中的 keyword 字段
      keyword 字段保存的是 AES 加密后的功能配置 JSON
    - generate_key: 生成随机 AES 密钥
    - generate_license_id: 生成许可证 ID (形如 run.vef 中的标识)
    - RSA 签名/验签: 用于许可证文件防篡改校验
    """

    def __init__(self, master_secret: Optional[str] = None) -> None:
        self._master_secret: str = master_secret or settings.license_id or settings.app_id

    # ======================== AES 配置加密 ========================

    def encrypt_config(self, data: Union[dict, str, bytes], secret: Optional[str] = None) -> str:
        """
        加密配置数据, 返回 base64 字符串 (IV + 密文)

        对应原软件 keyword 字段: 明文为功能配置 JSON, 密文存储在 ini 的 keyword 项
        """
        try:
            if isinstance(data, dict):
                plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
            elif isinstance(data, str):
                plaintext = data.encode("utf-8")
            else:
                plaintext = data

            key = _derive_key(secret or self._master_secret)
            iv = get_random_bytes(_BLOCK_SIZE)
            cipher = AES.new(key, AES.MODE_CBC, iv)
            ciphertext = cipher.encrypt(pad(plaintext, _BLOCK_SIZE))
            # 将 IV 拼接到密文前, 便于解密时取回
            blob = iv + ciphertext
            return base64.b64encode(blob).decode("ascii")
        except Exception as exc:  # pragma: no cover - 异常兜底
            logger.error(f"加密配置失败: {exc}")
            raise

    def decrypt_config(self, encrypted: str, secret: Optional[str] = None) -> Any:
        """
        解密配置数据

        若解密结果为合法 JSON 则返回 dict, 否则返回原始字符串
        """
        try:
            blob = base64.b64decode(encrypted)
            iv, ciphertext = blob[:_BLOCK_SIZE], blob[_BLOCK_SIZE:]
            key = _derive_key(secret or self._master_secret)
            cipher = AES.new(key, AES.MODE_CBC, iv)
            plaintext = unpad(cipher.decrypt(ciphertext), _BLOCK_SIZE)
            text = plaintext.decode("utf-8")
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text
        except Exception as exc:  # pragma: no cover - 异常兜底
            logger.error(f"解密配置失败: {exc}")
            raise

    # ======================== 密钥 / 许可证 ID 生成 ========================

    @staticmethod
    def generate_key(length: int = 32) -> str:
        """生成随机 AES 密钥 (hex 字符串)"""
        return secrets.token_hex(length)

    @staticmethod
    def generate_license_id() -> str:
        """
        生成许可证 ID (对应 run.vef 文件中的标识)

        格式: ROBOT3-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
        """
        short = uuid.uuid4().hex[:8].upper()
        tail = uuid.uuid4()
        return f"ROBOT3-{short}-{str(tail).upper()}"

    # ======================== RSA 签名 ========================

    def generate_rsa_keypair(self, bits: int = 2048) -> tuple[str, str]:
        """生成 RSA 密钥对, 返回 (private_pem, public_pem)"""
        key = RSA.generate(bits)
        private_pem = key.export_key().decode("ascii")
        public_pem = key.publickey().export_key().decode("ascii")
        return private_pem, public_pem

    def rsa_sign(self, data: Union[str, bytes], private_pem: str) -> str:
        """使用私钥对数据签名, 返回 base64 签名"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        key = RSA.import_key(private_pem)
        h = SHA256.new(data)
        signature = pkcs1_15.new(key).sign(h)
        return base64.b64encode(signature).decode("ascii")

    def rsa_verify(self, data: Union[str, bytes], signature_b64: str, public_pem: str) -> bool:
        """使用公钥验证签名, 验证通过返回 True"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            key = RSA.import_key(public_pem)
            h = SHA256.new(data)
            signature = base64.b64decode(signature_b64)
            pkcs1_15.new(key).verify(h, signature)
            return True
        except (ValueError, TypeError):
            return False


# 全局单例, 供 license / firewall 等模块复用
crypto = CryptoUtils()


def create_license_file(vef_path: Path, license_id: str, public_pem: str, private_pem: str) -> None:
    """
    生成一个示例 run.vef 许可证文件 (用于首次部署/测试)

    文件格式 (JSON):
      {"license_id": "...", "issued_at": ..., "signature": "..."}
    签名内容为 license_id + issued_at
    """
    import time

    issued_at = int(time.time())
    payload = f"{license_id}:{issued_at}"
    signature = CryptoUtils().rsa_sign(payload, private_pem)
    content = {
        "license_id": license_id,
        "issued_at": issued_at,
        "expire_at": issued_at + 365 * 24 * 3600,
        "signature": signature,
    }
    vef_path.parent.mkdir(parents=True, exist_ok=True)
    vef_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    # 保存公钥用于验签
    (vef_path.parent / "public.pem").write_text(public_pem, encoding="utf-8")
    logger.info(f"已生成许可证文件: {vef_path}")
