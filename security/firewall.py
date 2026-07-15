"""
IP 防火墙 - 黑/白名单管理 (持久化到数据库)

支持:
  - 黑名单 (add_black_ip / remove_black_ip / is_black_ip)
  - 白名单 (add_white_ip / is_white_ip)
  - CIDR 格式匹配
  - 内存缓存 + 数据库持久化
"""
from __future__ import annotations

import ipaddress
import time
from typing import Optional

from loguru import logger

from database.manager import DatabaseManager, db_manager


class IPFirewall:
    """IP 防火墙 (黑/白名单)"""

    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self._db = db or db_manager
        # 内存缓存, 启动时从数据库加载
        self._black_ips: list[str] = []
        self._white_ips: list[str] = []
        self._loaded: bool = False

    async def _ensure_loaded(self) -> None:
        """从数据库加载名单到内存缓存 (仅一次)"""
        if self._loaded:
            return
        try:
            self._black_ips = await self._db.list_firewall_ips("firewall_black")
            self._white_ips = await self._db.list_firewall_ips("firewall_white")
            self._loaded = True
            logger.info(f"防火墙名单已加载: 黑名单 {len(self._black_ips)} 条, "
                        f"白名单 {len(self._white_ips)} 条")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"加载防火墙名单失败: {exc}")
            self._loaded = True  # 避免反复尝试

    # ======================== 黑名单 ========================

    async def add_black_ip(self, ip: str, note: str = "") -> bool:
        """添加黑名单 IP/CIDR, 返回是否新增成功"""
        await self._ensure_loaded()
        if not self._validate_ip_or_cidr(ip):
            logger.warning(f"无效的 IP/CIDR: {ip}")
            return False
        ok = await self._db.add_firewall_ip("firewall_black", ip, note)
        if ok:
            self._black_ips.append(ip)
            logger.info(f"已添加黑名单 IP: {ip}")
        return ok

    async def remove_black_ip(self, ip: str) -> bool:
        """移除黑名单 IP/CIDR"""
        await self._ensure_loaded()
        ok = await self._db.remove_firewall_ip("firewall_black", ip)
        if ok and ip in self._black_ips:
            self._black_ips.remove(ip)
            logger.info(f"已移除黑名单 IP: {ip}")
        return ok

    async def is_black_ip(self, ip: str) -> bool:
        """判断 IP 是否在黑名单中 (支持 CIDR 匹配)"""
        await self._ensure_loaded()
        return self._match_any(ip, self._black_ips)

    # ======================== 白名单 ========================

    async def add_white_ip(self, ip: str, note: str = "") -> bool:
        """添加白名单 IP/CIDR"""
        await self._ensure_loaded()
        if not self._validate_ip_or_cidr(ip):
            logger.warning(f"无效的 IP/CIDR: {ip}")
            return False
        ok = await self._db.add_firewall_ip("firewall_white", ip, note)
        if ok:
            self._white_ips.append(ip)
            logger.info(f"已添加白名单 IP: {ip}")
        return ok

    async def remove_white_ip(self, ip: str) -> bool:
        """移除白名单 IP/CIDR"""
        await self._ensure_loaded()
        ok = await self._db.remove_firewall_ip("firewall_white", ip)
        if ok and ip in self._white_ips:
            self._white_ips.remove(ip)
        return ok

    async def is_white_ip(self, ip: str) -> bool:
        """判断 IP 是否在白名单中"""
        await self._ensure_loaded()
        return self._match_any(ip, self._white_ips)

    # ======================== 综合判断 ========================

    async def is_allowed(self, ip: str, whitelist_enabled: bool = False) -> bool:
        """
        综合判断 IP 是否允许访问

        规则:
          1. 在黑名单中 -> 拒绝
          2. 白名单启用时: 不在白名单中 -> 拒绝
          3. 否则 -> 允许
        """
        if await self.is_black_ip(ip):
            return False
        if whitelist_enabled and not await self.is_white_ip(ip):
            return False
        return True

    async def list_black(self) -> list[str]:
        await self._ensure_loaded()
        return list(self._black_ips)

    async def list_white(self) -> list[str]:
        await self._ensure_loaded()
        return list(self._white_ips)

    # ======================== 工具方法 ========================

    @staticmethod
    def _validate_ip_or_cidr(value: str) -> bool:
        """校验是否为合法 IP 或 CIDR"""
        try:
            if "/" in value:
                ipaddress.ip_network(value, strict=False)
            else:
                ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    @staticmethod
    def _match_any(ip: str, rules: list[str]) -> bool:
        """判断 IP 是否匹配任一规则 (支持单 IP 与 CIDR)"""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for rule in rules:
            try:
                if "/" in rule:
                    if addr in ipaddress.ip_network(rule, strict=False):
                        return True
                else:
                    if str(addr) == rule:
                        return True
            except ValueError:
                continue
        return False


# 全局单例
ip_firewall = IPFirewall()
