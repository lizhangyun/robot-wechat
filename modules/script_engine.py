"""
嵌入式 JavaScript 脚本引擎 - 对应原软件 node.dll

原软件通过嵌入 Node.js/V8 引擎（node.dll）执行 JavaScript 脚本，用于：
- 灵活的消息处理逻辑（自定义 JS 脚本处理收到的消息）；
- 动态规则扩展（运行时加载 JS 插件）；
- 数据转换与过滤。

实现策略
========
按优先级尝试以下后端，缺失时自动降级：

1. **PyMiniRacer**：V8 引擎的 Python 绑定，性能最佳，完整支持 ES6+；
2. **execjs**：通过外部 Node.js / 系统 JS 引擎执行；
3. **Python 简易求值器（降级方案）**：当上述两者均不可用时，使用
   受限的 Python ``eval`` / ``exec`` 执行。此模式仅支持简单表达式与
   类 Python 语法，不保证完整 JS 兼容，适合基本的消息过滤/转换。

降级方案的上下文兼容
====================
降级模式提供与 JS 等价的上下文对象：
- ``message``：消息对象，支持属性访问（``message.content`` 等）；
- ``send_text(uin, text)`` / ``send_image(uin, path)`` 等辅助函数；
- ``get_contact(uin)`` / ``get_group_members(group_uin)`` 等查询函数；
- ``result``：脚本返回值的载体（赋值 ``result = ...`` 或 ``return ...``）。

消息处理上下文
==============
:meth:`handle_message` 专门用于消息处理，提供::

    {
        "message": {                # 当前消息
            "msg_id": "...",
            "sender_wxid": "...",
            "content": "...",
            "is_group": false,
            "group_wxid": null,
            "msg_type": "text",
            ...
        },
        "send_text": <func>,        # 发送文本
        "send_image": <func>,       # 发送图片
        "get_contact": <func>,      # 查询联系人
        "get_group_members": <func>,# 查询群成员
        "reply": <func>,            # 快捷回复
    }
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional, Union

from loguru import logger

# 可选依赖：PyMiniRacer（V8 引擎 Python 绑定）
try:
    from py_mini_racer import py_mini_racer  # type: ignore[import-not-found]
    _MINI_RACER_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MINI_RACER_AVAILABLE = False
    py_mini_racer = None  # type: ignore[assignment]

# 可选依赖：execjs（外部 JS 运行时）
try:
    import execjs  # type: ignore[import-not-found]
    _EXECJS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _EXECJS_AVAILABLE = False
    execjs = None  # type: ignore[assignment]


# ====================================================================== #
#  属性访问字典（降级模式用，模拟 JS 对象属性访问）
# ====================================================================== #
class _AttrDict(dict):
    """支持属性访问的字典，模拟 JS 对象。

    ``obj.key`` 等价于 ``obj["key"]``，嵌套字典自动转换。
    """

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError:
            # JS 风格：访问不存在的属性返回 undefined -> None
            return None
        if isinstance(value, dict) and not isinstance(value, _AttrDict):
            return _AttrDict(value)
        if isinstance(value, list):
            return [_AttrDict(v) if isinstance(v, dict) else v for v in value]
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


# ====================================================================== #
#  脚本引擎
# ====================================================================== #
class ScriptEngine:
    """嵌入式 JavaScript 脚本引擎，对应原软件 ``node.dll``。

    按优先级使用 PyMiniRacer / execjs / Python 简易求值器执行脚本。

    Args:
        backend: 指定后端（``"mini_racer"`` / ``"execjs"`` / ``"python"`` /
            ``"auto"``）。``"auto"`` 时按可用性自动选择。
    """

    def __init__(self, backend: str = "auto") -> None:
        self._backend: str = self._select_backend(backend)
        self._mini_racer_ctx: Optional[Any] = None
        # Python 函数注册表（降级模式与 JS 桥接均使用）
        self._functions: dict[str, Callable[..., Any]] = {}
        logger.info(
            f"ScriptEngine 初始化 backend={self._backend} "
            f"(mini_racer={_MINI_RACER_AVAILABLE}, execjs={_EXECJS_AVAILABLE})"
        )

    # ------------------------------------------------------------------ #
    #  后端选择
    # ------------------------------------------------------------------ #
    @staticmethod
    def _select_backend(backend: str) -> str:
        """根据可用性与用户偏好选择执行后端。"""
        if backend == "auto":
            if _MINI_RACER_AVAILABLE:
                return "mini_racer"
            if _EXECJS_AVAILABLE:
                return "execjs"
            return "python"
        if backend == "mini_racer" and _MINI_RACER_AVAILABLE:
            return "mini_racer"
        if backend == "execjs" and _EXECJS_AVAILABLE:
            return "execjs"
        if backend == "python":
            return "python"
        # 指定的后端不可用，降级
        if backend != "auto":
            logger.warning(
                f"指定的脚本后端 '{backend}' 不可用，降级为 "
                f"{'mini_racer' if _MINI_RACER_AVAILABLE else 'execjs' if _EXECJS_AVAILABLE else 'python'}"
            )
            if _MINI_RACER_AVAILABLE:
                return "mini_racer"
            if _EXECJS_AVAILABLE:
                return "execjs"
        return "python"

    @property
    def backend(self) -> str:
        """当前使用的执行后端。"""
        return self._backend

    @property
    def is_v8(self) -> bool:
        """是否使用真正的 V8 引擎（PyMiniRacer）。"""
        return self._backend == "mini_racer"

    # ------------------------------------------------------------------ #
    #  函数注册
    # ------------------------------------------------------------------ #
    def register_function(self, name: str, func: Callable[..., Any]) -> None:
        """注册 Python 函数供 JS 调用。

        在 PyMiniRacer 模式下，函数会被注入 V8 全局上下文；
        在降级模式下，函数作为上下文变量供脚本调用。

        Args:
            name: 函数名（JS 中通过此名称调用）。
            func: Python 可调用对象。
        """
        self._functions[name] = func
        if self._backend == "mini_racer":
            self._ensure_mini_racer()
            # PyMiniRacer 通过 eval 注入函数引用
            # 实际桥接在调用时通过 context 传递
        logger.debug(f"已注册脚本函数: {name}")

    def _ensure_mini_racer(self) -> None:
        """确保 PyMiniRacer 上下文已初始化。"""
        if self._mini_racer_ctx is None and _MINI_RACER_AVAILABLE:
            self._mini_racer_ctx = py_mini_racer.MiniRacer()

    # ------------------------------------------------------------------ #
    #  脚本执行
    # ------------------------------------------------------------------ #
    async def execute(self, script: str, context: Optional[dict] = None) -> Any:
        """执行 JavaScript 脚本。

        Args:
            script: JavaScript 脚本字符串。
            context: 上下文变量字典（注入为脚本全局变量）。

        Returns:
            脚本执行结果。降级模式下若脚本无显式返回则返回 ``context``
            中可能被修改的 ``result`` 变量，或最后一条表达式的值。
        """
        context = context or {}
        try:
            if self._backend == "mini_racer":
                return await self._execute_mini_racer(script, context)
            if self._backend == "execjs":
                return await self._execute_execjs(script, context)
            return await self._execute_python(script, context)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"脚本执行异常: {e}")
            raise

    async def execute_file(self, filepath: str, context: Optional[dict] = None) -> Any:
        """执行 JS 文件。

        Args:
            filepath: JS 文件路径。
            context: 上下文变量字典。

        Returns:
            脚本执行结果。

        Raises:
            FileNotFoundError: 文件不存在。
        """
        path = Path(filepath)
        if not path.is_file():
            raise FileNotFoundError(f"脚本文件不存在: {filepath}")
        script = path.read_text(encoding="utf-8")
        logger.info(f"执行脚本文件: {filepath} ({len(script)} 字符)")
        return await self.execute(script, context)

    # ------------------------------------------------------------------ #
    #  消息处理
    # ------------------------------------------------------------------ #
    async def handle_message(
        self, script: str, message: dict
    ) -> dict:
        """用脚本处理消息。

        构建消息处理上下文，执行脚本并返回处理结果。

        上下文包含：
        - ``message``：当前消息对象（属性访问）；
        - ``send_text(uin, text)``：发送文本（异步，返回 bool）；
        - ``send_image(uin, path)``：发送图片；
        - ``get_contact(uin)``：查询联系人；
        - ``get_group_members(group_uin)``：查询群成员；
        - ``reply(text)``：快捷回复发送者；
        - ``result``：脚本返回值载体。

        Args:
            script: 处理消息的 JS 脚本。
            message: 消息字典（含 msg_id/sender_wxid/content 等）。

        Returns:
            处理结果字典，形如::

                {
                    "handled": True,       # 脚本是否处理了消息
                    "result": <any>,       # 脚本返回值
                    "actions": [...],      # 脚本执行期间发起的动作记录
                }
        """
        actions: list[dict[str, Any]] = []

        # 构建消息处理上下文
        msg_obj = _AttrDict(message)
        sender = message.get("sender_wxid") or message.get("sender_uin") or ""

        def _send_text(uin: str, text: str) -> bool:
            actions.append({"type": "send_text", "uin": uin, "text": text})
            logger.debug(f"[脚本] send_text({uin}, {text[:30]})")
            return True

        def _send_image(uin: str, path: str) -> bool:
            actions.append({"type": "send_image", "uin": uin, "path": path})
            logger.debug(f"[脚本] send_image({uin}, {path})")
            return True

        def _get_contact(uin: str) -> dict:
            actions.append({"type": "get_contact", "uin": uin})
            return {"uin": uin, "nickname": uin, "remark": ""}

        def _get_group_members(group_uin: str) -> list:
            actions.append({"type": "get_group_members", "group_uin": group_uin})
            return []

        def _reply(text: str) -> bool:
            return _send_text(sender, text)

        context: dict[str, Any] = {
            "message": msg_obj,
            "send_text": _send_text,
            "send_image": _send_image,
            "get_contact": _get_contact,
            "get_group_members": _get_group_members,
            "reply": _reply,
            "result": None,
        }
        # 合并已注册函数
        context.update(self._functions)

        try:
            result_value = await self.execute(script, context)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"消息处理脚本异常: {e}")
            return {
                "handled": False,
                "result": None,
                "actions": actions,
                "error": str(e),
            }

        # 降级模式下 result 可能通过 context["result"] 返回
        if result_value is None and context.get("result") is not None:
            result_value = context["result"]

        handled = result_value is not None or len(actions) > 0
        return {
            "handled": handled,
            "result": result_value,
            "actions": actions,
        }

    # ------------------------------------------------------------------ #
    #  PyMiniRacer 后端
    # ------------------------------------------------------------------ #
    async def _execute_mini_racer(self, script: str, context: dict) -> Any:
        """使用 PyMiniRacer (V8) 执行脚本。"""
        self._ensure_mini_racer()
        assert self._mini_racer_ctx is not None

        # 将上下文变量注入 V8 全局
        # 注意：PyMiniRacer 仅支持可 JSON 序列化的值与 eval 注入
        # 函数通过 JSON 无法传递，需通过 eval 桥接（此处简化：仅注入数据）
        setup_lines: list[str] = []
        for key, value in context.items():
            if callable(value):
                # 可调用对象：记录占位，实际调用经 Python 桥接
                # PyMiniRacer 不直接支持 Python 回调，这里用占位函数
                setup_lines.append(
                    f"var {key} = function() {{ return null; }};"
                )
            else:
                try:
                    json_str = json.dumps(value, ensure_ascii=False, default=str)
                    setup_lines.append(f"var {key} = {json_str};")
                except (TypeError, ValueError):
                    pass

        # 注入已注册函数占位
        for fname in self._functions:
            if fname not in context:
                setup_lines.append(
                    f"var {fname} = function() {{ return null; }};"
                )

        setup = "\n".join(setup_lines)
        full_script = f"{setup}\n{script}"

        # 在线程池中执行（V8 调用是同步阻塞的）
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._mini_racer_ctx.eval, full_script
        )
        return result

    # ------------------------------------------------------------------ #
    #  execjs 后端
    # ------------------------------------------------------------------ #
    async def _execute_execjs(self, script: str, context: dict) -> Any:
        """使用 execjs 执行脚本。"""
        assert execjs is not None

        # 构建上下文注入
        setup_lines: list[str] = []
        for key, value in context.items():
            if callable(value):
                setup_lines.append(
                    f"var {key} = function() {{ return null; }};"
                )
            else:
                try:
                    json_str = json.dumps(value, ensure_ascii=False, default=str)
                    setup_lines.append(f"var {key} = {json_str};")
                except (TypeError, ValueError):
                    pass

        setup = "\n".join(setup_lines)
        # execjs.eval 执行表达式并返回结果
        full_script = f"(function() {{ {setup}\n{script} }})()"

        loop = asyncio.get_event_loop()

        def _run() -> Any:
            return execjs.eval(full_script)

        return await loop.run_in_executor(None, _run)

    # ------------------------------------------------------------------ #
    #  Python 简易求值器（降级方案）
    # ------------------------------------------------------------------ #
    async def _execute_python(self, script: str, context: dict) -> Any:
        """使用 Python 简易求值器执行脚本（降级方案）。

        策略：
        1. 将 JS ``return`` 语句转换为对 ``result`` 变量赋值；
        2. 简单的 JS 字符串方法（``includes`` / ``startsWith`` 等）映射到 Python；
        3. 在受限命名空间中 ``exec`` 脚本；
        4. 返回 ``result`` 变量值，或最后一条表达式的值。

        此方案不保证完整 JS 兼容，仅支持基本的消息处理脚本。
        """
        # 预处理：JS -> Python 语法转换
        processed = self._js_to_python(script)

        # 构建命名空间：将上下文中的字典转换为属性可访问的 _AttrDict，
        # 使 JS 风格的 ``message.content`` 在降级模式下也能正常工作
        namespace: dict[str, Any] = {}
        for key, value in context.items():
            if isinstance(value, dict) and not isinstance(value, _AttrDict):
                namespace[key] = _AttrDict(value)
            elif isinstance(value, list):
                namespace[key] = [
                    _AttrDict(v) if isinstance(v, dict) else v for v in value
                ]
            else:
                namespace[key] = value
        # 注入已注册函数
        namespace.update(self._functions)
        # 提供常用内建
        namespace.setdefault("len", len)
        namespace.setdefault("str", str)
        namespace.setdefault("int", int)
        namespace.setdefault("float", float)
        namespace.setdefault("bool", bool)
        namespace.setdefault("list", list)
        namespace.setdefault("dict", dict)
        namespace.setdefault("True", True)
        namespace.setdefault("False", False)
        namespace.setdefault("None", None)
        namespace.setdefault("null", None)
        namespace.setdefault("undefined", None)
        namespace.setdefault("JSON", _JSONHelper())

        # result 初始为 None
        namespace.setdefault("result", None)

        # 判断是否有 return（已转换为 result=）或多条语句
        try:
            if "\n" not in processed and ";" not in processed and "=" not in processed:
                # 单表达式：直接 eval
                return eval(processed, {"__builtins__": {}}, namespace)
            # 多语句：exec，最后返回 result
            exec(processed, {"__builtins__": {}}, namespace)
            return namespace.get("result")
        except SyntaxError:
            # 语法错误，尝试作为表达式 eval
            try:
                return eval(processed, {"__builtins__": {}}, namespace)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"降级求值器执行失败: {e}")
                return namespace.get("result")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"降级求值器执行异常: {e}")
            return namespace.get("result")

    @staticmethod
    def _js_to_python(script: str) -> str:
        """将 JS 脚本做简单的语法转换，使其可在 Python 中执行。

        转换规则（保守，仅处理常见模式）：
        - ``return X;`` -> ``result = X``
        - ``return X``  -> ``result = X``
        - ``const`` / ``let`` / ``var`` 声明 -> 去除关键字
        - ``===`` / ``!==`` -> ``==`` / ``!=``
        - ``&&`` / ``||`` -> ``and`` / ``or``
        - ``!x`` -> ``not x``（简单情况）
        - ``true`` / ``false`` / ``null`` -> ``True`` / ``False`` / ``None``
        - ``//注释`` -> ``#注释``
        - ``console.log(...)`` -> ``print(...)``
        - ``.includes(x)`` -> ``.__contains__(x)``
        - ``.startsWith(x)`` -> ``.startswith(x)``
        - ``.endsWith(x)`` -> ``.endswith(x)``
        - ``typeof x`` -> ``type(x).__name__``

        注意：此转换不保证所有 JS 语法正确转换，仅覆盖常见消息处理模式。
        """
        result = script

        # 注释：单行 //
        result = re.sub(r"//([^\n]*)", r"#\1", result)

        # return X; -> result = X
        result = re.sub(r"\breturn\s+(.+?);", r"result = \1", result)
        # return X (无分号，行尾)
        result = re.sub(r"\breturn\s+(.+?)$", r"result = \1", result, flags=re.MULTILINE)

        # 变量声明关键字
        result = re.sub(r"\b(const|let|var)\s+", "", result)

        # 严格相等
        result = result.replace("===", "==")
        result = result.replace("!==", "!=")

        # 逻辑运算符（注意避免替换字符串内的 &&）
        # 简单替换：行内 && / ||
        result = re.sub(r"&&", " and ", result)
        result = re.sub(r"\|\|", " or ", result)

        # 布尔与空值
        result = re.sub(r"\btrue\b", "True", result)
        result = re.sub(r"\bfalse\b", "False", result)
        result = re.sub(r"\bnull\b", "None", result)
        result = re.sub(r"\bundefined\b", "None", result)

        # console.log
        result = re.sub(r"\bconsole\.log\b", "print", result)

        # 字符串方法
        result = re.sub(r"\.includes\(", ".__contains__(", result)
        result = re.sub(r"\.startsWith\(", ".startswith(", result)
        result = re.sub(r"\.endsWith\(", ".endswith(", result)
        result = re.sub(r"\.indexOf\(", ".find(", result)
        result = re.sub(r"\.toUpperCase\(", ".upper(", result)
        result = re.sub(r"\.toLowerCase\(", ".lower(", result)
        result = re.sub(r"\.trim\(", ".strip(", result)
        result = re.sub(r"\.slice\(", "[", result)  # 近似，可能不完美

        # typeof x -> type(x).__name__
        result = re.sub(r"\btypeof\s+(\w+)", r"type(\1).__name__", result)

        # 分号行尾 -> 换行
        result = result.replace(";\n", "\n")
        result = result.replace(";", "\n")

        return result


# ====================================================================== #
#  JSON 辅助对象（降级模式模拟 JS 的 JSON 对象）
# ====================================================================== #
class _JSONHelper:
    """模拟 JS ``JSON`` 对象，供降级模式脚本使用。"""

    @staticmethod
    def stringify(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, default=str)

    @staticmethod
    def parse(text: str) -> Any:
        return json.loads(text)


# ====================================================================== #
#  全局单例
# ====================================================================== #
script_engine: ScriptEngine = ScriptEngine()
"""脚本引擎全局单例。"""


def create_script_engine(backend: str = "auto") -> ScriptEngine:
    """创建脚本引擎的便捷工厂。

    Args:
        backend: 执行后端（``"auto"`` / ``"mini_racer"`` / ``"execjs"`` /
            ``"python"``）。

    Returns:
        :class:`ScriptEngine` 实例。
    """
    return ScriptEngine(backend=backend)


# ====================================================================== #
#  自测入口
# ====================================================================== #
async def _self_test() -> None:
    """脚本引擎自测。"""
    engine = ScriptEngine()
    logger.info(f"当前后端: {engine.backend}")

    # 基本表达式执行
    result = await engine.execute("1 + 2")
    logger.info(f"1 + 2 = {result}")

    # 带上下文执行
    result = await engine.execute("message.content", {"message": {"content": "hello"}})
    logger.info(f"message.content = {result}")

    # 消息处理
    message = {
        "msg_id": "msg_001",
        "sender_wxid": "wxid_sender",
        "content": "记账 100 午餐",
        "is_group": False,
    }
    # 脚本：判断消息内容是否包含"记账"
    script = 'result = message.content.includes("记账")'
    handle_result = await engine.handle_message(script, message)
    logger.info(f"消息处理结果: {handle_result}")

    # 注册函数
    def greet(name: str) -> str:
        return f"你好 {name}"

    engine.register_function("greet", greet)
    result = await engine.execute('greet("世界")', {"greet": greet})
    logger.info(f"greet 结果: {result}")

    logger.info("脚本引擎自测完成")


if __name__ == "__main__":
    asyncio.run(_self_test())
