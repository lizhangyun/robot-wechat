"""
配置模块单元测试

测试范围:
  - config/settings.py        : 全局配置默认值、目录创建
  - config/instance_config.py : 实例配置 INI 文件读写往返、兼容原软件格式

所有测试使用临时目录, 不污染项目目录。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config.settings import Settings, settings
from config.instance_config import InstanceConfig


# ============================================================================
# 测试: 默认配置值
# ============================================================================
def test_settings_default():
    """测试默认配置值"""
    s = Settings()

    # 基础配置
    assert s.app_name == "机器人3-复刻版"
    assert s.app_version == "1.0.0"
    assert s.app_id == "robot3_replica"

    # 数据库配置
    assert s.main_db_name == "data.db"
    assert s.users_db_name == "users.db"
    assert s.appdata_db_name == "appdata.db"
    assert s.db_encrypt_key == ""  # 默认不加密

    # HTTP API 配置
    assert s.api_host == "0.0.0.0"
    assert s.api_port == 3000

    # 微信配置
    assert s.wechat_version == "3.9.12.56"
    assert s.msg_max_lines == 70
    assert s.msg_sleep_sec == 1.0
    assert s.msg_split_enabled is True

    # 线程配置
    assert s.thread_pool_size == 50

    # 安全配置
    assert s.ip_whitelist_enabled is False
    assert s.ip_whitelist == []

    # 日志配置
    assert s.log_level == "INFO"
    assert s.log_to_file is True

    # 路径配置
    assert s.base_dir == Path(_PROJECT_ROOT)
    assert s.data_dir == s.base_dir / "data"
    assert s.log_dir == s.base_dir / "logs"
    assert s.db_dir == s.data_dir / "db"


def test_settings_global_singleton():
    """测试全局 settings 单例"""
    assert settings is not None
    assert isinstance(settings, Settings)
    assert settings.app_name == "机器人3-复刻版"


# ============================================================================
# 测试: 目录创建
# ============================================================================
def test_ensure_dirs():
    """测试目录创建"""
    # 使用临时基础目录
    temp_base = Path(tempfile.mkdtemp(prefix="robot3_test_config_"))
    s = Settings()
    # 临时覆盖路径
    original_data_dir = s.data_dir
    original_log_dir = s.log_dir
    original_db_dir = s.db_dir
    original_web_dir = s.web_dir

    try:
        s.data_dir = temp_base / "data"
        s.log_dir = temp_base / "logs"
        s.db_dir = temp_base / "data" / "db"
        s.web_dir = temp_base / "web"

        # 确保目录不存在
        assert not s.data_dir.exists()

        # 执行 ensure_dirs
        s.ensure_dirs()

        # 验证目录已创建
        assert s.data_dir.exists(), "data_dir 未创建"
        assert s.log_dir.exists(), "log_dir 未创建"
        assert s.db_dir.exists(), "db_dir 未创建"
        assert s.web_dir.exists(), "web_dir 未创建"
    finally:
        # 恢复原始路径
        s.data_dir = original_data_dir
        s.log_dir = original_log_dir
        s.db_dir = original_db_dir
        s.web_dir = original_web_dir


def test_ensure_dirs_idempotent():
    """测试目录创建幂等性 (重复调用不报错)"""
    s = Settings()
    # 调用多次不应报错
    s.ensure_dirs()
    s.ensure_dirs()
    assert s.data_dir.exists()


# ============================================================================
# 测试: InstanceConfig INI 文件读写往返
# ============================================================================
def test_instance_config_ini():
    """测试 INI 文件读写往返"""
    temp_dir = Path(tempfile.mkdtemp(prefix="robot3_test_ini_"))
    # 模拟原软件目录结构: data/app/{instance_id}/config.ini
    instance_dir = temp_dir / "app" / "c6801"
    instance_dir.mkdir(parents=True, exist_ok=True)
    ini_path = instance_dir / "config.ini"

    # 创建配置并保存
    config = InstanceConfig(
        instance_id="c6801",
        display_name="c6801",
        wxid="wxid_test_001",
        jizhang_enabled=True,
        jizhang_domain="https://api.example.com",
        jizhang_keyword="encrypted_keyword_data",
        msg_split_enabled=True,
        msg_max_lines=50,
        msg_sleep_sec=0.5,
        thread_post_count=30,
    )

    config.save_ini(ini_path)

    # 验证文件已创建
    assert ini_path.exists(), "INI 文件未创建"

    # 从 INI 加载
    loaded = InstanceConfig.from_ini(ini_path)

    # 验证往返一致性
    assert loaded.instance_id == "c6801"  # 从目录名获取
    assert loaded.jizhang_enabled is True
    assert loaded.jizhang_domain == "https://api.example.com"
    assert loaded.jizhang_keyword == "encrypted_keyword_data"
    assert loaded.msg_split_enabled is True
    assert loaded.msg_max_lines == 50
    assert loaded.msg_sleep_sec == 0.5
    assert loaded.thread_post_count == 30


def test_instance_config_ini_roundtrip_disabled():
    """测试禁用分片时的 INI 读写往返"""
    temp_dir = Path(tempfile.mkdtemp(prefix="robot3_test_ini2_"))
    instance_dir = temp_dir / "app" / "c6802"
    instance_dir.mkdir(parents=True, exist_ok=True)
    ini_path = instance_dir / "config.ini"

    config = InstanceConfig(
        instance_id="c6802",
        msg_split_enabled=False,
        msg_max_lines=100,
        msg_sleep_sec=2.0,
        jizhang_enabled=False,
    )
    config.save_ini(ini_path)

    loaded = InstanceConfig.from_ini(ini_path)
    assert loaded.msg_split_enabled is False
    assert loaded.msg_max_lines == 100
    assert loaded.msg_sleep_sec == 2.0
    assert loaded.jizhang_enabled is False


# ============================================================================
# 测试: 兼容原软件格式
# ============================================================================
def test_instance_config_compat():
    """测试兼容原软件格式"""
    temp_dir = Path(tempfile.mkdtemp(prefix="robot3_test_compat_"))
    instance_dir = temp_dir / "app" / "c6803"
    instance_dir.mkdir(parents=True, exist_ok=True)
    ini_path = instance_dir / "config.ini"

    # 手动写入原软件格式的 INI (GBK 编码, 中文键名)
    import configparser
    config = configparser.ConfigParser()

    # 原软件格式: [jizhang] section 有 enabled/keyword/domain
    config["jizhang"] = {
        "enabled": "True",
        "keyword": "AES加密的配置数据",
        "domain": "https://jizhang.example.com",
    }
    # [msg_split] section 有 status
    config["msg_split"] = {"status": "1"}
    # [msg] section 有中文键名 "消息最多行数"
    config["msg"] = {"消息最多行数": "80"}
    # [sleep_time] section 有 sec
    config["sleep_time"] = {"sec": "1.5"}
    # [thread] section 有 post
    config["thread"] = {"post": "40"}

    with open(ini_path, "w", encoding="gbk") as f:
        config.write(f)

    # 使用 from_ini 加载
    loaded = InstanceConfig.from_ini(ini_path)

    # 验证兼容读取
    assert loaded.instance_id == "c6803"  # 从目录名获取
    assert loaded.jizhang_enabled is True
    assert loaded.jizhang_keyword == "AES加密的配置数据"
    assert loaded.jizhang_domain == "https://jizhang.example.com"
    assert loaded.msg_split_enabled is True
    assert loaded.msg_max_lines == 80
    assert loaded.msg_sleep_sec == 1.5
    assert loaded.thread_post_count == 40


def test_instance_config_compat_defaults():
    """测试原软件格式缺失项时使用默认值"""
    temp_dir = Path(tempfile.mkdtemp(prefix="robot3_test_compat_def_"))
    instance_dir = temp_dir / "app" / "c6804"
    instance_dir.mkdir(parents=True, exist_ok=True)
    ini_path = instance_dir / "config.ini"

    # 写入一个几乎空的 INI
    import configparser
    config = configparser.ConfigParser()
    config["jizhang"] = {"enabled": "True"}

    with open(ini_path, "w", encoding="gbk") as f:
        config.write(f)

    loaded = InstanceConfig.from_ini(ini_path)

    # 验证默认值
    assert loaded.instance_id == "c6804"
    assert loaded.jizhang_enabled is True
    assert loaded.jizhang_domain == ""  # 默认空
    assert loaded.jizhang_keyword == ""  # 默认空
    assert loaded.msg_split_enabled is True  # 默认 True
    assert loaded.msg_max_lines == 70  # 默认 70
    assert loaded.msg_sleep_sec == 1.0  # 默认 1.0
    assert loaded.thread_post_count == 50  # 默认 50


# ============================================================================
# 测试: InstanceConfig 模型
# ============================================================================
def test_instance_config_model():
    """测试 InstanceConfig 模型基本功能"""
    config = InstanceConfig(
        instance_id="model_test",
        display_name="模型测试",
        wxid="wxid_model",
    )

    assert config.instance_id == "model_test"
    assert config.display_name == "模型测试"
    assert config.wxid == "wxid_model"

    # 默认值
    assert config.jizhang_enabled is True
    assert config.msg_split_enabled is True
    assert config.msg_max_lines == 70
    assert config.msg_sleep_sec == 1.0
    assert config.thread_post_count == 50

    # to_dict
    d = config.to_dict()
    assert d["instance_id"] == "model_test"
    assert d["display_name"] == "模型测试"
    assert "jizhang_enabled" in d
    assert "msg_max_lines" in d


def test_instance_config_model_dump_json():
    """测试 InstanceConfig JSON 序列化"""
    config = InstanceConfig(
        instance_id="json_test",
        display_name="JSON测试",
        wxid="wxid_json",
        jizhang_domain="https://api.test.com",
    )

    # model_dump_json (Pydantic v2)
    json_str = config.model_dump_json()
    assert "json_test" in json_str
    assert "JSON测试" in json_str

    # 反序列化往返
    restored = InstanceConfig.model_validate_json(json_str)
    assert restored.instance_id == "json_test"
    assert restored.display_name == "JSON测试"
    assert restored.jizhang_domain == "https://api.test.com"
