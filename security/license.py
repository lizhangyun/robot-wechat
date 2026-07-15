"""
许可证验证 - 对应原软件 run.vef 许可证文件

功能:
  - 验证 run.vef 文件 (RSA 签名校验)
  - IP 白名单检查
  - 在线验证 (连接后端 API)
  - 离线验证 (本地缓存)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from config.settings import settings
from network.http_client import HttpClient
from security.crypto import CryptoUtils, crypto
from security.firewall import IPFirewall, ip_firewall


class LicenseError(Exception):
    """许可证错误"""


class LicenseManager:
    """
    许可证管理器

    验证流程:
      1. verify_file: 校验 run.vef 文件签名
      2. check_ip_whitelist: IP 白名单检查
      3. verify_online: 在线向后端校验 (可选)
      4. verify_offline: 本地缓存校验 (在线不可用时降级)
    """

    def __init__(
        self,
        vef_path: Optional[Path] = None,
        public_pem_path: Optional[Path] = None,
        firewall: Optional[IPFirewall] = None,
        http_client: Optional[HttpClient] = None,
    ) -> None:
        self.vef_path: Path = vef_path or (settings.data_dir / "run.vef")
        self.public_pem_path: Path = public_pem_path or (settings.data_dir / "public.pem")
        self._firewall: IPFirewall = firewall or ip_firewall
        self._http: HttpClient = http_client or HttpClient(timeout=10.0, max_retries=2)
        self._crypto: CryptoUtils = crypto
        # 本地缓存 (离线验证用)
        self._cache: dict = {}
        self._cache_path: Path = settings.data_dir / "license_cache.json"
        self._status: dict = {"valid": False, "reason": "未验证", "last_check": 0}

    # ======================== 文件验证 ========================

    def verify_file(self) -> dict:
        """
        验证 run.vef 文件

        文件格式 (JSON):
          {"license_id": "...", "issued_at": ..., "expire_at": ..., "signature": "..."}

        返回验证结果 dict: {valid, license_id, expire_at, reason}
        """
        if not self.vef_path.exists():
            return {"valid": False, "reason": f"许可证文件不存在: {self.vef_path}"}

        try:
            content = json.loads(self.vef_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return {"valid": False, "reason": f"许可证文件解析失败: {exc}"}

        license_id = content.get("license_id", "")
        issued_at = int(content.get("issued_at", 0))
        expire_at = int(content.get("expire_at", 0))
        signature = content.get("signature", "")

        # 校验 license_id 是否与 settings 匹配
        if settings.license_id and license_id and settings.license_id != license_id:
            return {"valid": False, "reason": "license_id 与配置不一致",
                    "license_id": license_id}

        # 校验过期
        now = int(time.time())
        if expire_at and now > expire_at:
            return {"valid": False, "reason": "许可证已过期",
                    "license_id": license_id, "expire_at": expire_at}

        # 校验签名
        if not self.public_pem_path.exists():
            # 无公钥时降级为仅校验内容 (开发模式)
            logger.warning("未找到公钥文件, 跳过签名校验 (开发模式)")
            result = {"valid": True, "license_id": license_id,
                       "issued_at": issued_at, "expire_at": expire_at,
                       "reason": "验证成功(无签名)"}
        else:
            public_pem = self.public_pem_path.read_text(encoding="utf-8")
            payload = f"{license_id}:{issued_at}"
            if self._crypto.rsa_verify(payload, signature, public_pem):
                result = {"valid": True, "license_id": license_id,
                          "issued_at": issued_at, "expire_at": expire_at,
                          "reason": "验证成功"}
            else:
                return {"valid": False, "reason": "许可证签名校验失败",
                        "license_id": license_id}

        # 更新本地缓存
        self._cache = result
        self._save_cache()
        return result

    # ======================== IP 白名单 ========================

    async def check_ip_whitelist(self, client_ip: str) -> bool:
        """
        检查 IP 是否在白名单中

        - 黑名单优先: 在黑名单中直接拒绝
        - 白名单启用时: 必须在白名单中
        """
        return await self._firewall.is_allowed(client_ip, settings.ip_whitelist_enabled)

    # ======================== 在线验证 ========================

    async def verify_online(self) -> dict:
        """
        在线向后端 API 校验许可证

        后端接口约定: POST {backend_url}/api/license/verify
        请求体: {"license_id": "..."}
        响应: {"valid": bool, "expire_at": ..., "message": "..."}
        """
        if not settings.backend_url:
            logger.info("未配置 backend_url, 跳过在线验证, 使用离线验证")
            return self.verify_offline()

        try:
            await self._http.open()
            payload = {"license_id": settings.license_id or self._cache.get("license_id", "")}
            headers = {}
            if settings.backend_token:
                headers["Authorization"] = f"Bearer {settings.backend_token}"
            resp = await self._http.post(
                "/api/license/verify", json=payload, headers=headers, max_retries=1
            )
            if resp.status_code != 200:
                logger.warning(f"在线验证返回状态码 {resp.status_code}, 降级离线验证")
                return self.verify_offline()
            data = resp.json()
            if data.get("valid"):
                self._cache = {
                    "valid": True,
                    "license_id": settings.license_id,
                    "expire_at": data.get("expire_at", 0),
                    "last_check": int(time.time()),
                    "reason": data.get("message", "在线验证成功"),
                }
                self._save_cache()
                self._status = self._cache
                return self._cache
            return {"valid": False, "reason": data.get("message", "在线验证失败")}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"在线验证异常, 降级离线验证: {exc}")
            return self.verify_offline()
        finally:
            await self._http.close()

    # ======================== 离线验证 ========================

    def verify_offline(self) -> dict:
        """
        离线验证 - 优先使用本地缓存, 缓存失效则验证文件

        缓存有效期: 24 小时 (在线验证成功后写入)
        """
        self._load_cache()
        now = int(time.time())
        cache_fresh = (
            self._cache.get("valid")
            and (now - int(self._cache.get("last_check", 0))) < 24 * 3600
        )
        if cache_fresh:
            return dict(self._cache)
        # 缓存失效或不存在, 回退到文件验证
        result = self.verify_file()
        result["last_check"] = now
        self._cache = result
        self._save_cache()
        return result

    # ======================== 综合验证 ========================

    async def verify(self, client_ip: Optional[str] = None) -> dict:
        """
        综合验证 (文件 + 在线/离线 + IP 白名单)

        返回: {valid, reason, ...}
        """
        # 1. 文件验证
        file_result = self.verify_file()
        if not file_result.get("valid"):
            self._status = file_result
            return file_result

        # 2. 在线/离线验证
        result = await self.verify_online()
        if not result.get("valid"):
            self._status = result
            return result

        # 3. IP 白名单
        if client_ip:
            allowed = await self.check_ip_whitelist(client_ip)
            if not allowed:
                return {"valid": False, "reason": f"IP 不在允许范围: {client_ip}"}

        self._status = result
        return result

    # ======================== 状态查询 ========================

    def get_status(self) -> dict:
        """获取最近一次验证状态"""
        return dict(self._status)

    # ======================== 缓存读写 ========================

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"保存许可证缓存失败: {exc}")


# 全局单例
license_manager = LicenseManager()
