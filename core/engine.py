"""
核心引擎 - 系统中枢, 协调数据库、消息队列、WebSocket、安全模块与实例生命周期

职责:
  - 启动/停止各子系统
  - 管理机器人实例 (创建/启动/停止/状态)
  - 消息收发 (记录 + WebSocket 推送 + 消息队列发布)
  - 联系人 / 群 / 记账操作转发
  - mock 模式: 无真实微信时模拟消息收发
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from config.instance_config import InstanceConfig
from config.settings import settings
from core.websocket_manager import WebSocketManager, ws_manager
from database.manager import DatabaseManager, db_manager
from network.message_queue import MessageQueue, message_queue
from security.firewall import IPFirewall, ip_firewall
from security.license import LicenseManager, license_manager


@dataclass
class InstanceState:
    """运行中的实例状态"""
    instance_id: str
    config: InstanceConfig
    status: str = "stopped"  # stopped / starting / running / error
    started_at: Optional[float] = None
    task: Optional[asyncio.Task] = None
    stats: dict = field(default_factory=lambda: {"sent": 0, "received": 0})


class CoreEngine:
    """核心引擎 (单例)"""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        ws: Optional[WebSocketManager] = None,
        mq: Optional[MessageQueue] = None,
        firewall: Optional[IPFirewall] = None,
        license_mgr: Optional[LicenseManager] = None,
        mock: bool = False,
    ) -> None:
        self.db: DatabaseManager = db or db_manager
        self.ws: WebSocketManager = ws or ws_manager
        self.mq: MessageQueue = mq or message_queue
        self.firewall: IPFirewall = firewall or ip_firewall
        self.license: LicenseManager = license_mgr or license_manager
        self.mock: bool = mock

        self._instances: dict[str, InstanceState] = {}
        self._started: bool = False
        # 订阅消息队列 -> 转发到 WebSocket
        self._sub_tags: list[tuple[str, str]] = []

    # ======================== 生命周期 ========================

    async def start(self) -> None:
        """启动引擎 (初始化数据库与各子系统)"""
        if self._started:
            return
        logger.info(f"核心引擎启动中... (mock={self.mock})")
        # 1. 数据库
        await self.db.init()
        # 2. 防火墙名单加载
        await self.firewall._ensure_loaded()  # noqa: SLF001
        # 3. 消息队列
        await self.mq.start()
        # 4. 订阅消息队列: 将队列消息广播到 WebSocket
        tag = self.mq.subscribe("message.out", self._on_message_out)
        self._sub_tags.append(("message.out", tag))
        tag = self.mq.subscribe("message.in", self._on_message_in)
        self._sub_tags.append(("message.in", tag))
        # 5. 加载已存在的实例到内存
        await self._load_instances()
        # 6. 许可证本地验证 (不阻塞)
        try:
            self.license.verify_offline()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"许可证离线验证异常: {exc}")
        self._started = True
        logger.info("核心引擎已启动")

    async def stop(self) -> None:
        """停止引擎 (优雅关闭)"""
        if not self._started:
            return
        logger.info("核心引擎停止中...")
        # 停止所有运行中的实例
        for iid in list(self._instances.keys()):
            await self.stop_instance(iid)
        # 取消消息队列订阅
        for topic, tag in self._sub_tags:
            self.mq.unsubscribe(topic, tag)
        self._sub_tags.clear()
        # 停止消息队列
        await self.mq.stop()
        # 关闭数据库
        await self.db.close()
        self._started = False
        logger.info("核心引擎已停止")

    # ======================== 实例管理 ========================

    async def _load_instances(self) -> None:
        """从数据库加载实例到内存"""
        rows = await self.db.list_instances()
        for row in rows:
            cfg = InstanceConfig(
                instance_id=row["instance_id"],
                display_name=row.get("display_name", ""),
                wxid=row.get("wxid", ""),
            )
            self._instances[row["instance_id"]] = InstanceState(
                instance_id=row["instance_id"],
                config=cfg,
                status=row.get("status", "stopped"),
            )
        logger.info(f"已加载 {len(self._instances)} 个实例")

    async def create_instance(self, instance_id: str, display_name: str = "",
                              wxid: str = "", config: Optional[dict] = None) -> dict:
        """创建实例"""
        config = config or {}
        cfg = InstanceConfig(
            instance_id=instance_id,
            display_name=display_name or instance_id,
            wxid=wxid,
        )
        # 合并额外配置
        if config.get("msg_split_enabled") is not None:
            cfg.msg_split_enabled = bool(config["msg_split_enabled"])
        if config.get("msg_max_lines"):
            cfg.msg_max_lines = int(config["msg_max_lines"])
        if config.get("msg_sleep_sec"):
            cfg.msg_sleep_sec = float(config["msg_sleep_sec"])
        if config.get("jizhang_enabled") is not None:
            cfg.jizhang_enabled = bool(config["jizhang_enabled"])
        if config.get("jizhang_domain"):
            cfg.jizhang_domain = config["jizhang_domain"]

        await self.db.upsert_instance(instance_id, cfg.display_name, cfg.wxid,
                                      "stopped", cfg.to_dict())
        state = InstanceState(instance_id=instance_id, config=cfg, status="stopped")
        self._instances[instance_id] = state
        logger.info(f"已创建实例: {instance_id}")
        return await self.get_instance_status(instance_id)

    async def start_instance(self, instance_id: str) -> dict:
        """启动实例"""
        state = self._require_instance(instance_id)
        if state.status == "running":
            return await self.get_instance_status(instance_id)
        state.status = "starting"
        await self.db.set_instance_status(instance_id, "starting")
        # 启动一个后台任务模拟实例运行 (接收消息循环)
        state.task = asyncio.create_task(self._instance_loop(state))
        state.status = "running"
        state.started_at = time.time()
        await self.db.set_instance_status(instance_id, "running", state.started_at)
        await self.ws.broadcast_global("instance.started",
                                       {"instance_id": instance_id})
        logger.info(f"实例已启动: {instance_id}")
        return await self.get_instance_status(instance_id)

    async def stop_instance(self, instance_id: str) -> dict:
        """停止实例"""
        state = self._instances.get(instance_id)
        if not state:
            return {"instance_id": instance_id, "status": "not_found"}
        if state.task and not state.task.done():
            state.task.cancel()
            try:
                await state.task
            except asyncio.CancelledError:
                pass
        state.task = None
        state.status = "stopped"
        state.started_at = None
        await self.db.set_instance_status(instance_id, "stopped", None)
        await self.ws.broadcast_global("instance.stopped",
                                       {"instance_id": instance_id})
        logger.info(f"实例已停止: {instance_id}")
        return await self.get_instance_status(instance_id)

    async def get_instance_status(self, instance_id: str) -> dict:
        """获取实例状态"""
        state = self._instances.get(instance_id)
        if not state:
            row = await self.db.get_instance(instance_id)
            if row:
                return {"instance_id": instance_id, "status": row.get("status", "stopped"),
                        "config": row.get("config_json", {}), "exists": True}
            return {"instance_id": instance_id, "status": "not_found", "exists": False}
        return {
            "instance_id": instance_id,
            "status": state.status,
            "started_at": state.started_at,
            "config": state.config.model_dump(mode="json"),
            "stats": state.stats,
            "wxid": state.config.wxid,
            "display_name": state.config.display_name,
            "exists": True,
        }

    async def list_instances(self) -> list[dict]:
        """列出所有实例"""
        result = []
        for iid, state in self._instances.items():
            result.append({
                "instance_id": iid,
                "display_name": state.config.display_name,
                "wxid": state.config.wxid,
                "status": state.status,
                "started_at": state.started_at,
            })
        return result

    async def update_instance_config(self, instance_id: str, config: dict) -> dict:
        """更新实例配置"""
        state = self._require_instance(instance_id)
        # 更新内存配置
        if config.get("display_name") is not None:
            state.config.display_name = config["display_name"]
        if config.get("wxid") is not None:
            state.config.wxid = config["wxid"]
        if config.get("msg_split_enabled") is not None:
            state.config.msg_split_enabled = bool(config["msg_split_enabled"])
        if config.get("msg_max_lines") is not None:
            state.config.msg_max_lines = int(config["msg_max_lines"])
        if config.get("msg_sleep_sec") is not None:
            state.config.msg_sleep_sec = float(config["msg_sleep_sec"])
        if config.get("jizhang_enabled") is not None:
            state.config.jizhang_enabled = bool(config["jizhang_enabled"])
        if config.get("jizhang_domain") is not None:
            state.config.jizhang_domain = config["jizhang_domain"]
        # 持久化
        await self.db.update_instance_config(instance_id, state.config.to_dict())
        if config.get("display_name") is not None or config.get("wxid") is not None:
            await self.db.upsert_instance(
                instance_id, state.config.display_name, state.config.wxid,
                state.status, state.config.to_dict(),
            )
        logger.info(f"已更新实例配置: {instance_id}")
        return await self.get_instance_status(instance_id)

    def _require_instance(self, instance_id: str) -> InstanceState:
        state = self._instances.get(instance_id)
        if not state:
            raise ValueError(f"实例不存在: {instance_id}")
        return state

    async def _instance_loop(self, state: InstanceState) -> None:
        """实例运行循环 (mock 模式下定时模拟接收消息)"""
        try:
            while state.status == "running":
                await asyncio.sleep(settings.msg_sleep_sec * 5)
                if self.mock:
                    # mock: 偶尔模拟收到一条消息
                    await self._mock_receive(state)
        except asyncio.CancelledError:
            logger.debug(f"实例 {state.instance_id} 运行循环已取消")

    # ======================== 消息收发 ========================

    async def send_text(self, instance_id: str, wxid: str, text: str) -> dict:
        """发送文本消息"""
        self._require_instance(instance_id)
        msg_id = await self.db.add_message(instance_id, wxid, "out", "text", text)
        state = self._instances.get(instance_id)
        if state:
            state.stats["sent"] += 1
        payload = {"id": msg_id, "instance_id": instance_id, "wxid": wxid,
                    "direction": "out", "type": "text", "content": text,
                    "created_at": time.time()}
        # 发布到消息队列 (触发 WebSocket 广播)
        await self.mq.publish("message.out", payload)
        logger.info(f"[{instance_id}] 发送文本消息 -> {wxid}: {text[:50]}")
        return {"success": True, "message_id": msg_id, "mock": self.mock}

    async def send_image(self, instance_id: str, wxid: str, file_path: str,
                         text: str = "") -> dict:
        """发送图片消息"""
        self._require_instance(instance_id)
        msg_id = await self.db.add_message(instance_id, wxid, "out", "image", file_path,
                                            {"text": text})
        payload = {"id": msg_id, "instance_id": instance_id, "wxid": wxid,
                    "direction": "out", "type": "image", "content": file_path,
                    "extra": {"text": text}, "created_at": time.time()}
        await self.mq.publish("message.out", payload)
        return {"success": True, "message_id": msg_id, "mock": self.mock}

    async def send_file(self, instance_id: str, wxid: str, file_path: str,
                        file_name: str = "") -> dict:
        """发送文件消息"""
        self._require_instance(instance_id)
        msg_id = await self.db.add_message(instance_id, wxid, "out", "file", file_path,
                                            {"file_name": file_name})
        payload = {"id": msg_id, "instance_id": instance_id, "wxid": wxid,
                    "direction": "out", "type": "file", "content": file_path,
                    "extra": {"file_name": file_name}, "created_at": time.time()}
        await self.mq.publish("message.out", payload)
        return {"success": True, "message_id": msg_id, "mock": self.mock}

    async def get_message_history(self, instance_id: str, wxid: str, limit: int = 50) -> list[dict]:
        return await self.db.get_message_history(instance_id, wxid, limit)

    async def get_recent_messages(self, limit: int = 100, direction: Optional[str] = None) -> list[dict]:
        return await self.db.get_recent_messages(limit, direction)

    async def receive_message(self, instance_id: str, wxid: str, msg_type: str,
                              content: str, extra: Optional[dict] = None) -> int:
        """接收消息 (外部/微信回调调用)"""
        msg_id = await self.db.add_message(instance_id, wxid, "in", msg_type, content, extra)
        state = self._instances.get(instance_id)
        if state:
            state.stats["received"] += 1
        payload = {"id": msg_id, "instance_id": instance_id, "wxid": wxid,
                    "direction": "in", "type": msg_type, "content": content,
                    "extra": extra or {}, "created_at": time.time()}
        await self.mq.publish("message.in", payload)
        return msg_id

    # ======================== 队列回调 -> WebSocket ========================

    async def _on_message_out(self, message: Any) -> bool:
        """出站消息队列回调: 广播到 WebSocket"""
        try:
            payload = message.payload
            if isinstance(payload, dict):
                iid = payload.get("instance_id", "")
                await self.ws.broadcast_to_instance(iid, "message.out", payload)
                await self.ws.broadcast_global("message", payload)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"出站消息广播失败: {exc}")
            return False

    async def _on_message_in(self, message: Any) -> bool:
        """入站消息队列回调: 广播到 WebSocket"""
        try:
            payload = message.payload
            if isinstance(payload, dict):
                iid = payload.get("instance_id", "")
                await self.ws.broadcast_to_instance(iid, "message.in", payload)
                await self.ws.broadcast_global("message", payload)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"入站消息广播失败: {exc}")
            return False

    # ======================== 联系人 ========================

    async def list_contacts(self, instance_id: str, limit: int = 500) -> list[dict]:
        return await self.db.list_contacts(instance_id, limit)

    async def search_contacts(self, instance_id: str, keyword: str, limit: int = 50) -> list[dict]:
        return await self.db.search_contacts(instance_id, keyword, limit)

    async def update_contact_remark(self, instance_id: str, wxid: str, remark: str) -> bool:
        return await self.db.update_contact_remark(instance_id, wxid, remark)

    async def sync_contacts(self, instance_id: str) -> dict:
        """同步联系人 (mock: 生成示例数据)"""
        self._require_instance(instance_id)
        if self.mock:
            samples = [
                ("wxid_sample1", "张三", "同事"),
                ("wxid_sample2", "李四", "客户"),
                ("wxid_sample3", "王五", ""),
                ("gh_notify", "服务通知", ""),
            ]
            for wxid, nick, remark in samples:
                await self.db.upsert_contact(instance_id, wxid, nick, remark)
        return {"success": True, "synced": True}

    # ======================== 群 ========================

    async def list_groups(self, instance_id: str) -> list[dict]:
        return await self.db.list_groups(instance_id)

    async def list_group_members(self, group_wxid: str) -> list[dict]:
        return await self.db.list_group_members(group_wxid)

    async def send_group_announcement(self, instance_id: str, group_wxid: str,
                                      announcement: str) -> dict:
        self._require_instance(instance_id)
        await self.db.set_group_announcement(instance_id, group_wxid, announcement)
        await self.ws.broadcast_to_instance(instance_id, "group.announcement",
                                            {"group_wxid": group_wxid,
                                             "announcement": announcement})
        return {"success": True}

    async def group_stats(self, instance_id: str, group_wxid: str) -> dict:
        group = await self.db.get_group(instance_id, group_wxid)
        members = await self.db.list_group_members(group_wxid)
        return {
            "group": group,
            "member_count": len(members),
            "members": members,
        }

    # ======================== 记账 ========================

    async def list_bookkeeping(self, instance_id: str, limit: int = 100) -> list[dict]:
        return await self.db.list_bookkeeping(instance_id, limit)

    async def bookkeeping_stats(self, instance_id: str) -> dict:
        return await self.db.bookkeeping_stats(instance_id)

    async def add_bookkeeping(self, instance_id: str, wxid: str, amount: float, kind: str,
                              category: str = "", note: str = "") -> int:
        return await self.db.add_bookkeeping(instance_id, wxid, amount, kind, category, note)

    # ======================== 仪表盘 ========================

    async def dashboard_stats(self) -> dict:
        """仪表盘统计数据"""
        instances = await self.list_instances()
        running = [i for i in instances if i["status"] == "running"]
        today_msgs = await self.db.count_messages_today()
        return {
            "instance_total": len(instances),
            "instance_running": len(running),
            "today_messages": today_msgs,
            "ws_connections": self.ws.total_connections(),
            "mock_mode": self.mock,
            "license_status": self.license.get_status().get("valid", False),
            "mq_dead_letter": self.mq.dead_letter_count(),
        }

    # ======================== mock 工具 ========================

    async def _mock_receive(self, state: InstanceState) -> None:
        """mock 模式模拟接收消息"""
        import random as _r

        senders = ["wxid_sample1", "wxid_sample2", "wxid_sample3"]
        texts = ["你好", "在吗？", "收到", "今天天气不错", "稍后回复"]
        await self.receive_message(
            state.instance_id, _r.choice(senders), "text", _r.choice(texts)
        )


# 全局单例 (mock 标志由 run.py / server.py 启动时注入)
engine = CoreEngine()
