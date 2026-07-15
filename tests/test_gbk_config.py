"""
GBK 配置解析器单元测试

测试范围:
  - config/gbk_config.py : GBK 编码的 INI 配置文件解析

测试内容:
  - GBKConfigParser 读取 GBK 编码的 INI 文件
  - 创建临时 GBK 编码 INI 文件, 测试读取
  - get() / getint() / getfloat() / getboolean() 查询接口
  - get_section() / get_all_sections() / to_dict() 批量查询
  - 中文 section 名和 key 名
  - set() 和 save() 写入
  - has_section() / has_option() 判断
  - remove_section() / remove_option() 移除

对应原软件 config.ini 的 GBK 编码读取方式。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from config.gbk_config import GBKConfigParser


# ============================================================================
# 辅助函数
# ============================================================================
def _write_gbk_ini(filepath: Path, content: str) -> None:
    """将内容以 GBK 编码写入文件"""
    filepath.write_bytes(content.encode("gbk"))


# ============================================================================
# 测试: 基本读取
# ============================================================================
def test_read_basic(tmp_path):
    """读取基本 GBK 编码 INI 文件"""
    ini_path = tmp_path / "config.ini"
    content = """[jizhang]
domain = http://example.com
enabled = 1
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.get("jizhang", "domain") == "http://example.com"
    assert parser.get("jizhang", "enabled") == "1"


def test_read_nonexistent_file(tmp_path):
    """读取不存在的文件不抛异常 (仅警告)"""
    parser = GBKConfigParser()
    parser.read(str(tmp_path / "nonexistent.ini"))
    # 不抛异常即通过
    assert parser.get("any", "key", fallback="default") == "default"


def test_read_chinese_section_and_key(tmp_path):
    """读取中文 section 名和 key 名"""
    ini_path = tmp_path / "chinese.ini"
    content = """[进程]
路径 = C:\\\\WeChat
名称 = 微信

[msg 消息最多行数]
消息最多行数 = 70
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.get("进程", "路径") == "C:\\\\WeChat"
    assert parser.get("进程", "名称") == "微信"
    assert parser.get("msg 消息最多行数", "消息最多行数") == "70"


def test_read_chinese_values(tmp_path):
    """读取中文值"""
    ini_path = tmp_path / "chinese_values.ini"
    content = """[jizhang]
trigger_words = 记账,入账,收入
bank_whitelist = 工商银行,建设银行,微信
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.get("jizhang", "trigger_words") == "记账,入账,收入"
    assert parser.get("jizhang", "bank_whitelist") == "工商银行,建设银行,微信"


def test_read_utf8_file(tmp_path):
    """支持以 UTF-8 编码读取"""
    ini_path = tmp_path / "utf8.ini"
    content = "[section]\nkey = 值\n"
    ini_path.write_bytes(content.encode("utf-8"))

    parser = GBKConfigParser()
    parser.read(str(ini_path), encoding="utf-8")
    assert parser.get("section", "key") == "值"


def test_filepath_property(tmp_path):
    """read 后 filepath 属性记录路径"""
    ini_path = tmp_path / "fp.ini"
    _write_gbk_ini(ini_path, "[s]\nk=v\n")

    parser = GBKConfigParser()
    assert parser.filepath is None
    parser.read(str(ini_path))
    assert parser.filepath == str(ini_path)


# ============================================================================
# 测试: get / getint / getfloat / getboolean
# ============================================================================
def test_get_with_fallback(tmp_path):
    """get 不存在的键返回 fallback"""
    ini_path = tmp_path / "fb.ini"
    _write_gbk_ini(ini_path, "[s]\nexisting = yes\n")

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.get("s", "existing") == "yes"
    assert parser.get("s", "missing", fallback="default") == "default"
    assert parser.get("nonexistent_section", "key", fallback="d") == "d"


def test_getint(tmp_path):
    """getint 解析整数"""
    ini_path = tmp_path / "int.ini"
    content = """[config]
port = 8080
count = 42
invalid = not_a_number
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.getint("config", "port") == 8080
    assert parser.getint("config", "count") == 42
    # 非数字返回 fallback
    assert parser.getint("config", "invalid", fallback=0) == 0
    # 不存在的键返回 fallback
    assert parser.getint("config", "missing", fallback=99) == 99


def test_getfloat(tmp_path):
    """getfloat 解析浮点数"""
    ini_path = tmp_path / "float.ini"
    content = """[config]
