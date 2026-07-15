"""
QQ 客户端实现 - 模拟版(MockMode) + 真实 Hook 版(HookMode) 双模式

模拟模式：
    不连接真实 QQ，使用内置模拟数据测试，适合开发与单测。
    - 生成测试好友/群/群成员；
    - 通过定时任务随机产生消息并触发回调。

真实 Hook 模式：
    通过 ctypes 调用 C++ DLL(qq.dll) 导出的四个函数：
    ``init`` / ``api`` / ``loadWindow`` / ``uninstall``。
    与 ``wechat.wechat_client.RealWeChatClient`` 类似，通过
    ``CreateRemoteThread`` 注入 ``qq.dll`` 到 ``QQ.exe`` 进程，
    Hook QQ NT 内核消息分发。

通用能力：
    - psutil 检测 QQ 进程是否运行；
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
from typing import Any, Optional, Union

# 独立运行支持：将项目根目录加入 sys.path，便于 `python qq/qq_client.py` 直接运行
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger

from config.settings import settings
from qq.qq_hook_interface import (
    QQAPICommand,
    QQHookCallback,
    QQHookInterface,
    QQMessage,
    QQMessageCallback,
    QQMessageHook,
)
from wechat.message_types import MessageData, MessageType, SendResult

# psutil 为可选依赖（部分环境无），缺失时降级
try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

# 真实 Hook 模式依赖（DLL 注入 + 内存 Hook）
# 这些模块在非 Windows 平台 import 不报错，调用时才抛 PlatformNotSupportedError
from wechat.dll_injector import (  # noqa: E402
    DLLInjector,
    InjectionError,
    PlatformNotSupportedError,
)
from qq.qq_offsets import (
    OFFSETS,
    QQ_EXE,
    QQ_VERSION,
    QQ_WINDOW_CLASS,
    is_offset_available,
)


# ====================================================================== #
#  QQ 进程检测工具
# ====================================================================== #
def is_qq_running(process_name: str = "QQ") -> bool:
    """检测 QQ 进程是否正在运行。

    Args:
        process_name: 进程名关键字（大小写不敏感）。

    Returns:
        是否存在匹配进程；若 psutil 不可用则返回 False。
    """
    if psutil is None:
        logger.warning("psutil 未安装，无法检测 QQ 进程")
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


def find_qq_process(process_name: str = QQ_EXE) -> Optional[int]:
    """查找 QQ 进程 PID。

    优先使用 psutil（跨平台、更稳定），缺失时回退到 Windows
    ``CreateToolhelp32Snapshot`` 进程快照枚举（复用 wechat.dll_injector）。

    Args:
        process_name: 进程名（默认 ``QQ.exe``），大小写不敏感。

    Returns:
        QQ 进程 PID；未找到返回 None。
    """
    name_lower = process_name.lower()

    # 优先 psutil
    if psutil is not None:
        try:
            for proc in psutil.process_iter(["name", "pid"]):
                pname = (proc.info.get("name") or "").lower()
                if name_lower in pname:
                    pid = proc.info.get("pid")
                    if pid:
                        logger.debug(f"通过 psutil 找到 QQ 进程: pid={pid} name={pname}")
                        return int(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug(f"psutil 进程枚举异常: {e}")
        return None

    # 无 psutil，回退 Windows Toolhelp32（复用 wechat 的 find_wechat_process）
    try:
        from wechat.dll_injector import find_wechat_process
        return find_wechat_process(process_name)
    except PlatformNotSupportedError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"查找 QQ 进程异常: {e}")
        return None


def find_qq_window(
    class_name: str = QQ_WINDOW_CLASS,
    title_keyword: str = "QQ",
) -> Optional[int]:
    """查找 QQ 主窗口句柄（HWND）。

    复用 wechat.dll_injector 的窗口查找逻辑。

    Args:
        class_name: QQ 窗口类名。
        title_keyword: 标题关键字（回退匹配用）。

    Returns:
        窗口句柄（int）；未找到返回 None。
    """
    try:
        from wechat.dll_injector import find_wechat_window
        return find_wechat_window(class_name=class_name, title_keyword=title_keyword)
    except PlatformNotSupportedError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"查找 QQ 窗口异常: {e}")
        return None


# ====================================================================== #
#  模拟数据生成器
# ====================================================================== #
class _MockDataGenerator:
    """模拟模式数据生成器：生成好友、群、群成员及随机消息。"""

    SAMPLE_CONTACTS = [
        {"uin": "10001", "nickname": "张三", "remark": "客户-张三"},
        {"uin": "10002", "nickname": "李四", "remark": "供应商-李四"},
        {"uin": "10003", "nickname": "王五", "remark": ""},
        {"uin": "10004", "nickname": "赵六", "remark": "朋友"},
        {"uin": "10005", "nickname": "钱七", "remark": ""},
        {"uin": "10006", "nickname": "孙八", "remark": "财务"},
    ]
    SAMPLE_GROUPS = [
        {"group_uin": "200000001", "group_name": "测试群A", "member_count": 6},
        {"group_uin": "200000002", "group_name": "记账交流群", "member_count": 12},
        {"group_uin": "200000003", "group_name": "工作群", "member_count": 30},
    ]
    SAMPLE_MESSAGES = [
        "你好，在吗？",
        "记账 100 工商银行 午餐",
        "今天天气不错",
        "收到，谢谢",
        "记账 -50 QQ钱包 退款",
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
    def group_members(cls, group_uin: str) -> list[dict[str, Any]]:
        """根据群生成成员（包含登录账号自身）。"""
        members: list[dict[str, Any]] = []
        # 自身
        members.append(
            {"uin": "self_uin", "nickname": "机器人本体", "display_name": "小助手"}
        )
        for c in cls.SAMPLE_CONTACTS[:5]:
            members.append(
                {
                    "uin": c["uin"],
                    "nickname": c["nickname"],
                    "display_name": c["nickname"],
                }
            )
        return members

    @classmethod
    def random_message(
        cls, self_uin: str = "self_uin"
    ) -> MessageData:
        """生成一条随机模拟消息。"""
        is_group = random.random() < 0.5
        if is_group:
            group = random.choice(cls.SAMPLE_GROUPS)
            sender = random.choice(cls.SAMPLE_CONTACTS)["uin"]
            text = random.choice(cls.SAMPLE_MESSAGES)
            # 群消息 content 形如 "uin:\n内容"
            content = f"{sender}:\n{text}"
            return MessageData(
                msg_id=f"mock_{int(time.time() * 1000)}_{random.randint(0, 9999)}",
                sender_wxid=sender,
                receiver_wxid=self_uin,
                content=content,
                msg_type=MessageType.TEXT,
                is_group=True,
                group_wxid=group["group_uin"],
                at_users=[self_uin] if random.random() < 0.3 else [],
            )
        else:
            contact = random.choice(cls.SAMPLE_CONTACTS)
            return MessageData(
                msg_id=f"mock_{int(time.time() * 1000)}_{random.randint(0, 9999)}",
                sender_wxid=contact["uin"],
                receiver_wxid=self_uin,
                content=random.choice(cls.SAMPLE_MESSAGES),
                msg_type=MessageType.TEXT,
                is_group=False,
                group_wxid=None,
                at_users=[],
            )


# ====================================================================== #
#  QQ 客户端（模拟模式 + 本地 DLL 模式）
# ====================================================================== #
class QQClient(QQHookInterface):
    """QQ 客户端，实现 :class:`QQHookInterface`。

    Args:
        instance_id: 机器人实例ID。
        mock: 是否使用模拟模式。True=模拟，False=本地DLL模式。
        dll_path: 真实模式下 Hook DLL 路径（默认取空，需手动配置）。
    """

    def __init__(
        self,
        instance_id: str = "",
        mock: bool = True,
        dll_path: str = "",
    ) -> None:
        self.instance_id: str = instance_id
        self.mock: bool = mock
        self.dll_path: str = dll_path

        # 运行状态
        self._initialized: bool = False
        self._window_loaded: bool = False
        self._running: bool = False
        self._login_info: dict[str, Any] = {}

        # 消息回调
        self._callback: Optional[QQMessageCallback] = None

        # 并发控制：发送串行化，避免 Hook 并发冲突
        self._send_lock: asyncio.Lock = asyncio.Lock()

        # 真实模式 DLL 句柄
        self._dll: Optional[Any] = None

        # 后台任务
        self._mock_msg_task: Optional[asyncio.Task[None]] = None
        self._reconnect_task: Optional[asyncio.Task[None]] = None

        # 重连配置
        self._reconnect_interval: int = 30
        self._max_reconnect: int = 10
        self._reconnect_count: int = 0

    # ------------------------------------------------------------------ #
    #  生命周期
    # ------------------------------------------------------------------ #
    async def init(self, instance_id: str) -> bool:
        """初始化 Hook（模拟模式直接置位，真实模式加载 DLL 并调用 init）。"""
        self.instance_id = instance_id
        try:
            if self.mock:
                logger.info(f"[模拟模式] QQ 初始化实例 {instance_id}")
                self._login_info = {
                    "uin": "self_uin",
                    "nickname": "机器人本体",
                    "account": "robot_self",
                }
                self._initialized = True
                return True

            # 本地 DLL 模式
            if not self.dll_path:
                logger.error("未配置 qq_hook_dll，无法初始化真实 Hook")
                return False
            if not self._load_dll():
                return False
            # 调用 DLL init 导出函数
            ok = self._call_dll_bool("init")
            if ok:
                self._initialized = True
                logger.info(f"[Hook模式] QQ 初始化成功 实例={instance_id}")
            else:
                logger.error("[Hook模式] DLL init 返回失败")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception(f"QQ init 异常: {e}")
            return False

    async def load_window(self) -> bool:
        """查找并绑定 QQ 窗口。"""
        if not self._initialized:
            logger.warning("QQ 尚未初始化，无法加载窗口")
            return False
        if self.mock:
            # 模拟模式假设 QQ 窗口已就绪
            self._window_loaded = True
            logger.info("[模拟模式] QQ 窗口加载完成(虚拟)")
            return True

        # 真实模式：先检测进程
        if not is_qq_running():
            logger.error("未检测到 QQ 进程运行，请先启动并登录 QQ")
            return False
        ok = self._call_dll_bool("loadWindow")
        self._window_loaded = ok
        if ok:
            logger.info("[Hook模式] QQ 窗口绑定成功")
        else:
            logger.error("[Hook模式] QQ 窗口绑定失败")
        return ok

    async def uninstall(self) -> bool:
        """卸载 Hook，停止后台任务。"""
        await self._stop_background_tasks()
        if self.mock:
            self._initialized = False
            self._window_loaded = False
            logger.info("[模拟模式] QQ 已卸载(虚拟)")
            return True

        ok = self._call_dll_bool("uninstall")
        self._dll = None
        self._initialized = False
        self._window_loaded = False
        logger.info(f"[Hook模式] QQ 卸载 {'成功' if ok else '失败'}")
        return ok

    # ------------------------------------------------------------------ #
    #  核心 API 入口
    # ------------------------------------------------------------------ #
    async def api(self, cmd: Union[int, QQAPICommand], data: dict) -> dict:
        """核心 API 入口。

        Args:
            cmd: 命令编号（可为 int/QQAPICommand），见 :class:`QQAPICommand`。
            data: 参数字典。

        Returns:
            结果字典。模拟模式返回模拟数据；真实模式调用 DLL。
        """
        cmd_id = self._normalize_command(cmd)
        data = data or {}

        if self.mock:
            return await self._mock_api(cmd_id, data)

        # 真实模式：通过 ctypes 调用 DLL api(command, json)
        try:
            params_json = json.dumps(data, ensure_ascii=False).encode("utf-8")
            raw = self._dll.api(ctypes.c_int(cmd_id), ctypes.c_char_p(params_json))
            if not raw:
                return {"code": -1, "msg": "DLL 返回空"}
            result_str = ctypes.string_at(raw).decode("utf-8", errors="replace")
            return json.loads(result_str) if result_str else {}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"QQ api 调用异常 cmd={cmd_id}: {e}")
            return {"code": -1, "msg": str(e)}

    # ------------------------------------------------------------------ #
    #  消息发送
    # ------------------------------------------------------------------ #
    async def send_text(self, uin: str, text: str) -> SendResult:
        """发送文本消息，支持长消息分片与发送限速。"""
        if not text:
            return SendResult.fail("文本内容为空")

        chunks = self._split_text(text)
        last_result = SendResult.ok()
        async with self._send_lock:
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await asyncio.sleep(settings.msg_sleep_sec)
                res = await self._do_send_text(uin, chunk)
                if not res.success:
                    return res
                last_result = res
        return last_result

    async def _do_send_text(self, uin: str, text: str) -> SendResult:
        """实际发送单条文本。"""
        if self.mock:
            await asyncio.sleep(0.02)  # 模拟发送耗时
            msg_id = f"mock_send_{int(time.time() * 1000)}"
            logger.debug(f"[模拟模式] QQ -> {uin}: {text[:50]}")
            return SendResult.ok(msg_id)

        result = await self.api(QQAPICommand.SEND_TEXT, {"uin": uin, "content": text})
        if result.get("code") in (0, 200):
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送文本失败"))

    async def send_image(self, uin: str, path: str) -> SendResult:
        """发送图片消息。"""
        if not path:
            return SendResult.fail("图片路径为空")
        if self.mock:
            await asyncio.sleep(0.05)
            logger.debug(f"[模拟模式] QQ -> {uin}: [图片]{path}")
            return SendResult.ok(f"mock_img_{int(time.time() * 1000)}")

        result = await self.api(QQAPICommand.SEND_IMAGE, {"uin": uin, "path": path})
        if result.get("code") in (0, 200):
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送图片失败"))

    async def send_file(self, uin: str, path: str) -> SendResult:
        """发送文件消息。"""
        if not path:
            return SendResult.fail("文件路径为空")
        if self.mock:
            await asyncio.sleep(0.05)
            logger.debug(f"[模拟模式] QQ -> {uin}: [文件]{path}")
            return SendResult.ok(f"mock_file_{int(time.time() * 1000)}")

        result = await self.api(QQAPICommand.SEND_FILE, {"uin": uin, "path": path})
        if result.get("code") in (0, 200):
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送文件失败"))

    # ------------------------------------------------------------------ #
    #  联系人 / 群查询
    # ------------------------------------------------------------------ #
    async def get_contacts(self) -> list[dict[str, Any]]:
        """获取好友/联系人列表。"""
        if self.mock:
            return _MockDataGenerator.contacts()
        result = await self.api(QQAPICommand.GET_CONTACTS, {})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_groups(self) -> list[dict[str, Any]]:
        """获取群聊列表。"""
        if self.mock:
            return _MockDataGenerator.groups()
        result = await self.api(QQAPICommand.GET_GROUPS, {})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_group_members(self, group_uin: str) -> list[dict[str, Any]]:
        """获取指定群成员列表。"""
        if self.mock:
            return _MockDataGenerator.group_members(group_uin)
        result = await self.api(QQAPICommand.GET_GROUP_MEMBERS, {"group_uin": group_uin})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_login_info(self) -> dict[str, Any]:
        """获取当前登录账号信息。"""
        if self.mock:
            return dict(self._login_info)
        result = await self.api(QQAPICommand.GET_LOGIN_INFO, {})
        if isinstance(result, dict) and result.get("code") in (0, 200):
            self._login_info = result.get("data", {})
        return result

    # ------------------------------------------------------------------ #
    #  消息回调
    # ------------------------------------------------------------------ #
    def set_message_callback(self, callback: QQMessageCallback) -> None:
        """注册消息接收回调。"""
        self._callback = callback
        logger.info("已注册 QQ 消息回调")

    async def _dispatch_message(self, message: MessageData) -> None:
        """安全地分发消息到回调。"""
        if self._callback is None:
            return
        try:
            await self._callback(message)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"QQ 消息回调执行异常: {e}")

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
            logger.info(f"[模拟模式] QQ 消息生成任务已启动 间隔={msg_interval}s")

        # 真实模式开启重连监测
        if not self.mock:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _mock_message_loop(self, interval: float) -> None:
        """模拟模式：定时生成随机消息并回调。"""
        self_uin = self._login_info.get("uin", "self_uin")
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                msg = _MockDataGenerator.random_message(self_uin)
                logger.debug(f"[模拟消息] QQ {msg.sender_wxid}: {msg.content_body[:40]}")
                await self._dispatch_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception(f"QQ 模拟消息生成异常: {e}")

    async def _reconnect_loop(self) -> None:
        """真实模式：周期性检测连接状态，断线时自动重连。"""
        while self._running:
            try:
                await asyncio.sleep(self._reconnect_interval)
                if not self._running:
                    break
                if not self._check_connection():
                    logger.warning("检测到 QQ 连接异常，尝试重连...")
                    if await self._reconnect():
                        self._reconnect_count = 0
                    else:
                        self._reconnect_count += 1
                        if self._reconnect_count >= self._max_reconnect:
                            logger.error(
                                f"连续重连失败 {self._max_reconnect} 次，停止重连"
                            )
                            break
                        backoff = min(
                            self._reconnect_interval * (2 ** self._reconnect_count),
                            600,
                        )
                        logger.info(f"等待 {backoff}s 后重试...")
                        await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception(f"QQ 重连循环异常: {e}")

    def _check_connection(self) -> bool:
        """检测连接是否正常（进程存活 + 窗口绑定）。"""
        if self.mock:
            return True
        return self._window_loaded and is_qq_running()

    async def _reconnect(self) -> bool:
        """执行一次重连。"""
        try:
            logger.info("重新加载 QQ 窗口...")
            if not is_qq_running():
                logger.error("QQ 进程未运行，重连失败")
                return False
            ok = await self.load_window()
            if ok:
                logger.info("QQ 重连成功")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception(f"QQ 重连异常: {e}")
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
    #  本地 DLL 模式工具方法
    # ------------------------------------------------------------------ #
    def _load_dll(self) -> bool:
        """加载 Hook DLL 并设置函数签名。"""
        try:
            if not hasattr(ctypes, "WinDLL"):
                logger.error("当前平台非 Windows，无法加载 QQ Hook DLL")
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
            logger.info(f"QQ Hook DLL 加载成功: {self.dll_path}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"加载 QQ Hook DLL 失败: {e}")
            return False

    def _call_dll_bool(self, func_name: str) -> bool:
        """调用 DLL 的无参 bool 返回函数。"""
        if self._dll is None:
            logger.error("QQ DLL 未加载")
            return False
        try:
            func = getattr(self._dll, func_name)
            return bool(func())
        except Exception as e:  # noqa: BLE001
            logger.exception(f"QQ DLL {func_name} 调用异常: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  工具方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_command(cmd: Union[int, QQAPICommand, str]) -> int:
        """将命令归一化为整数编号。

        支持传入 int、QQAPICommand、数字字符串、名称。
        """
        if isinstance(cmd, QQAPICommand):
            return int(cmd)
        if isinstance(cmd, int):
            return cmd
        if isinstance(cmd, str):
            if cmd.isdigit():
                return int(cmd)
            name_map = {v: k for k, v in QQAPICommand.all_commands().items()}
            if cmd in name_map:
                return name_map[cmd]
        return int(QQAPICommand.INIT)

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
            if len(text) <= 2000:
                return [text]
            return [text[i : i + 2000] for i in range(0, len(text), 2000)]
        chunks: list[str] = []
        for i in range(0, len(lines), max_lines):
            chunks.append("\n".join(lines[i : i + max_lines]))
        return chunks

    async def _mock_api(self, cmd: int, data: dict) -> dict:
        """模拟模式 API 响应。"""
        def ok(d: Any = None) -> dict:
            return {"code": 0, "msg": "ok", "data": d}

        if cmd == int(QQAPICommand.GET_CONTACTS):
            return ok(_MockDataGenerator.contacts())
        if cmd == int(QQAPICommand.GET_GROUPS):
            return ok(_MockDataGenerator.groups())
        if cmd == int(QQAPICommand.GET_GROUP_MEMBERS):
            return ok(_MockDataGenerator.group_members(data.get("group_uin", "")))
        if cmd == int(QQAPICommand.GET_LOGIN_INFO):
            return ok(dict(self._login_info))
        if cmd in (
            int(QQAPICommand.SEND_TEXT),
            int(QQAPICommand.SEND_IMAGE),
            int(QQAPICommand.SEND_FILE),
        ):
            await asyncio.sleep(0.02)
            return ok({"msg_id": f"mock_{cmd}_{int(time.time() * 1000)}"})
        if cmd == int(QQAPICommand.REVOKE_MSG):
            return ok({"msg_id": data.get("msg_id")})
        if cmd == int(QQAPICommand.GROUP_ANNOUNCEMENT):
            return ok({"group_uin": data.get("group_uin")})
        # 其余命令统一返回成功空数据
        return ok(None)


# ====================================================================== #
#  真实 Hook 客户端（DLL 注入 + 内存 Hook）
# ====================================================================== #
# API 调用载荷结构：与 wechat 一致，将 (cmd_id, json 指针, json 长度)
# 打包为此结构，整体写入远端内存后以结构体指针作为 api() 的唯一参数。
class _ApiCallPayload(ctypes.Structure):
    """远程 api 调用的参数结构体（注入 DLL 与本客户端约定）。

    Fields:
        cmd_id: API 命令编号。
        json_ptr: 指向远端 JSON 字符串的指针（UTF-8，以 \\0 结尾）。
        json_len: JSON 字符串长度（不含结尾 \\0）。
        result_ptr: DLL 写入的结果字符串指针（DLL 填充，本端读取）。
    """

    _fields_ = [
        ("cmd_id", ctypes.c_int),
        ("json_ptr", ctypes.c_void_p),
        ("json_len", ctypes.c_int),
        ("result_ptr", ctypes.c_void_p),
    ]


class RealQQClient(QQHookInterface):
    """真实 Hook QQ 客户端：通过 DLL 注入与内存 Hook 实现真实 QQ 操作。

    与 :class:`QQClient`（模拟/本地 DLL 模式）实现相同的
    :class:`QQHookInterface` 接口，可在业务层无缝切换。

    工作流程
    ========
    1. :meth:`init` — 查找 QQ 进程 → 查找 QQ 窗口 → 注入 qq.dll
       → 安装消息 Hook → 调用 DLL ``init(hwnd)`` 初始化；
    2. :meth:`api` — 将 ``(cmd_id, data)`` 打包为结构体写入远端内存，
       通过 ``CreateRemoteThread`` 调用注入 DLL 的 ``api`` 导出函数，
       读取返回的 JSON 结果；
    3. :meth:`load_window` — 调用 DLL ``loadWindow`` 显示/绑定 QQ 窗口；
    4. 消息接收 — Hook 拦截消息后通过 ``WM_COPYDATA`` 推送，本类注册的
       Hook 回调将其转换为标准 :class:`MessageData` 并触发业务回调；
    5. :meth:`uninstall` — 卸载 Hook → 调用 DLL ``uninstall`` → 卸载 DLL。

    平台要求
    ========
    仅 Windows 可用。非 Windows 调用任何方法均返回 False / 空结果，
    但 ``import`` 不报错（优雅降级）。

    DLL 注入复用 ``wechat/dll_injector.py`` 的 :class:`DLLInjector` 类，
    QQ 与微信使用相同的注入机制（``CreateRemoteThread + LoadLibraryW``）。

    Args:
        instance_id: 机器人实例ID。
        dll_path: qq.dll 绝对路径（默认为空，需手动配置）。
        api_timeout_ms: 远程 api 调用超时（毫秒）。
    """

    def __init__(
        self,
        instance_id: str = "",
        dll_path: str = "",
        api_timeout_ms: int = 30000,
    ) -> None:
        self.instance_id: str = instance_id
        self.dll_path: str = dll_path
        self._api_timeout_ms: int = api_timeout_ms

        # 注入器与 Hook（复用 wechat.dll_injector 的 DLLInjector）
        self._injector: DLLInjector = DLLInjector()
        self._hook: QQMessageHook = QQMessageHook(injector=self._injector)

        # 运行状态
        self._pid: Optional[int] = None
        self._hwnd: int = 0
        self._dll_handle: int = 0
        self._initialized: bool = False
        self._window_loaded: bool = False
        self._login_info: dict[str, Any] = {}

        # 消息回调（业务层）
        self._callback: Optional[QQMessageCallback] = None

        # 发送串行化，避免 Hook 并发冲突
        self._send_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  生命周期
    # ------------------------------------------------------------------ #
    async def init(self, instance_id: str) -> bool:
        """初始化真实 Hook：查找进程 → 注入 DLL → 安装 Hook → 初始化。"""
        self.instance_id = instance_id
        try:
            if not self.dll_path:
                logger.error("未配置 qq_hook_dll，无法初始化真实 QQ Hook")
                return False

            # 1. 查找 QQ 进程
            pid = find_qq_process()
            if not pid:
                logger.error("未找到 QQ 进程，请先启动并登录 QQ")
                return False
            self._pid = pid
            logger.info(f"[RealHook] 找到 QQ 进程 pid={pid}")

            # 2. 查找 QQ 窗口
            hwnd = find_qq_window()
            if not hwnd:
                logger.error("未找到 QQ 主窗口，请确保 QQ 已登录")
                return False
            self._hwnd = hwnd
            logger.info(f"[RealHook] 找到 QQ 窗口 hwnd=0x{hwnd:X}")

            # 3. 注入 DLL
            self._dll_handle = self._injector.inject_dll(pid, self.dll_path)
            if not self._dll_handle:
                logger.error("QQ DLL 注入失败")
                return False

            # 4. 配置 Hook（注入完成后才有 dll_handle）
            self._hook._dll_handle = self._dll_handle
            if not await self._hook.install_hook(pid):
                logger.warning("QQ 消息 Hook 安装失败（消息接收将不可用）")

            # 5. 调用 DLL init(hwnd) 初始化
            exit_code = self._injector.call_remote_function(
                pid, self._dll_handle, "init", args=int(hwnd)
            )
            if not exit_code:
                logger.error("[RealHook] QQ DLL init 返回失败")
                return False

            self._initialized = True
            logger.info(f"[RealHook] QQ 初始化成功 实例={instance_id} pid={pid}")
            return True
        except PlatformNotSupportedError as e:
            logger.error(f"[RealHook] QQ 平台不支持: {e}")
            return False
        except InjectionError as e:
            logger.error(f"[RealHook] QQ 注入失败: {e}")
            return False
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[RealHook] QQ init 异常: {e}")
            return False

    async def load_window(self) -> bool:
        """调用注入 DLL 的 ``loadWindow`` 显示/绑定 QQ 窗口。"""
        if not self._initialized:
            logger.warning("QQ 尚未初始化，无法加载窗口")
            return False
        try:
            exit_code = self._injector.call_remote_function(
                self._pid,  # type: ignore[arg-type]
                self._dll_handle, "loadWindow", args=None
            )
            self._window_loaded = bool(exit_code)
            if self._window_loaded:
                logger.info("[RealHook] QQ 窗口加载成功")
            else:
                logger.error("[RealHook] QQ 窗口加载失败")
            return self._window_loaded
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[RealHook] QQ load_window 异常: {e}")
            return False

    async def uninstall(self) -> bool:
        """卸载 Hook → 调用 DLL uninstall → 卸载 DLL。"""
        ok = True
        # 1. 卸载 Hook
        try:
            if self._hook.is_installed:
                await self._hook.uninstall_hook()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[RealHook] QQ 卸载 Hook 异常: {e}")
            ok = False

        # 2. 调用 DLL uninstall
        if self._dll_handle and self._pid:
            try:
                self._injector.call_remote_function(
                    self._pid, self._dll_handle, "uninstall", args=None
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[RealHook] QQ DLL uninstall 调用失败: {e}")

        # 3. 卸载 DLL
        if self._pid:
            try:
                dll_name = self.dll_path.split("/")[-1].split("\\")[-1]
                ok = self._injector.eject_dll(self._pid, dll_name) and ok
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[RealHook] QQ 卸载 DLL 异常: {e}")
                ok = False

        self._dll_handle = 0
        self._initialized = False
        self._window_loaded = False
        self._pid = None
        logger.info(f"[RealHook] QQ 卸载 {'成功' if ok else '部分失败'}")
        return ok

    # ------------------------------------------------------------------ #
    #  核心 API 入口
    # ------------------------------------------------------------------ #
    async def api(self, cmd: Union[int, QQAPICommand], data: dict) -> dict:
        """调用注入 DLL 的 ``api(cmd_id, json_data)``。

        将 ``cmd`` 归一化为 cmd_id，把 ``data`` 序列化为 JSON，
        打包进 :class:`_ApiCallPayload` 结构体写入远端内存，通过远程线程
        调用 DLL 的 ``api`` 导出函数，再读取返回的 JSON 结果字符串。

        Args:
            cmd: 命令编号或 :class:`QQAPICommand`。
            data: 参数字典。

        Returns:
            DLL 返回的结果字典，通常含 ``code``/``msg``/``data`` 字段。
            调用异常时返回 ``{"code": -1, "msg": "..."}``。
        """
        if not self._initialized or not self._pid or not self._dll_handle:
            return {"code": -1, "msg": "未初始化或未注入 DLL"}
        cmd_id = self._normalize_command(cmd)
        data = data or {}
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._call_remote_api, cmd_id, data
            )
        except PlatformNotSupportedError as e:
            logger.error(f"[RealHook] QQ api 平台不支持: {e}")
            return {"code": -1, "msg": str(e)}
        except InjectionError as e:
            logger.error(f"[RealHook] QQ api 注入错误: {e}")
            return {"code": -1, "msg": str(e)}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[RealHook] QQ api 异常 cmd={cmd_id}: {e}")
            return {"code": -1, "msg": str(e)}

    def _call_remote_api(self, cmd: int, data: dict) -> dict:
        """同步执行远程 api 调用（在线程池中运行）。

        步骤：
        1. 序列化 data 为 UTF-8 JSON 字节串；
        2. 在远端分配内存写入 JSON 字符串；
        3. 构造 ``_ApiCallPayload``（cmd_id + json 指针 + 长度），
           在远端分配内存写入结构体；
        4. 解析 ``api`` 导出函数远端地址；
        5. ``CreateRemoteThread`` 调用 ``api``，传入结构体指针；
        6. 读取结构体回填的 ``result_ptr``，再读取结果 JSON 字符串；
        7. 释放远端内存，解析 JSON 返回。
        """
        json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        json_bytes_null = json_bytes + b"\x00"

        proc_handle = 0
        remote_json = 0
        remote_struct = 0
        try:
            proc_handle = self._injector.open_process(self._pid)  # type: ignore[arg-type]

            # 2. 远端写入 JSON 字符串
            remote_json = self._injector.remote_alloc(
                proc_handle, max(len(json_bytes_null), 1)
            )
            self._injector.remote_write(proc_handle, remote_json, json_bytes_null)

            # 3. 构造并写入结构体
            payload = _ApiCallPayload(
                cmd_id=cmd,
                json_ptr=remote_json,
                json_len=len(json_bytes),
                result_ptr=0,
            )
            payload_bytes = bytes(payload)
            remote_struct = self._injector.remote_alloc(
                proc_handle, max(len(payload_bytes), 1)
            )
            self._injector.remote_write(proc_handle, remote_struct, payload_bytes)

            # 4. 解析 api 函数地址
            api_addr = self._injector._resolve_remote_func_addr(
                self._pid,  # type: ignore[arg-type]
                self.dll_path, self._dll_handle, "api"
            )

            # 5. 远程线程调用 api(struct_ptr)
            self._injector.call_remote_thread(
                self._pid,  # type: ignore[arg-type]
                api_addr, arg=remote_struct, timeout_ms=self._api_timeout_ms
            )

            # 6. 读取回填的 result_ptr
            result_struct_bytes = self._injector.remote_read(
                proc_handle, remote_struct, ctypes.sizeof(_ApiCallPayload)
            )
            result_payload = _ApiCallPayload.from_buffer_copy(result_struct_bytes)
            result_ptr = int(result_payload.result_ptr or 0)

            if not result_ptr:
                logger.warning(f"[RealHook] QQ api 返回空结果 cmd={cmd}")
                return {"code": -1, "msg": "DLL 返回空结果"}

            # 读取结果字符串（最多 64KB，遇 \0 截断）
            raw = self._injector.remote_read(proc_handle, result_ptr, 64 * 1024)
            nul = raw.find(b"\x00")
            if nul >= 0:
                raw = raw[:nul]
            text = raw.decode("utf-8", errors="replace")
            try:
                return json.loads(text) if text else {}
            except json.JSONDecodeError:
                logger.warning(f"[RealHook] QQ api 结果 JSON 解析失败: {text[:100]}")
                return {"code": -1, "msg": "结果 JSON 解析失败", "raw": text}
        finally:
            if remote_struct and proc_handle:
                try:
                    self._injector.remote_free(proc_handle, remote_struct)
                except Exception:  # noqa: BLE001
                    pass
            if remote_json and proc_handle:
                try:
                    self._injector.remote_free(proc_handle, remote_json)
                except Exception:  # noqa: BLE001
                    pass
            if proc_handle:
                self._injector.close_handle(proc_handle)

    # ------------------------------------------------------------------ #
    #  消息发送
    # ------------------------------------------------------------------ #
    async def send_text(self, uin: str, text: str) -> SendResult:
        """发送文本消息（支持长消息分片与发送限速）。"""
        if not text:
            return SendResult.fail("文本内容为空")
        chunks = self._split_text(text)
        last = SendResult.ok()
        async with self._send_lock:
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await asyncio.sleep(settings.msg_sleep_sec)
                res = await self._do_send_text(uin, chunk)
                if not res.success:
                    return res
                last = res
        return last

    async def _do_send_text(self, uin: str, text: str) -> SendResult:
        """实际发送单条文本。"""
        result = await self.api(QQAPICommand.SEND_TEXT, {"uin": uin, "content": text})
        if result.get("code") in (0, 200):
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送文本失败"))

    async def send_image(self, uin: str, path: str) -> SendResult:
        """发送图片消息。"""
        if not path:
            return SendResult.fail("图片路径为空")
        result = await self.api(QQAPICommand.SEND_IMAGE, {"uin": uin, "path": path})
        if result.get("code") in (0, 200):
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送图片失败"))

    async def send_file(self, uin: str, path: str) -> SendResult:
        """发送文件消息。"""
        if not path:
            return SendResult.fail("文件路径为空")
        result = await self.api(QQAPICommand.SEND_FILE, {"uin": uin, "path": path})
        if result.get("code") in (0, 200):
            return SendResult.ok(result.get("msg_id"))
        return SendResult.fail(result.get("msg", "发送文件失败"))

    # ------------------------------------------------------------------ #
    #  联系人 / 群查询
    # ------------------------------------------------------------------ #
    async def get_contacts(self) -> list[dict[str, Any]]:
        """获取好友/联系人列表。"""
        result = await self.api(QQAPICommand.GET_CONTACTS, {})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_groups(self) -> list[dict[str, Any]]:
        """获取群聊列表。"""
        result = await self.api(QQAPICommand.GET_GROUPS, {})
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_group_members(self, group_uin: str) -> list[dict[str, Any]]:
        """获取指定群成员列表。"""
        result = await self.api(
            QQAPICommand.GET_GROUP_MEMBERS, {"group_uin": group_uin}
        )
        return result.get("data", []) if isinstance(result, dict) else []

    async def get_login_info(self) -> dict[str, Any]:
        """获取当前登录账号信息。"""
        result = await self.api(QQAPICommand.GET_LOGIN_INFO, {})
        if isinstance(result, dict) and result.get("code") in (0, 200):
            self._login_info = result.get("data", {})
        return result

    # ------------------------------------------------------------------ #
    #  消息回调
    # ------------------------------------------------------------------ #
    def set_message_callback(self, callback: QQMessageCallback) -> None:
        """注册消息接收回调。

        内部会向 :class:`QQMessageHook` 注册一个 Hook 回调，将 Hook 推送的
        原始消息字典转换为 :class:`MessageData`，再异步调用业务回调。
        """
        self._callback = callback
        # 同步注册到 hook 的回调表（register_callback 是异步，这里用线程调度）
        self._hook._callbacks.setdefault("*", [])
        self._hook._callbacks["*"].append(self._on_hook_message)
        logger.info("[RealHook] QQ 已注册消息回调")

    def _on_hook_message(self, msg_dict: dict[str, Any]) -> None:
        """Hook 消息回调：把原始字典转为 MessageData 并异步分发。"""
        try:
            qq_msg = QQMessage.from_dict(msg_dict)
            message = qq_msg.to_message_data()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[RealHook] QQ 消息转换异常: {e}")
            return
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._dispatch_message(message))
        except RuntimeError:
            logger.debug("[RealHook] QQ 无事件循环，跳过消息分发")

    async def _dispatch_message(self, message: MessageData) -> None:
        """安全地分发消息到业务回调。"""
        if self._callback is None:
            return
        try:
            await self._callback(message)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[RealHook] QQ 消息回调执行异常: {e}")

    # ------------------------------------------------------------------ #
    #  工具方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_command(cmd: Union[int, QQAPICommand, str]) -> int:
        """将命令归一化为整数编号（与 QQClient 一致）。"""
        if isinstance(cmd, QQAPICommand):
            return int(cmd)
        if isinstance(cmd, int):
            return cmd
        if isinstance(cmd, str):
            if cmd.isdigit():
                return int(cmd)
            name_map = {v: k for k, v in QQAPICommand.all_commands().items()}
            if cmd in name_map:
                return name_map[cmd]
        return int(QQAPICommand.INIT)

    def _split_text(self, text: str) -> list[str]:
        """长消息分片（与 QQClient 一致）。"""
        if not settings.msg_split_enabled:
            return [text]
        max_lines = settings.msg_max_lines
        lines = text.split("\n")
        if len(lines) <= max_lines:
            if len(text) <= 2000:
                return [text]
            return [text[i : i + 2000] for i in range(0, len(text), 2000)]
        chunks: list[str] = []
        for i in range(0, len(lines), max_lines):
            chunks.append("\n".join(lines[i : i + max_lines]))
        return chunks

    @property
    def is_connected(self) -> bool:
        """连接是否正常（已初始化且 QQ 进程存活）。"""
        if not self._initialized or not self._pid:
            return False
        return is_qq_running()


# ====================================================================== #
#  便捷工厂
# ====================================================================== #
def create_qq_client(
    instance_id: str = "", mock: bool = True, dll_path: str = ""
) -> QQClient:
    """创建 QQ 客户端实例的便捷工厂。

    Args:
        instance_id: 机器人实例ID。
        mock: True=模拟模式(QQClient)；False=本地DLL模式(QQClient)。
        dll_path: DLL 路径。

    Returns:
        :class:`QQClient` 实例。
    """
    return QQClient(instance_id=instance_id, mock=mock, dll_path=dll_path)


def create_real_qq_client(
    instance_id: str = "", dll_path: str = ""
) -> RealQQClient:
    """创建真实 Hook QQ 客户端的便捷工厂（DLL 注入 + 内存 Hook）。

    与 :func:`create_qq_client` 不同，本工厂返回的客户端通过注入 qq.dll
    到 QQ 进程实现真实自动化，仅 Windows 可用。

    Args:
        instance_id: 机器人实例ID。
        dll_path: qq.dll 绝对路径。

    Returns:
        :class:`RealQQClient` 实例。
    """
    return RealQQClient(instance_id=instance_id, dll_path=dll_path)


# ====================================================================== #
#  独立运行测试（模拟模式）
# ====================================================================== #
async def _self_test() -> None:
    """模拟模式自测：初始化、加载窗口、查询好友/群、收发消息。"""
    client = QQClient(instance_id="test_instance", mock=True)
    received: list[MessageData] = []

    async def on_message(msg: MessageData) -> None:
        received.append(msg)
        logger.info(f"收到 QQ 消息: {msg.sender_wxid} -> {msg.content_body[:30]}")

    client.set_message_callback(on_message)

    assert await client.init("test_instance"), "初始化失败"
    assert await client.load_window(), "加载窗口失败"

    contacts = await client.get_contacts()
    groups = await client.get_groups()
    login = await client.get_login_info()
    logger.info(f"好友数量: {len(contacts)}")
    logger.info(f"群数量: {len(groups)}")
    logger.info(f"登录信息: {login}")

    res = await client.send_text("10001", "你好，这是一条测试消息")
    logger.info(f"发送结果: {res}")

    # 启动模拟消息 3 秒
    await client.start(msg_interval=1.0)
    await asyncio.sleep(3.2)
    await client.stop()
    logger.info(f"共收到 {len(received)} 条模拟消息")

    await client.uninstall()
    logger.info("QQ 自测完成")


async def _real_client_smoke_test() -> None:
    """真实 Hook 客户端冒烟测试。

    在非 Windows 平台应优雅降级（init 返回 False 而非崩溃），
    在 Windows 平台需要真实 QQ 进程与 qq.dll 才能成功。
    """
    client = RealQQClient(instance_id="real_smoke", dll_path="")
    ok = await client.init("real_smoke")
    logger.info(f"[RealSmoke] QQ init 返回: {ok}")
    if ok:
        await client.load_window()
        info = await client.get_login_info()
        logger.info(f"[RealSmoke] QQ 登录信息: {info}")
        await client.uninstall()
    else:
        logger.info("[RealSmoke] 非 Windows 或无 QQ 进程，降级跳过（符合预期）")


if __name__ == "__main__":
    asyncio.run(_self_test())
    asyncio.run(_real_client_smoke_test())
