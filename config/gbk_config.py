"""
GBK 配置解析器 - 对应原软件使用 GBK 编码的 config.ini 文件

原软件（易语言）的 config.ini 文件使用 GBK 编码，
且包含中文 section 名和 key 名，例如：
  [进程], [log], [thread post], [msg 消息最多行数]

Python 标准库 configparser 默认不支持指定编码读取，
且对中文 section/key 的处理需要额外封装。
本模块封装了 GBK 编码的 INI 文件读取与解析。
"""
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any, Optional

from loguru import logger


class GBKConfigParser:
    """解析 GBK 编码的 INI 配置文件。

    封装 Python 标准库 ``configparser.ConfigParser``，
    自动以 GBK 编码读取文件，并提供便捷的查询接口。

    对应原软件 config.ini 的读取方式：
      - 文件编码为 GBK；
      - section 名和 key 名可能包含中文（如 ``[msg 消息最多行数]``）；
      - 值可能为中文（如银行名称、触发关键词）。
    """

    def __init__(self) -> None:
        self._parser: configparser.ConfigParser = configparser.ConfigParser(
            interpolation=None,  # 禁用插值，避免值中的 % 被解析
        )
        self._filepath: Optional[str] = None

    # ------------------------------------------------------------------ #
    #  读取 / 保存
    # ------------------------------------------------------------------ #
    def read(self, filepath: str, encoding: str = "gbk") -> None:
        """读取 GBK 编码的 INI 文件。

        Args:
            filepath: INI 文件路径。
            encoding: 文件编码，默认 GBK。原软件使用 GBK；
                      如果文件为 UTF-8 编码，可传入 ``"utf-8"``。
        """
        path = Path(filepath)
        if not path.exists():
            logger.warning(f"GBK 配置文件不存在: {filepath}")
            return

        self._filepath = str(filepath)
        # 先以二进制读取再按指定编码解码，避免 configparser 的编码兼容问题
        raw_text = path.read_bytes().decode(encoding, errors="replace")
        self._parser.read_string(raw_text, source=str(filepath))
        logger.debug(f"已加载 GBK 配置文件: {filepath} (encoding={encoding})")

    def save(self, filepath: str, encoding: str = "gbk") -> None:
        """保存配置到 INI 文件（GBK 编码）。

        Args:
            filepath: 目标文件路径。
            encoding: 文件编码，默认 GBK。
        """
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 使用 StringIO 收集写入内容，再以指定编码写入文件
        import io

        buf = io.StringIO()
        self._parser.write(buf)
        content = buf.getvalue()

        path.write_bytes(content.encode(encoding, errors="replace"))
        self._filepath = str(filepath)
        logger.debug(f"已保存 GBK 配置文件: {filepath} (encoding={encoding})")

    # ------------------------------------------------------------------ #
    #  查询接口
    # ------------------------------------------------------------------ #
    def get(
        self, section: str, key: str, fallback: Any = None
    ) -> Optional[str]:
        """获取配置值。

        Args:
            section: 段名（可能含中文）。
            key: 键名（可能含中文）。
            fallback: 键不存在时的默认返回值。

        Returns:
            配置值字符串，或 fallback。
        """
        try:
            return self._parser.get(section, key, fallback=fallback)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    def getint(
        self, section: str, key: str, fallback: int = 0
    ) -> int:
        """获取整型配置值。"""
        try:
            return self._parser.getint(section, key, fallback=fallback)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return fallback

    def getfloat(
        self, section: str, key: str, fallback: float = 0.0
    ) -> float:
        """获取浮点型配置值。"""
        try:
            return self._parser.getfloat(section, key, fallback=fallback)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return fallback

    def getboolean(
        self, section: str, key: str, fallback: bool = False
    ) -> bool:
        """获取布尔型配置值。"""
        try:
            return self._parser.getboolean(section, key, fallback=fallback)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return fallback

    def get_section(self, section: str) -> dict[str, str]:
        """获取整个 section 的所有键值对。

        Args:
            section: 段名。

        Returns:
            ``{key: value}`` 字典；段不存在时返回空字典。
        """
        if not self._parser.has_section(section):
            return {}
        return dict(self._parser.items(section))

    def get_all_sections(self) -> list[str]:
        """获取所有 section 名（可能含中文）。

        Returns:
            section 名列表。
        """
        return list(self._parser.sections())

    def has_section(self, section: str) -> bool:
        """判断指定 section 是否存在。"""
        return self._parser.has_section(section)

    def has_option(self, section: str, key: str) -> bool:
        """判断指定键是否存在。"""
        return self._parser.has_option(section, key)

    # ------------------------------------------------------------------ #
    #  写入接口
    # ------------------------------------------------------------------ #
    def set(self, section: str, key: str, value: str) -> None:
        """设置配置值（段不存在时自动创建）。"""
        if not self._parser.has_section(section):
            self._parser.add_section(section)
        self._parser.set(section, key, value)

    def remove_section(self, section: str) -> bool:
        """移除整个段。"""
        return self._parser.remove_section(section)

    def remove_option(self, section: str, key: str) -> bool:
        """移除指定键。"""
        return self._parser.remove_option(section, key)

    # ------------------------------------------------------------------ #
    #  转换
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, dict[str, str]]:
        """将整个配置转为嵌套字典。

        Returns:
            ``{section: {key: value}}`` 结构的字典。
        """
        result: dict[str, dict[str, str]] = {}
        for section in self._parser.sections():
            result[section] = dict(self._parser.items(section))
        return result

    @property
    def filepath(self) -> Optional[str]:
        """当前加载的文件路径。"""
        return self._filepath

    @property
    def raw_parser(self) -> configparser.ConfigParser:
        """底层 configparser 实例（供高级用法使用）。"""
        return self._parser
