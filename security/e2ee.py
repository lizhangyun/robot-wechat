"""
E2EE 端到端加密客户端 - 对应原软件 e2eeE.com:8443 服务

原软件通过 ``e2eeE.com:8443`` 提供端到端加密服务，用于：
- 许可证验证（license verify）
- 配置同步（config sync）
- 安全通信（加密指令传输）

加密方案
========
采用 **TLS + AES 双重加密**：

1. **传输层**：通过 TLS（SSL）建立加密通道，保证网络传输安全；
2. **应用层**：对请求载荷再做一次 AES-CBC 加密，即使 TLS 被剥离
   也无法读取明文。

请求格式
========
每个请求包含防重放信息与签名::

    {
        "timestamp": 1700000000,        # Unix 时间戳（秒）
        "nonce": "a1b2c3...",           # 一次性随机数（防重放）
        "encrypted_payload": "base64",  # AES 加密后的业务数据
        "signature": "hmac_hex"         # HMAC-SHA256(timestamp + nonce + payload)
    }

服务端验证流程：
1. 校验 timestamp 在时间窗口内（防重放）；
2. 校验 nonce 唯一性（防重放）；
3. 校验 HMAC 签名（防篡改）；
4. AES 解密 payload，处理业务逻辑。

降级策略
========
- TLS 连接失败时，``connect`` 返回 False，各业务方法返回错误字典，
  不抛异常；
- :mod:`Crypto` (pycryptodome) 缺失时，``import`` 不报错，但加解密
  方法会返回降级错误。

典型用法
========

    client = E2EEClient(server="e2eeE.com", port=8443)
    if await client.connect():
        result = await client.verify_license("ROBOT3-XXXX")
        await client.sync_config("instance_1", {"key": "value"})
        await client.close()
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import ssl
from typing import Any, Optional

from loguru import logger

from config.settings import settings
from security.anti_replay import AntiReplayManager

# pycryptodome 为可选依赖（部分环境可能缺失），缺失时降级
try:
    from Crypto.Cipher import AES
    from Crypto.Random import get_random_bytes
    from Crypto.Util.Padding import pad, unpad
    import base64
    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CRYPTO_AVAILABLE = False
    AES = None  # type: ignore[assignment]
    get_random_bytes = None  # type: ignore[assignment]
    pad = None  # type: ignore[assignment]
    unpad = None  # type: ignore[assignment]
    base64 = None  # type: ignore[assignment]


# ====================================================================== #
#  异常定义
# ====================================================================== #
class E2EEError(Exception):
    """E2EE 端到端加密错误。"""


class E2EEConnectionError(E2EEError):
    """E2EE 连接错误。"""


# ====================================================================== #
#  E2EE 客户端
# ====================================================================== #
class E2EEClient:
    """端到端加密客户端，对应原软件 ``e2eeE.com:8443``。

    使用 TLS + AES 双重加密，结合防重放机制保障通信安全。

    Args:
        server: E2EE 服务器地址（默认 ``e2eeE.com``）。
        port: E2EE 服务器端口（默认 8443）。
        shared_secret: AES 加密与 HMAC 签名共用的共享密钥。
            默认取 ``settings.license_id`` 或 ``settings.app_id``。
        time_window: 防重放时间窗口（秒），默认 300。
        verify_tls: 是否校验 TLS 证书（生产环境应为 True）。
    """

    def __init__(
        self,
        server: str = "e2eeE.com",
        port: int = 8443,
        shared_secret: str = "",
        time_window: int = 300,
        verify_tls: bool = True,
    ) -> None:
        self.server: str = server
        self.port: int = int(port)
        self.shared_secret: str = shared_secret or settings.license_id or settings.app_id
        self.verify_tls: bool = verify_tls

        # 防重放管理器
        self._anti_replay: AntiReplayManager = AntiReplayManager(
            time_window=time_window
        )

        # 连接状态
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected: bool = False

        # AES 块大小
        self._block_size: int = 16

    # ------------------------------------------------------------------ #
    #  属性
    # ------------------------------------------------------------------ #
    @property
    def is_connected(self) -> bool:
        """是否已建立连接。"""
        return self._connected

    @property
    def address(self) -> str:
        """服务器地址（host:port）。"""
        return f"{self.server}:{self.port}"

    # ------------------------------------------------------------------ #
    #  连接管理
    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        """建立 TLS 加密连接。

        创建 SSL 上下文，通过 ``asyncio.open_connection`` 连接 E2EE 服务器。
        连接失败时返回 False 并记录日志，不抛异常。

        Returns:
            连接成功返回 True，失败返回 False。
        """
        if self._connected:
            logger.debug("E2EE 已连接，跳过重复连接")
            return True

        try:
            ssl_context = self._create_ssl_context()
            logger.info(f"E2EE 正在连接 {self.address} (TLS={self.verify_tls})")
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=self.server,
                    port=self.port,
                    ssl=ssl_context,
                    server_hostname=self.server if self.verify_tls else None,
                ),
                timeout=15.0,
            )
            self._connected = True
            logger.info(f"E2EE 连接成功 {self.address}")
            return True
        except asyncio.TimeoutError:
            logger.error(f"E2EE 连接超时 {self.address}")
            return False
        except OSError as e:
            logger.error(f"E2EE 连接失败 {self.address}: {e}")
            return False
        except Exception as e:  # noqa: BLE001
            logger.exception(f"E2EE 连接异常: {e}")
            return False

    async def close(self) -> None:
        """关闭连接。"""
        self._connected = False
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"E2EE 关闭连接异常: {e}")
            finally:
                self._writer = None
                self._reader = None
        logger.info("E2EE 连接已关闭")

    def _create_ssl_context(self) -> ssl.SSLContext:
        """创建 SSL 上下文。

        ``verify_tls=True`` 时校验证书，``False`` 时跳过校验（仅用于测试）。
        """
        if self.verify_tls:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # ------------------------------------------------------------------ #
    #  加密 / 签名
    # ------------------------------------------------------------------ #
    def _derive_key(self) -> bytes:
        """从共享密钥派生 32 字节 AES-256 密钥。

        使用 SHA-256 派生固定长度密钥。
        """
        return hashlib.sha256(self.shared_secret.encode("utf-8")).digest()

    def _encrypt_payload(self, data: dict) -> str:
        """AES-CBC 加密业务数据，返回 base64 字符串。

        格式：base64(IV + ciphertext)。

        Args:
            data: 业务数据字典。

        Returns:
            base64 编码的 ``IV + 密文``。

        Raises:
            E2EEError: pycryptodome 不可用时。
        """
        if not _CRYPTO_AVAILABLE:
            raise E2EEError("pycryptodome 不可用，无法加密")
        plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
        key = self._derive_key()
        iv = get_random_bytes(self._block_size)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(pad(plaintext, self._block_size))
        blob = iv + ciphertext
        return base64.b64encode(blob).decode("ascii")

    def _decrypt_payload(self, encrypted: str) -> dict:
        """AES-CBC 解密业务数据。

        Args:
            encrypted: base64 编码的 ``IV + 密文``。

        Returns:
            解密后的业务数据字典。

        Raises:
            E2EEError: 解密失败或 pycryptodome 不可用。
        """
        if not _CRYPTO_AVAILABLE:
            raise E2EEError("pycryptodome 不可用，无法解密")
        blob = base64.b64decode(encrypted)
        iv = blob[: self._block_size]
        ciphertext = blob[self._block_size :]
        key = self._derive_key()
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = unpad(cipher.decrypt(ciphertext), self._block_size)
        text = plaintext.decode("utf-8")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"raw": text}

    def _sign(self, timestamp: int, nonce: str, encrypted_payload: str) -> str:
        """对请求生成 HMAC-SHA256 签名。

        签名内容为 ``timestamp + nonce + encrypted_payload``，
        使用共享密钥作为 HMAC 密钥。

        Args:
            timestamp: 时间戳。
            nonce: 随机数。
            encrypted_payload: 加密后的载荷。

        Returns:
            十六进制 HMAC 签名字符串。
        """
        key = self.shared_secret.encode("utf-8")
        msg = f"{timestamp}{nonce}{encrypted_payload}".encode("utf-8")
        return hmac.new(key, msg, hashlib.sha256).hexdigest()

    def _verify_signature(
        self, timestamp: int, nonce: str, encrypted_payload: str, signature: str
    ) -> bool:
        """验证 HMAC 签名。

        Args:
            timestamp: 时间戳。
            nonce: 随机数。
            encrypted_payload: 加密后的载荷。
            signature: 待验证的签名。

        Returns:
            签名匹配返回 True。
        """
        expected = self._sign(timestamp, nonce, encrypted_payload)
        return hmac.compare_digest(expected, signature)

    def _build_request(self, data: dict) -> dict:
        """构建带防重放信息与签名的加密请求。

        Args:
            data: 业务数据字典。

        Returns:
            完整请求::

                {
                    "timestamp": ...,
                    "nonce": "...",
                    "encrypted_payload": "base64...",
                    "signature": "hmac_hex"
                }
        """
        timestamp = self._anti_replay.generate_timestamp()
        nonce = self._anti_replay.generate_nonce()
        encrypted_payload = self._encrypt_payload(data)
        signature = self._sign(timestamp, nonce, encrypted_payload)
        return {
            "timestamp": timestamp,
            "nonce": nonce,
            "encrypted_payload": encrypted_payload,
            "signature": signature,
        }

    def _parse_response(self, response: dict) -> dict:
        """解析服务端响应，验证签名并解密载荷。

        Args:
            response: 服务端返回的响应字典。

        Returns:
            解密后的业务数据字典。验证失败时返回错误字典。
        """
        if not isinstance(response, dict):
            return {"code": -1, "msg": "响应格式错误"}

        # 检查是否为错误响应（无加密载荷）
        if "encrypted_payload" not in response:
            return {
                "code": response.get("code", -1),
                "msg": response.get("msg", "未知错误"),
            }

        timestamp = response.get("timestamp", 0)
        nonce = response.get("nonce", "")
        encrypted_payload = response.get("encrypted_payload", "")
        signature = response.get("signature", "")

        # 验证签名
        if not self._verify_signature(timestamp, nonce, encrypted_payload, signature):
            logger.warning("E2EE 响应签名校验失败")
            return {"code": -1, "msg": "响应签名校验失败"}

        # 解密载荷
        try:
            return self._decrypt_payload(encrypted_payload)
        except E2EEError as e:
            return {"code": -1, "msg": str(e)}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"E2EE 响应解密失败: {e}")
            return {"code": -1, "msg": f"响应解密失败: {e}"}

    # ------------------------------------------------------------------ #
    #  核心通信
    # ------------------------------------------------------------------ #
    async def send_encrypted(self, data: dict) -> dict:
        """发送加密数据并接收响应。

        将业务数据加密、签名、附带防重放信息后发送到 E2EE 服务，
        接收响应并解密返回。

        通信协议：每条消息为单行 JSON，以 ``\\n`` 分隔。

        Args:
            data: 业务数据字典。

        Returns:
            服务端响应（解密后的业务数据字典）。
            连接失败或异常时返回 ``{"code": -1, "msg": "..."}``。
        """
        if not self._connected or self._writer is None or self._reader is None:
            return {"code": -1, "msg": "未连接 E2EE 服务"}

        if not _CRYPTO_AVAILABLE:
            return {"code": -1, "msg": "pycryptodome 不可用，无法加密通信"}

        try:
            request = self._build_request(data)
            request_json = json.dumps(request, ensure_ascii=False) + "\n"
            self._writer.write(request_json.encode("utf-8"))
            await self._writer.drain()
            logger.debug(f"E2EE 已发送请求 nonce={request['nonce'][:16]}...")

            # 读取响应（单行 JSON）
            raw = await asyncio.wait_for(
                self._reader.readline(), timeout=30.0
            )
            if not raw:
                logger.warning("E2EE 服务关闭连接")
                self._connected = False
                return {"code": -1, "msg": "服务关闭连接"}

            response = json.loads(raw.decode("utf-8", errors="replace"))
            return self._parse_response(response)
        except asyncio.TimeoutError:
            logger.error("E2EE 响应超时")
            return {"code": -1, "msg": "响应超时"}
        except json.JSONDecodeError as e:
            logger.error(f"E2EE 响应 JSON 解析失败: {e}")
            return {"code": -1, "msg": f"响应解析失败: {e}"}
        except (ConnectionError, OSError) as e:
            logger.error(f"E2EE 连接异常: {e}")
            self._connected = False
            return {"code": -1, "msg": f"连接异常: {e}"}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"E2EE 发送异常: {e}")
            return {"code": -1, "msg": str(e)}

    # ------------------------------------------------------------------ #
    #  业务接口
    # ------------------------------------------------------------------ #
    async def verify_license(self, license_id: str) -> dict:
        """验证许可证。

        向 E2EE 服务发送许可证 ID，服务端返回验证结果。

        Args:
            license_id: 许可证 ID。

        Returns:
            验证结果字典，通常含::

                {
                    "code": 0,
                    "valid": True/False,
                    "expire_at": 1700000000,
                    "msg": "..."
                }
        """
        logger.info(f"E2EE 验证许可证: {license_id[:20]}...")
        return await self.send_encrypted(
            {"action": "verify_license", "license_id": license_id}
        )

    async def sync_config(self, instance_id: str, config: dict) -> dict:
        """同步配置到 E2EE 服务。

        将实例配置加密上传到 E2EE 服务，服务端返回同步结果。

        Args:
            instance_id: 机器人实例ID。
            config: 配置字典。

        Returns:
            同步结果字典，通常含::

                {
                    "code": 0,
                    "synced": True/False,
                    "version": "...",
                    "msg": "..."
                }
        """
        logger.info(f"E2EE 同步配置: instance={instance_id}")
        return await self.send_encrypted(
            {
                "action": "sync_config",
                "instance_id": instance_id,
                "config": config,
            }
        )

    async def fetch_config(self, instance_id: str) -> dict:
        """从 E2EE 服务拉取配置。

        Args:
            instance_id: 机器人实例ID。

        Returns:
            配置字典（含 ``code`` / ``config`` 字段）。
        """
        logger.info(f"E2EE 拉取配置: instance={instance_id}")
        return await self.send_encrypted(
            {"action": "fetch_config", "instance_id": instance_id}
        )

    async def heartbeat(self, instance_id: str) -> dict:
        """发送心跳。

        Args:
            instance_id: 机器人实例ID。

        Returns:
            心跳响应字典。
        """
        return await self.send_encrypted(
            {"action": "heartbeat", "instance_id": instance_id}
        )

    # ------------------------------------------------------------------ #
    #  上下文管理器
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "E2EEClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


# ====================================================================== #
#  全局单例
# ====================================================================== #
e2ee_client: E2EEClient = E2EEClient()
"""E2EE 客户端全局单例。"""


def create_e2ee_client(
    server: str = "e2eeE.com",
    port: int = 8443,
    shared_secret: str = "",
) -> E2EEClient:
    """创建 E2EE 客户端的便捷工厂。

    Args:
        server: E2EE 服务器地址。
        port: 端口。
        shared_secret: 共享密钥。

    Returns:
        :class:`E2EEClient` 实例。
    """
    return E2EEClient(server=server, port=port, shared_secret=shared_secret)


# ====================================================================== #
#  自测入口
# ====================================================================== #
def _self_test() -> None:
    """E2EE 客户端自测（不连接真实服务）。"""
    client = E2EEClient(
        server="e2eeE.com",
        port=8443,
        shared_secret="e2ee_test_secret",
        verify_tls=False,
    )
    logger.info(f"E2EE 客户端创建: {client.address}, connected={client.is_connected}")

    # 测试加密往返（不经过网络）
    if _CRYPTO_AVAILABLE:
        data = {"action": "test", "msg": "你好 E2EE"}
        encrypted = client._encrypt_payload(data)
        decrypted = client._decrypt_payload(encrypted)
        assert decrypted == data, f"加解密往返失败: {decrypted}"
        logger.info("E2EE AES 加解密往返成功")

        # 测试签名
        ts = 1700000000
        nonce = "test_nonce_123"
        sig = client._sign(ts, nonce, encrypted)
        assert client._verify_signature(ts, nonce, encrypted, sig) is True
        assert client._verify_signature(ts, nonce, encrypted + "x", sig) is False
        logger.info("E2EE HMAC 签名验证成功")

        # 测试请求构建
        request = client._build_request(data)
        assert "timestamp" in request
        assert "nonce" in request
        assert "encrypted_payload" in request
        assert "signature" in request
        logger.info("E2EE 请求构建成功")
    else:
        logger.warning("pycryptodome 不可用，跳过加解密测试")

    logger.info("E2EE 自测完成")


if __name__ == "__main__":
    _self_test()