rate = 3.14
interval = 0.5
invalid = abc
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.getfloat("config", "rate") == 3.14
    assert parser.getfloat("config", "interval") == 0.5
    assert parser.getfloat("config", "invalid", fallback=0.0) == 0.0
    assert parser.getfloat("config", "missing", fallback=1.5) == 1.5


def test_getboolean(tmp_path):
    """getboolean 解析布尔值"""
    ini_path = tmp_path / "bool.ini"
    content = """[config]
enabled = true
disabled = false
yes_val = yes
no_val = no
on_val = on
off_val = off
one_val = 1
zero_val = 0
invalid = maybe
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.getboolean("config", "enabled") is True
    assert parser.getboolean("config", "disabled") is False
    assert parser.getboolean("config", "yes_val") is True
    assert parser.getboolean("config", "no_val") is False
    assert parser.getboolean("config", "on_val") is True
    assert parser.getboolean("config", "off_val") is False
    assert parser.getboolean("config", "one_val") is True
    assert parser.getboolean("config", "zero_val") is False
    # 非布尔值返回 fallback
    assert parser.getboolean("config", "invalid", fallback=False) is False
    assert parser.getboolean("config", "missing", fallback=True) is True


# ============================================================================
# 测试: get_section / get_all_sections / to_dict
# ============================================================================
def test_get_section(tmp_path):
    """get_section 返回整个 section 的键值对"""
    ini_path = tmp_path / "section.ini"
    content = """[jizhang]
domain = http://example.com
enabled = 1
keyword = abc123
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    section = parser.get_section("jizhang")
    assert section["domain"] == "http://example.com"
    assert section["enabled"] == "1"
    assert section["keyword"] == "abc123"


def test_get_section_nonexistent():
    """get_section 不存在的 section 返回空字典"""
    parser = GBKConfigParser()
    assert parser.get_section("nonexistent") == {}


def test_get_all_sections(tmp_path):
    """get_all_sections 返回所有 section 名"""
    ini_path = tmp_path / "sections.ini"
    content = """[section1]
key1 = val1
[section2]
key2 = val2
[section3]
key3 = val3
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    sections = parser.get_all_sections()
    assert sections == ["section1", "section2", "section3"]


def test_get_all_sections_empty():
    """空配置 get_all_sections 返回空列表"""
    parser = GBKConfigParser()
    assert parser.get_all_sections() == []


def test_to_dict(tmp_path):
    """to_dict 返回嵌套字典"""
    ini_path = tmp_path / "to_dict.ini"
    content = """[section1]
