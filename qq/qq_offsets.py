"""
QQ NT 版本偏移量表

存储 QQ NT (Windows) 各内部函数相对于核心业务模块基址的偏移量（RVA）。
偏移量通过逆向工程获得，不同 QQ NT 版本需要不同的偏移量表。
当前表对应 QQ NT 9.9.15（原软件目标版本）。

QQ NT 架构说明
==============
QQ NT (9.9.x) 采用 Electron + 原生 Node.js 架构，核心消息逻辑位于
``QQ.exe`` 进程内的原生模块（如 ``wrapper.node`` / ``NapCat`` 系列），
与经典版 QQ（基于 ``QQ.exe`` + ``TIM.exe`` 的 C++ 体系）差异较大：

- 消息收发通过 Node.js 层的 IPC / NativeAddon 暴露；
- Hook 目标不再是单一 DLL，而是 ``wrapper.node`` 等原生模块内的导出函数；
- 联系人/群信息存储在 NTData SQLite 数据库中。

原软件 ``qq.dll`` 的 Hook 策略
==============================
原软件的 ``qq.dll`` 与 ``weixin.dll`` 类似，导出四个函数：
``init`` / ``api`` / ``loadWindow`` / ``uninstall``，通过
``CreateRemoteThread`` 注入到 ``QQ.exe`` 进程，Hook NT 内核消息分发。

如何获取真实偏移量（逆向工程方法）
================================

1. 准备工具：
   - x64dbg (https://x64dbg.com)      动态调试
   - IDA Pro  (https://hex-rays.com)  静态反汇编
   - Ghidra   (https://ghidra-sre.org) 开源反汇编
   - Cheat Engine (https://cheatengine.org) 内存搜索

2. 定位关键函数的常见思路：
   a) SendTextMsg：在 Node.js 层搜索 ``sendTextMsg`` 字符串引用，
      顺藤摸瓜找到原生 Addon 中接收 peerUin + content 的函数入口。
   b) RecvMsg：在消息分发回调处下断点（NTMessageListener），
      回溯找到统一的消息接收函数。
   c) GetContactList：搜索 SQLite 语句 ``SELECT * FROM Buddy`` 的引用位置。
   d) GetLoginInfo：搜索当前登录账号 uin 的全局指针。

3. 记录函数 RVA：
   - 在 IDA/Ghidra 中查看函数地址，减去 ``wrapper.node`` 的
     ImageBase，得到 RVA。
   - 运行时函数地址 = 模块基址 + RVA。

4. 验证偏移量：
   - 在 x64dbg 中附加 QQ，计算 基址 + RVA，
     确认反汇编与静态分析一致。

注意
====
本文件中的偏移量全部为占位符（0x00000000），非真实值。
真实使用前必须通过上述逆向工具确认对应 QQ NT 版本的实际偏移量，
否则 Hook 会写错地址导致 QQ 崩溃。
"""
from __future__ import annotations

from typing import NamedTuple


# ====================================================================== #
#  版本元信息
# ====================================================================== #
QQ_VERSION: str = "9.9.15"
"""目标 QQ NT 版本号（主.次.修订）。"""

QQ_WIN_DLL: str = "wrapper.node"
"""QQ NT 核心业务逻辑所在原生模块名称（Hook 目标模块）。"""

QQ_EXE: str = "QQ.exe"
"""QQ 进程可执行文件名（用于进程查找）。"""

QQ_WINDOW_CLASS: str = "TXGuiFoundation"
"""QQ NT 主窗口类名（用于窗口查找）。"""

QQ_LOGIN_WINDOW_CLASS: str = "TXGuiFoundation"
"""QQ NT 登录窗口类名。"""


