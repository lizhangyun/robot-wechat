"""
微信客户端实现 - 模拟版(MockMode) + 真实 Hook 版(HookMode) 双模式

模拟模式：
    不连接真实微信，使用内置模拟数据测试，适合开发与单测。
    - 生成测试联系人/群/群成员；
    - 通过定时任务随机产生消息并触发回调。

真实 Hook 模式：
    通过 ctypes 调用 C++ DLL(weixin.dll) 导出的四个函数：
    ``init`` / ``api`` / ``loadWindow`` / ``uninstall``。
    - ``api(command, params)`` 传入命令编号与 JSON 参数，返回 JSON 结果；
    - 消息由 DLL 通过回调推送，本类负责桥接到异步回调。

通用能力：
    - psutil 检测微信进程是否运行；
    - 自动重连机制（连接断开时按策略重试）；
    - 发送限速与长消息分片（依据 settings.msg_sleep_sec / msg_max_lines）。
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Optional

# 独立运行支持：将项目根目录加入 sys.path，便于 `python wechat/wechat_client.py` 直接运行
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger

from config.settings import settings
from wechat.hook_interface import APICommand, MessageCallback, WeChatHookInterface
from wechat.message_types import MessageData, MessageType, SendResult

# psutil 为可选依赖（部分环境无），缺失时降级
try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


# ====================================================================== #
#  微信进程检测工具
# ====================================================================== #
def is_wechat_running(process_name: str = "WeChat") -> bool:
    """检测微信进程是否正在运行。

    Args:
        process_name: 进程名关键字（大小写不敏感）。

    Returns:
        是否存在匹配进程；若 psutil 不可用则返回 False。
    """
    if psutil is None:
        logger.warning("psutil 未安装，无法检测微信进程")
        return False
    try:
        for proc in psutil.process_iter(["name", "pid"]):
            name = proc.info.get("name") or ""
            if process_name.lower() in name.lower():
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug(f"进程检测异常: {e}")
    return False


# ====================================================================== #
#  模拟数据生成器
# ====================================================================== #
class _MockDataGenerator:
    """模拟模式数据生成器：生成联系人、群、群成员及随机消息。"""

    SAMPLE_CONTACTS = [
        {"wxid": "wxid_test001", "nickname": "张三", "remark": "客户-张三"},
        {"wxid": "wxid_test002", "nickname": "李四", "remark": "供应商-李四"},
        {"wxid": "wxid_test003", "nickname": "王五", "remark": ""},
        {"wxid": "wxid_test004", "nickname": "赵六", "remark": "朋友"},
        {"wxid": "wxid_test005", "nickname": "钱七", "remark": ""},
        {"wxid": "wxid_test006", "nickname": "孙八", "remark": "财务"},
    ]
    SAMPLE_GROUPS = [
        {"group_wxid": "12345678901@chatroom", "group_name": "测试群A", "member_count": 6},
        {"group_wxid": "12345678902@chatroom", "group_name": "记账交流群", "member_count": 12},
        {"group_wxid": "12345678903@chatroom", "group_name": "工作群", "member_count": 30},
    ]
    SAMPLE_MESSAGES = [
        "你好，在吗？",
        "记账 100 工商银行 午餐",
        "今天天气不错",
        "收到，谢谢",
        "记账 -50 微信 退款",
        "大家早上好",
        "这个文件帮我看一下",
        "晚上一起吃饭吗",
    ]

    @classmethod
    def contacts(cls) -> list[dict[str, Any]]:
        return [dict(c) for c in cls.SAMPLE_CONTACTS]

    @classmethod
    def groups(cls) -> list[dict[str, Any]]:
        return [dict(g) for g in cls.SAMPLE_GROUPS]

    @classmethod
    def group_members(cls, group_wxid: str) -> list[dict[str, Any]]:
        """根据群生成成员（包含登录账号自身）。"""
        members: list[dict[str, Any]] = []
        # 自身
        members.append(
            {"wxid": "wxid_self_000", "nickname": "机器人本体", "display_name": "小助手"}
        )
        for c in cls.SAMPLE_CONTACTS[:5]:
            members.append(
                {
                    "wxid": c["wxid"],
                    "nickname": c["nickname"],
                    "display_name": c["nickname"],
                }
            )
        return members

    @classmethod
    def random_message(
        cls, self_wxid: str = "wxid_self_000"
    ) -> MessageData:
        """生成一条随机模拟消息。"""
        is_group = random.random() < 0.5
        if is_group:
            group = random.choice(cls.SAMPLE_GROUPS)
            sender = random.choice(cls.SAMPLE_CONTACTS)["wxid"]
            text = random.choice(cls.SAMPLE_MESSAGES)
            # 群消息 content 形如 "wxid_xxx:\n内容"
            content = f"{sender}:\n{text}"
            return MessageData(
                msg_id=f"mock_{int(time.time() * 1000)}_{random.randint(0, 9999)}",
                sender_wxid=sender,
                receiver_wxid=self_wxid,
                content=content,
                msg_type=MessageType.TEXT,
                is_group=True,
                group_wxid=group["group_wxid"],
                at_users=[self_wxid] if random.random() < 0.3 else [],
            )
        else:
            contact = random.choice(cls.SAMPLE_CONTACTS)
            return MessageData(
                msg_id=f"mock_{int(time.time() * 1000)}_{random.randint(0, 9999)}",
                sender_wxid=contact["wxid"],
                receiver_wxid=self_wxid,
                content=random.choice(cls.SAMPLE_MESSAGES),
                msg_type=MessageType.TEXT,
                is_group=False,
                group_wxid=None,
                at_users=[],
            )


# ====================================================================== #
#  微信客户端
# ====================================================================== #
class WeChatClient(WeChatHookInterface):
    """微信客户端，实现 :class:`WeChatHookInterface`。

    Args:
        instance_id: 机器人实例ID。
        mock: 是否使用模拟模式。True=模拟，False=真实Hook。
        dll_path: 真实模式下 Hook DLL 路径（默认取 settings.wechat_hook_dll）。
    """

    def __init__(
        self,
        instance_id: str = "",
        mock: bool = True,
        dll_path: str = "",
    ) -> None:
        self.instance_id: str = instance_id
        self.mock: bool = mock
        self.dll_path: str = dll_path or settings.wechat_hook_dll

        # 运行状态
        self._initialized: bool = False
        self._window_loaded: bool = False
        self._running: bool = False
        self._login_info: dict[str, Any] = {}

        # 消息回调
        self._callback: Optional[MessageCallback] = None

        # 并发控制：发送串行化，避免 Hook 并发冲突
        self._send_lock: asyncio.Lock = asyncio.Lock()

        # 真实模式 DLL 句柄
        self._dll: Optional[Any] = None

        # 后台任务
        self._mock_msg_task: Optional[asyncio.Task[None]] = None
        self._reconnect_task: Optional[asyncio.Task[None]] = None

        # 重连配置
        self._reconnect_interval: int = 30  # 重连检测间隔(秒)
        self._max_reconnect: int = 10       # 最大连续重连次数
        self._reconnect_count: int = 0

    # ------------------------------------------------------------------ #
    #  生命周期
    # ------------------------------------------------------------------ #
    async def init(self, instance_id: str) -> bool:
        """初始化 Hook（模拟模式直接置位，真实模式加载 DLL 并调用 init）。"""
        self.instance_id = instance_id
        try:
            if self.mock:
                logger.info(f"[模拟模式] 初始化实例 {instance_id}")
                self._login_info = {
                    "wxid": "wxid_self_000",
                    "nickname": "机器人本体",
                    "alias": "robot_self",
                    "account": "robot_self",
                }
                self._initialized = True
                return True

            # 真实模式
            if not self.dll_path:
                logger.error("未配置 wechat_hook_dll，无法初始化真实Hook")
                return False
            if not self._load_dll():
                return False
            # 调用 DLL init 导出函数
            ok = self._call_dll_bool("init")
            if ok:
                self._initialized = True
                logger.info(f"[Hook模式] 初始化成功 实例={instance_id}")
            else:
                logger.error("[Hook模式] DLL init 返回失败")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception(f"init 异常: {e}")
            return False

    async def load_window(self) -> bool:
        """查找并绑定微信窗口。"""
        if not self._initialized:
            logger.warning("尚未初始化，无法加载窗口")
            return False
        if self.mock:
            # 模拟模式假设微信窗口已就绪
            self._window_loaded = True
            logger.info("[模拟模式] 窗口加载完成(虚拟)")
            return True

        # 真实模式：先检测进程
        if not is_wechat_running():
            logger.error("未检测到微信进程运行，请先启动并登录微信")
            return False
        ok = self._call_dll_bool("loadWindow")
        self._window_loaded = ok
        if ok:
            logger.info("[Hook模式] 微信窗口绑定成功")
        else:
            logger.error("[Hook模式] 微信窗口绑定失败")
        return ok

    async def uninstall(self) -> bool:
        """卸载 Hook，停止后台任务。"""
        await self._stop_background_tasks()
        if self.mock:
            self._initialized = False
            self._window_loaded = False
            logger.info("[模拟模式] 已卸载(虚拟)")
            return True

        ok = self._call_dll_bool("uninstall")
        self._dll = None
        self._initialized = False
        self._window_loaded = False
        logger.info(f"[Hook模式] 卸载 {'成功' if ok else '失败'}")
        return ok

    # ------------------------------------------------------------------ #
    #  核心 API 入口
    # ------------------------------------------------------------------ #
    async def api(self, command: str, params: dict) -> dict:
        """核心 API 入口。

        Args:
            command: 命令编号（可为 int/str/名称），见 :class:`APICommand`。
            params: 参数字典。

        Returns:
            结果字典。模拟模式返回模拟数据；真实模式调用 DLL。
        """
        cmd = self._normalize_command(command)
        params = params or {}

        if self.mock:
            return await self._mock_api(cmd, params)

        # 真实模式：通过 ctypes 调用 DLL api(command, json)
        try:
            params_json = json.dumps(params, ensure_ascii=False).encode("utf-8")
            # 注意：DLL 返回 char* 指向的内存由 DLL 管理，立即拷贝
            raw = self._dll.api(ctypes.c_int(cmd), ctypes.c_char_p(params_json))
            if not raw:
                return {"code": -1, "msg": "DLL 返回空"}
            result_str = ctypes.string_at(raw).decode("utf-8", errors="replace")
            return json.loads(result_str) if result_str else {}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"api 调用异常 cmd={cmd}: {e}")
            return {"code": -1, "msg": str(e)}

    # ------------------------------------------------------------------ #
    #  消息发送
    # ------------------------------------------------------------------ #
    async def send_text(self, wxid: str, text: str) -> SendResult:
        """发送文本消息，支持长消息分片与发送限速。"""
        if not text:
            return SendResult.fail("文本内容为空")

        chunks = self._split_text(text)
        last_result = SendResult.ok()
        async with self._send_lock:
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await asyncio.sleep(settings.msg_sleep_sec)
                res = await self._do_send_text(wxid, chunk)
                if not res.success:
                    return res
                last_result = res
        return last_result

    async def _do_send_text(self, wxid: str, text: str) -> SendResult:
        """实际发送单条文本。"""
        if self.mock:
            await asyncio.sleep(0.02)  # 模拟发送耗时
            msg_id = f"mock_send_{int(time.time() * 1000)}"
            logger.debug(f"[模拟模式] -> {wxid}: {text[:50]}")
            return SendResult.ok(msg_id)

        result = await self.api(APICommand.SEND_TEXT, {"wxid": wxid, "content": text})
        if result.get("code") == 0 or result.get("code") == 200:
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送文本失败"))

    async def send_image(self, wxid: str, path: str) -> SendResult:
        """发送图片消息。"""
        if not path:
            return SendResult.fail("图片路径为空")
        if self.mock:
            await asyncio.sleep(0.05)
            logger.debug(f"[模拟模式] -> {wxid}: [图片]{path}")
            return SendResult.ok(f"mock_img_{int(time.time() * 1000)}")

        result = await self.api(APICommand.SEND_IMAGE, {"wxid": wxid, "path": path})
        if result.get("code") in (0, 200):
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送图片失败"))

    async def send_file(self, wxid: str, path: str) -> SendResult:
        """发送文件消息。"""
        if not path:
            return SendResult.fail("文件路径为空")
        if self.mock:
            await asyncio.sleep(0.05)
            logger.debug(f"[模拟模式] -> {wxid}: [文件]{path}")
            return SendResult.ok(f"mock_file_{int(time.time() * 1000)}")

        result = await self.api(APICommand.SEND_FILE, {"wxid": wxid, "path": path})
        if result.get("code") in (0, 200):
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送文件失败"))

    # ------------------------------------------------------------------ #
    #  联系人 / 群查询
    # ------------------------------------------------------------------ #
    async def get_contacts(self) -> list[dict[str, Any]]:
        """获取联系人列表。"""
        if self.mock:
            return _MockDataGenerator.contacts()
        result = await self.api(APICommand.GET_CONTACTS, {})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_groups(self) -> list[dict[str, Any]]:
        """获取群聊列表。"""
        if self.mock:
            return _MockDataGenerator.groups()
        result = await self.api(APICommand.GET_GROUPS, {})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_group_members(self, group_wxid: str) -> list[dict[str, Any]]:
        """获取指定群成员列表。"""
        if self.mock:
            return _MockDataGenerator.group_members(group_wxid)
        result = await self.api(APICommand.GET_GROUP_MEMBERS, {"group_wxid": group_wxid})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_login_info(self) -> dict[str, Any]:
        """获取当前登录账号信息。"""
        if self.mock:
            return dict(self._login_info)
        result = await self.api(APICommand.GET_LOGIN_INFO, {})
        # 缓存登录信息
        if isinstance(result, dict) and result.get("code") in (0, 200):
            self._login_info = result.get("data", {})
        return result

    # ------------------------------------------------------------------ #
    #  消息回调
    # ------------------------------------------------------------------ #
    def set_message_callback(self, callback: MessageCallback) -> None:
        """注册消息接收回调。"""
        self._callback = callback
        logger.info("已注册消息回调")

    async def _dispatch_message(self, message: MessageData) -> None:
        """安全地分发消息到回调。"""
        if self._callback is None:
            return
        try:
            await self._callback(message)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"消息回调执行异常: {e}")

    # ------------------------------------------------------------------ #
    #  后台任务：模拟消息生成 / 自动重连
    # ------------------------------------------------------------------ #
    async def start(self, msg_interval: float = 5.0) -> None:
        """启动后台任务（模拟消息生成 + 自动重连检测）。

        Args:
            msg_interval: 模拟模式消息生成间隔(秒)。
        """
        if self._running:
            return
        self._running = True

        if self.mock and self._callback is not None:
            self._mock_msg_task = asyncio.create_task(
                self._mock_message_loop(msg_interval)
            )
            logger.info(f"[模拟模式] 消息生成任务已启动 间隔={msg_interval}s")

        # 真实模式开启重连监测
        if not self.mock:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _mock_message_loop(self, interval: float) -> None:
        """模拟模式：定时生成随机消息并回调。"""
        self_wxid = self._login_info.get("wxid", "wxid_self_000")
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                msg = _MockDataGenerator.random_message(self_wxid)
                logger.debug(f"[模拟消息] {msg.sender_wxid}: {msg.content_body[:40]}")
                await self._dispatch_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception(f"模拟消息生成异常: {e}")

    async def _reconnect_loop(self) -> None:
        """真实模式：周期性检测连接状态，断线时自动重连。"""
        while self._running:
            try:
                await asyncio.sleep(self._reconnect_interval)
                if not self._running:
                    break
                if not self._check_connection():
                    logger.warning("检测到微信连接异常，尝试重连...")
                    if await self._reconnect():
                        self._reconnect_count = 0
                    else:
                        self._reconnect_count += 1
                        if self._reconnect_count >= self._max_reconnect:
                            logger.error(
                                f"连续重连失败 {self._max_reconnect} 次，停止重连"
                            )
                            break
                        # 指数退避
                        backoff = min(
                            self._reconnect_interval * (2 ** self._reconnect_count),
                            600,
                        )
                        logger.info(f"等待 {backoff}s 后重试...")
                        await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception(f"重连循环异常: {e}")

    def _check_connection(self) -> bool:
        """检测连接是否正常（进程存活 + 窗口绑定）。"""
        if self.mock:
            return True
        return self._window_loaded and is_wechat_running()

    async def _reconnect(self) -> bool:
        """执行一次重连。"""
        try:
            logger.info("重新加载微信窗口...")
            if not is_wechat_running():
                logger.error("微信进程未运行，重连失败")
                return False
            ok = await self.load_window()
            if ok:
                logger.info("重连成功")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception(f"重连异常: {e}")
            return False

    async def stop(self) -> None:
        """停止客户端（停止后台任务，不卸载Hook）。"""
        await self._stop_background_tasks()

    async def _stop_background_tasks(self) -> None:
        """取消所有后台任务。"""
        self._running = False
        for task in (self._mock_msg_task, self._reconnect_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._mock_msg_task = None
        self._reconnect_task = None

    # ------------------------------------------------------------------ #
    #  真实模式 DLL 工具方法
    # ------------------------------------------------------------------ #
    def _load_dll(self) -> bool:
        """加载 Hook DLL 并设置函数签名。"""
        try:
            # ctypes.WinDLL 仅 Windows 可用
            if not hasattr(ctypes, "WinDLL"):
                logger.error("当前平台非 Windows，无法加载 Hook DLL")
                return False
            dll = ctypes.WinDLL(self.dll_path)  # type: ignore[attr-defined]

            # init() -> bool
            dll.init.restype = ctypes.c_bool
            dll.init.argtypes = []
            # loadWindow() -> bool
            dll.loadWindow.restype = ctypes.c_bool
            dll.loadWindow.argtypes = []
            # uninstall() -> bool
            dll.uninstall.restype = ctypes.c_bool
            dll.uninstall.argtypes = []
            # api(int command, const char* params_json) -> const char*
            dll.api.restype = ctypes.c_char_p
            dll.api.argtypes = [ctypes.c_int, ctypes.c_char_p]

            self._dll = dll
            logger.info(f"Hook DLL 加载成功: {self.dll_path}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"加载 Hook DLL 失败: {e}")
            return False

    def _call_dll_bool(self, func_name: str) -> bool:
        """调用 DLL 的无参 bool 返回函数。"""
        if self._dll is None:
            logger.error("DLL 未加载")
            return False
        try:
            func = getattr(self._dll, func_name)
            return bool(func())
        except Exception as e:  # noqa: BLE001
            logger.exception(f"DLL {func_name} 调用异常: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  工具方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_command(command: str | int) -> int:
        """将命令归一化为整数编号。

        支持传入 int、数字字符串、APICommand 名称。
        """
        if isinstance(command, int):
            return command
        if isinstance(command, str):
            # 数字字符串
            if command.isdigit():
                return int(command)
            # 名称 -> 编号
            name_map = {v: k for k, v in APICommand.all_commands().items()}
            if command in name_map:
                return name_map[command]
        return 0

    def _split_text(self, text: str) -> list[str]:
        """长消息分片。

        依据 settings.msg_split_enabled 与 msg_max_lines。
        超过最大行数时按行切分；单行过长按字符切分。
        """
        if not settings.msg_split_enabled:
            return [text]
        max_lines = settings.msg_max_lines
        lines = text.split("\n")
        if len(lines) <= max_lines:
            # 行数未超，但检查单字符长度
            if len(text) <= 2000:
                return [text]
            # 按字符切分
            return [text[i : i + 2000] for i in range(0, len(text), 2000)]
        # 按行切分
        chunks: list[str] = []
        for i in range(0, len(lines), max_lines):
            chunks.append("\n".join(lines[i : i + max_lines]))
        return chunks

    async def _mock_api(self, cmd: int, params: dict) -> dict:
        """模拟模式 API 响应。"""
        # 统一返回结构
        def ok(data: Any = None) -> dict:
            return {"code": 0, "msg": "ok", "data": data}

        if cmd == APICommand.GET_CONTACTS:
            return ok(_MockDataGenerator.contacts())
        if cmd == APICommand.GET_GROUPS:
            return ok(_MockDataGenerator.groups())
        if cmd == APICommand.GET_GROUP_MEMBERS:
            return ok(_MockDataGenerator.group_members(params.get("group_wxid", "")))
        if cmd == APICommand.GET_LOGIN_INFO:
            return ok(dict(self._login_info))
        if cmd in (APICommand.SEND_TEXT, APICommand.SEND_IMAGE, APICommand.SEND_FILE):
            await asyncio.sleep(0.02)
            return ok({"msg_id": f"mock_{cmd}_{int(time.time() * 1000)}"})
        if cmd == APICommand.REVOKE_MSG:
            return ok({"msg_id": params.get("msg_id")})
        if cmd == APICommand.GROUP_ANNOUNCEMENT:
            return ok({"group_wxid": params.get("group_wxid")})
        # 其余命令统一返回成功空数据
        return ok(None)


# ====================================================================== #
#  便捷工厂
# ====================================================================== #
def create_client(
    instance_id: str = "", mock: bool = True, dll_path: str = ""
) -> WeChatClient:
    """创建微信客户端实例的便捷工厂。"""
    return WeChatClient(instance_id=instance_id, mock=mock, dll_path=dll_path)


# ====================================================================== #
#  独立运行测试（模拟模式）
# ====================================================================== #
async def _self_test() -> None:
    """模拟模式自测：初始化、加载窗口、查询联系人/群、收发消息。"""
    client = WeChatClient(instance_id="test_instance", mock=True)
    received: list[MessageData] = []

    async def on_message(msg: MessageData) -> None:
        received.append(msg)
        logger.info(f"收到消息: {msg.sender_wxid} -> {msg.content_body[:30]}")

    client.set_message_callback(on_message)

    assert await client.init("test_instance"), "初始化失败"
    assert await client.load_window(), "加载窗口失败"

    contacts = await client.get_contacts()
    groups = await client.get_groups()
    login = await client.get_login_info()
    logger.info(f"联系人数量: {len(contacts)}")
    logger.info(f"群数量: {len(groups)}")
    logger.info(f"登录信息: {login}")

    res = await client.send_text("wxid_test001", "你好，这是一条测试消息")
    logger.info(f"发送结果: {res}")

    # 启动模拟消息 3 秒
    await client.start(msg_interval=1.0)
    await asyncio.sleep(3.2)
    await client.stop()
    logger.info(f"共收到 {len(received)} 条模拟消息")

    await client.uninstall()
    logger.info("自测完成")


if __name__ == "__main__":
    asyncio.run(_self_test())
