"""
自动更新器 - 从 GitHub / Gitee Release 获取更新

功能:
  - 检查更新 (获取最新 Release)
  - 版本比较 (语义化版本)
  - 下载更新包
  - 获取更新日志 (changelog)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from loguru import logger

from config.settings import settings
from network.http_client import HttpClient


class UpdateInfo:
    """更新信息"""

    def __init__(self, data: dict) -> None:
        self.version: str = data.get("tag_name", "").lstrip("vV")
        self.name: str = data.get("name", "")
        self.body: str = data.get("body", "")  # 更新日志 (markdown)
        self.published_at: str = data.get("published_at", "")
        self.html_url: str = data.get("html_url", "")
        self.assets: list[dict] = data.get("assets", [])

    @property
    def download_url(self) -> Optional[str]:
        """返回第一个资源下载地址"""
        if self.assets:
            return self.assets[0].get("browser_download_url")
        return None

    @property
    def download_size(self) -> int:
        if self.assets:
            return int(self.assets[0].get("size", 0))
        return 0

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "body": self.body,
            "published_at": self.published_at,
            "html_url": self.html_url,
            "download_url": self.download_url,
            "download_size": self.download_size,
        }


class AutoUpdater:
    """
    自动更新器

    repo 格式: "owner/repo" 或完整 URL
    支持 GitHub 与 Gitee (根据 update_repo 自动判断 API)
    """

    GITHUB_API = "https://api.github.com"
    GITEE_API = "https://gitee.com/api/v5"

    def __init__(self, repo: Optional[str] = None, current_version: Optional[str] = None) -> None:
        self.repo: str = repo or settings.update_repo
        self.current_version: str = current_version or settings.app_version
        self._http = HttpClient(timeout=20.0, max_retries=2)

    def _parse_repo(self) -> tuple[str, str]:
        """解析仓库标识, 返回 (platform, owner_repo)"""
        repo = self.repo.strip()
        if not repo:
            raise ValueError("未配置 update_repo")
        # 处理完整 URL
        for host, platform in (("github.com", "github"), ("gitee.com", "gitee")):
            if host in repo:
                # 提取 owner/repo
                m = re.search(rf"{host}/([^/]+/[^/]+?)(?:\.git)?(?:/|$)", repo)
                if m:
                    return platform, m.group(1)
        # 默认当作 github owner/repo
        return "github", repo

    async def check_update(self) -> Optional[UpdateInfo]:
        """
        检查更新, 返回最新 Release 信息 (无更新或失败返回 None)
        """
        if not self.repo:
            logger.debug("未配置 update_repo, 跳过更新检查")
            return None
        try:
            platform, owner_repo = self._parse_repo()
            api_base = self.GITHUB_API if platform == "github" else self.GITEE_API
            url = f"{api_base}/repos/{owner_repo}/releases/latest"
            await self._http.open()
            resp = await self._http.get(url, max_retries=2)
            if resp.status_code != 200:
                logger.warning(f"检查更新失败: HTTP {resp.status_code}")
                return None
            info = UpdateInfo(resp.json())
            if self.compare_versions(info.version, self.current_version) > 0:
                logger.info(f"发现新版本: {info.version} (当前 {self.current_version})")
                return info
            logger.info(f"当前已是最新版本: {self.current_version}")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"检查更新异常: {exc}")
            return None
        finally:
            await self._http.close()

    @staticmethod
    def compare_versions(v1: str, v2: str) -> int:
        """
        比较两个语义化版本号

        返回: 1 (v1>v2), 0 (相等), -1 (v1<v2)
        """
        def parse(v: str) -> tuple[int, ...]:
            nums = re.findall(r"\d+", v or "")
            return tuple(int(x) for x in nums) or (0,)

        a, b = parse(v1), parse(v2)
        # 补齐长度
        length = max(len(a), len(b))
        a = a + (0,) * (length - len(a))
        b = b + (0,) * (length - len(b))
        if a > b:
            return 1
        if a < b:
            return -1
        return 0

    async def download_update(self, info: UpdateInfo, dest_dir: Optional[Path] = None) -> Optional[Path]:
        """
        下载更新包到指定目录, 返回本地文件路径
        """
        if not info.download_url:
            logger.warning("该 Release 没有可下载资源")
            return None
        dest_dir = dest_dir or (settings.data_dir / "updates")
        dest_dir.mkdir(parents=True, exist_ok=True)
        # 从 URL 推断文件名
        filename = info.download_url.rsplit("/", 1)[-1] or f"update-{info.version}.zip"
        dest_path = dest_dir / filename

        try:
            await self._http.open()
            # 流式下载
            async with self._http._http.stream("GET", info.download_url) as resp:  # noqa: SLF001
                if resp.status_code != 200:
                    logger.warning(f"下载更新包失败: HTTP {resp.status_code}")
                    return None
                total = 0
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                        total += len(chunk)
            logger.info(f"更新包已下载: {dest_path} ({total} bytes)")
            return dest_path
        except Exception as exc:  # noqa: BLE001
            logger.error(f"下载更新包异常: {exc}")
            return None
        finally:
            await self._http.close()

    async def get_changelog(self, info: Optional[UpdateInfo] = None) -> str:
        """获取更新日志 (markdown 原文)"""
        if info is None:
            info = await self.check_update()
        if info is None:
            return "暂无更新日志"
        return info.body or "暂无更新日志"


# 全局单例
auto_updater = AutoUpdater()