class VersionInfo(NamedTuple):
    """QQ 版本号结构（主.次.修订）。"""

    major: int
    minor: int
    revision: int

    @classmethod
    def from_string(cls, version: str) -> "VersionInfo":
        """从 "9.9.15" 形式字符串解析版本号。

        不足 3 段时以 0 补齐，多余段忽略。
        """
        parts = version.split(".")
        nums: list[int] = []
        for p in parts[:3]:
            try:
                nums.append(int(p))
            except ValueError:
                nums.append(0)
        while len(nums) < 3:
            nums.append(0)
        return cls(*nums)  # type: ignore[arg-type]

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.revision}"


#: 当前目标版本信息
VERSION_INFO: VersionInfo = VersionInfo.from_string(QQ_VERSION)


# ====================================================================== #
#  偏移量表
# ====================================================================== #
#: 偏移量表。
#:
#: 键   : 函数/数据名称（语义化命名）
#: 值   : 相对 wrapper.node 基址的偏移量（RVA），单位字节
#:
#: 注意: 以下数值全部为占位符 ``0x00000000``，真实值需通过逆向工具获取。
#:       见模块文档字符串中的逆向方法说明。
OFFSETS: dict[str, int] = {
    # === 发送消息类 ===
    "SendTextMsg":       0x00000000,  # 发送文本消息函数入口
    "SendImageMsg":      0x00000000,  # 发送图片消息
    "SendFileMsg":       0x00000000,  # 发送文件消息
    "SendAtMsg":         0x00000000,  # 发送群@消息
    "SendCardMsg":       0x00000000,  # 发送名片
    "SendLinkMsg":       0x00000000,  # 发送链接
    "SendAppMsg":        0x00000000,  # 发送小程序消息
    "SendReplyMsg":      0x00000000,  # 发送回复消息
    "SendEmojiMsg":      0x00000000,  # 发送表情消息
    "RevokeMsg":         0x00000000,  # 撤回消息
    "ForwardMsg":        0x00000000,  # 转发消息

    # === 接收消息类（Hook 安装点） ===
    "RecvMsg":           0x00000000,  # 消息接收分发函数（主 Hook 点）
    "RecvMsgHook":       0x00000000,  # 消息接收 Hook 安装地址
    "NewMsgNotify":      0x00000000,  # 新消息通知回调（DLL 通过此回调推消息）
    "GetMsgRecord":      0x00000000,  # 获取消息记录

    # === 联系人类 ===
    "GetBuddy":          0x00000000,  # 获取单个好友
    "GetBuddyList":      0x00000000,  # 获取好友列表
    "GetBuddyDetail":    0x00000000,  # 获取好友详情
    "EditRemark":        0x00000000,  # 修改备注
    "AddBuddy":          0x00000000,  # 添加好友
    "DelBuddy":          0x00000000,  # 删除好友
    "AcceptBuddy":       0x00000000,  # 接受好友请求
    "Blacklist":         0x00000000,  # 拉黑/取消拉黑

    # === 群管理类 ===
    "GetGroupList":      0x00000000,  # 获取群列表
    "GetGroupMembers":   0x00000000,  # 获取群成员
    "GroupCreate":       0x00000000,  # 创建群
    "GroupInvite":       0x00000000,  # 邀请入群
    "GroupKick":         0x00000000,  # 踢出群成员
    "GroupAnnouncement": 0x00000000,  # 发布/修改群公告
    "GroupQuit":         0x00000000,  # 退出群
    "GroupRename":       0x00000000,  # 修改群名
    "GroupMute":         0x00000000,  # 群禁言
    "GroupQrcode":       0x00000000,  # 获取群二维码

    # === 登录信息类 ===
    "GetLoginInfo":      0x00000000,  # 获取登录账号信息
    "GetSelfUin":        0x00000000,  # 获取自身 uin 全局指针
    "LoginWnd":          0x00000000,  # 登录窗口相关

    # === 数据库类 ===
    "DBKey":             0x00000000,  # QQ 数据库密钥地址（解密 NTData SQLite）
    "MsgDbHandle":       0x00000000,  # 消息数据库句柄
    "BuddyDbHandle":     0x00000000,  # 好友数据库句柄

    # === OCR / 其他 ===
    "OcrImage":          0x00000000,  # 图片 OCR 识别
}