key1 = val1
key2 = val2
[section2]
key3 = val3
"""
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    d = parser.to_dict()
    assert d["section1"]["key1"] == "val1"
    assert d["section1"]["key2"] == "val2"
    assert d["section2"]["key3"] == "val3"


def test_to_dict_empty():
    """空配置 to_dict 返回空字典"""
    parser = GBKConfigParser()
    assert parser.to_dict() == {}


# ============================================================================
# 测试: has_section / has_option
# ============================================================================
def test_has_section(tmp_path):
    """has_section 判断 section 是否存在"""
    ini_path = tmp_path / "has.ini"
    _write_gbk_ini(ini_path, "[exists]\nkey=val\n")

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.has_section("exists") is True
    assert parser.has_section("not_exists") is False


def test_has_option(tmp_path):
    """has_option 判断键是否存在"""
    ini_path = tmp_path / "has_opt.ini"
    _write_gbk_ini(ini_path, "[s]\nexisting=yes\n")

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.has_option("s", "existing") is True
    assert parser.has_option("s", "missing") is False
    assert parser.has_option("no_section", "key") is False


# ============================================================================
# 测试: set / save 写入
# ============================================================================
def test_set_new_section(tmp_path):
    """set 自动创建不存在的 section"""
    parser = GBKConfigParser()
    parser.set("new_section", "key", "value")
    assert parser.get("new_section", "key") == "value"


def test_set_existing_section(tmp_path):
    """set 向已存在 section 添加键"""
    ini_path = tmp_path / "set_exist.ini"
    _write_gbk_ini(ini_path, "[s]\nold=1\n")

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    parser.set("s", "new", "2")
    assert parser.get("s", "old") == "1"
    assert parser.get("s", "new") == "2"


def test_set_overwrite(tmp_path):
    """set 覆盖已存在的键"""
    parser = GBKConfigParser()
    parser.set("s", "k", "v1")
    parser.set("s", "k", "v2")
    assert parser.get("s", "k") == "v2"


def test_set_chinese(tmp_path):
    """set 写入中文键值"""
    parser = GBKConfigParser()
    parser.set("记账", "触发词", "记账入账")
    assert parser.get("记账", "触发词") == "记账入账"


def test_save_and_reload(tmp_path):
    """save 保存后重新读取一致"""
    save_path = tmp_path / "saved.ini"

    parser = GBKConfigParser()
    parser.set("jizhang", "domain", "http://example.com")
    parser.set("jizhang", "enabled", "1")
    parser.set("中文段", "中文键", "中文值")
    parser.save(str(save_path))

    # 文件应以 GBK 编码保存
    assert save_path.exists()

    # 重新读取
    parser2 = GBKConfigParser()
    parser2.read(str(save_path))
    assert parser2.get("jizhang", "domain") == "http://example.com"
    assert parser2.get("jizhang", "enabled") == "1"
    assert parser2.get("中文段", "中文键") == "中文值"


def test_save_creates_parent_dir(tmp_path):
    """save 自动创建父目录"""
    save_path = tmp_path / "nested" / "dir" / "config.ini"
    parser = GBKConfigParser()
    parser.set("s", "k", "v")
    parser.save(str(save_path))
    assert save_path.exists()


def test_save_utf8(tmp_path):
    """save 支持指定 UTF-8 编码"""
    save_path = tmp_path / "utf8_save.ini"
    parser = GBKConfigParser()
    parser.set("s", "k", "值")
    parser.save(str(save_path), encoding="utf-8")

    # 以 UTF-8 读取验证
    parser2 = GBKConfigParser()
    parser2.read(str(save_path), encoding="utf-8")
    assert parser2.get("s", "k") == "值"


def test_filepath_after_save(tmp_path):
    """save 后 filepath 属性更新"""
    save_path = tmp_path / "fp_save.ini"
    parser = GBKConfigParser()
    parser.set("s", "k", "v")
    parser.save(str(save_path))
    assert parser.filepath == str(save_path)


# ============================================================================
# 测试: remove_section / remove_option
# ============================================================================
def test_remove_section(tmp_path):
    """remove_section 移除整个段"""
    ini_path = tmp_path / "rm_sec.ini"
    _write_gbk_ini(ini_path, "[s1]\nk1=v1\n[s2]\nk2=v2\n")

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.remove_section("s1") is True
    assert parser.has_section("s1") is False
    assert parser.has_section("s2") is True
    # 移除不存在的 section 返回 False
    assert parser.remove_section("nonexistent") is False


def test_remove_option(tmp_path):
    """remove_option 移除指定键"""
    ini_path = tmp_path / "rm_opt.ini"
    _write_gbk_ini(ini_path, "[s]\nk1=v1\nk2=v2\n")

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.remove_option("s", "k1") is True
    assert parser.has_option("s", "k1") is False
    assert parser.has_option("s", "k2") is True
    # 移除不存在的键返回 False
    assert parser.remove_option("s", "nonexistent") is False


# ============================================================================
# 测试: raw_parser 属性
# ============================================================================
def test_raw_parser(tmp_path):
    """raw_parser 返回底层 configparser 实例"""
    parser = GBKConfigParser()
    raw = parser.raw_parser
    assert raw is not None
    # 底层 configparser 应禁用插值 (通过行为验证: % 字符原样保留)
    parser.set("s", "path", "C:\\\\%USERPROFILE%\\\\data")
    assert parser.get("s", "path") == "C:\\\\%USERPROFILE%\\\\data"


# ============================================================================
# 测试: 百分号不被插值
# ============================================================================
def test_interpolation_disabled(tmp_path):
    """禁用插值, 值中的 % 不被解析"""
    ini_path = tmp_path / "interp.ini"
    content = "[s]\npath = C:\\\\%USERPROFILE%\\\\data\n"
    _write_gbk_ini(ini_path, content)

    parser = GBKConfigParser()
    parser.read(str(ini_path))
    assert parser.get("s", "path") == "C:\\\\%USERPROFILE%\\\\data"