# ====================================================================== #
#  原软件 API 编号 -> 偏移量名称映射
# ====================================================================== #
#: 原软件 qq.dll 的 API 编号 -> 偏移量名称映射。
#:
#: 此映射对应原易语言软件逆向得到的 QQ API 编号语义，
#: 与复刻版自身的 :class:`qq.qq_hook_interface.QQAPICommand` 编号体系一致。
#: 真实 Hook 模式下，:class:`qq.qq_client.RealQQClient` 会按 cmd_id
#: 查找对应 QQ NT 内部函数。
ORIGINAL_API_TABLE: dict[int, str] = {
    0:  "GetLoginInfo",       # API[0]  初始化（获取登录信息）
    1:  "SendTextMsg",        # API[1]  发送文本消息
    2:  "SendImageMsg",       # API[2]  发送图片消息
    3:  "SendFileMsg",        # API[3]  发送文件消息
    4:  "SendAtMsg",          # API[4]  发送群@消息
    5:  "GetBuddyList",       # API[5]  获取好友列表
    6:  "GetGroupList",       # API[6]  获取群列表
    7:  "GetGroupMembers",    # API[7]  获取群成员
    8:  "EditRemark",         # API[8]  修改备注
    9:  "GroupAnnouncement",  # API[9]  发送群公告
    10: "GroupKick",          # API[10] 踢出群成员
    11: "GroupInvite",        # API[11] 邀请入群
    12: "GetMsgRecord",       # API[12] 获取消息记录
    13: "NewMsgNotify",       # API[13] 接收新消息（Hook 回调）
    14: "SendCardMsg",        # API[14] 发送名片
    15: "SendLinkMsg",        # API[15] 发送链接
    16: "SendAppMsg",         # API[16] 发送小程序
    17: "ForwardMsg",         # API[17] 转发消息
    18: "RevokeMsg",          # API[18] 撤回消息
    19: "GroupQrcode",        # API[19] 获取群二维码
    20: "GroupCreate",        # API[20] 创建群
    21: "GroupQuit",          # API[21] 退出群
    22: "GroupRename",        # API[22] 修改群名
    23: "GroupMute",          # API[23] 群禁言
    24: "GetLoginInfo",       # API[24] 获取登录信息
}


# ====================================================================== #
#  辅助函数
# ====================================================================== #
def get_offset(name: str) -> int:
    """获取指定函数/数据的偏移量（RVA）。

    Args:
        name: 函数名称（见 :data:`OFFSETS` 的键）。

    Returns:
        偏移量（字节）。不存在时返回 0 并记录警告。

    Raises:
        KeyError: 名称完全未知时。
    """
    if name not in OFFSETS:
        raise KeyError(f"未知偏移量名称: {name}")
    return OFFSETS[name]


def is_offset_available(name: str) -> bool:
    """判断指定函数偏移量是否已配置（非零）。

    用于在安装 Hook 前检查偏移量是否已通过逆向工具填入真实值，
    避免在占位符（0x0）地址上 Hook 导致崩溃。

    Args:
        name: 函数名称。

    Returns:
        已配置且非零返回 True，否则 False。
    """
    return OFFSETS.get(name, 0) != 0


def validate_offsets() -> dict[str, bool]:
    """校验所有偏移量配置状态。

    Returns:
        ``{名称: 是否已配置}`` 字典，便于启动时日志输出与诊断。
    """
    return {name: (offset != 0) for name, offset in OFFSETS.items()}


def resolve_function_address(dll_base: int, name: str) -> int:
    """根据 DLL 基址与偏移量名称计算函数绝对地址。

    Args:
        dll_base: wrapper.node 在目标进程中的基址（运行时通过模块枚举获得）。
        name: 偏移量名称。

    Returns:
        函数绝对地址。偏移量为 0 时返回 dll_base 本身（调用方应先
        通过 :func:`is_offset_available` 检查）。
    """
    return dll_base + get_offset(name)
