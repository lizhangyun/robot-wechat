"""
记账多配置管理单元测试

测试范围:
  - modules/jizhang_config.py : 记账多配置管理

测试内容:
  - JizhangConfig 数据类
  - JizhangConfigManager 初始化
  - load_config() 从临时目录加载
  - load_all() 批量加载
  - save_config() 保存
  - create_config() 创建新配置
  - delete_config() 删除配置
  - domain 自动补全 /6802cishi/ 后缀
  - keyword 加密/解密 (有 AES 密钥时)

对应原软件 data/app/jizhang_c1/, jizhang_c12/ 等多配置目录。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from modules.jizhang_config import JizhangConfig, JizhangConfigManager
from security.keyword_decoder import KeywordDecoder


# 测试用 AES 密钥 (16 字节)
_TEST_AES_KEY = b"0123456789abcdef"


# ============================================================================
# 辅助函数
# ============================================================================
def _write_gbk_ini(filepath: Path, content: str) -> None:
    """将内容以 GBK 编码写入文件"""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(content.encode("gbk"))


# ============================================================================
# 测试: JizhangConfig 数据类
# ============================================================================
def test_jizhang_config_default():
    """JizhangConfig 默认值"""
    config = JizhangConfig()
    assert config.config_id == ""
    assert config.trigger_words == ["记账"]  # 默认触发词
    assert config.bank_whitelist == []
    assert config.domain == ""
    assert config.enabled is True
    assert config.db_key == ""
    assert config.features == {}
    assert config.keyword_hex == ""
    assert config.instance_id == ""


def test_jizhang_config_custom():
    """JizhangConfig 自定义字段"""
    config = JizhangConfig(
        config_id="c1",
        trigger_words=["记账", "入账"],
        bank_whitelist=["工商银行", "微信"],
        domain="http://example.com/6802cishi/",
        enabled=False,
        db_key="secret_key",
        features={"sync": True},
        keyword_hex="abcdef",
        instance_id="c6801",
    )
    assert config.config_id == "c1"
    assert config.trigger_words == ["记账", "入账"]
    assert config.bank_whitelist == ["工商银行", "微信"]
    assert config.domain == "http://example.com/6802cishi/"
    assert config.enabled is False
    assert config.db_key == "secret_key"
    assert config.features == {"sync": True}
    assert config.keyword_hex == "abcdef"
    assert config.instance_id == "c6801"


def test_jizhang_config_to_dict():
    """to_dict 返回完整字典"""
    config = JizhangConfig(config_id="c1", domain="http://x.com/6802cishi/")
    d = config.to_dict()
    assert d["config_id"] == "c1"
    assert d["domain"] == "http://x.com/6802cishi/"
    assert d["enabled"] is True
    assert d["trigger_words"] == ["记账"]
    assert "features" in d
    assert "keyword_hex" in d
    assert "instance_id" in d


def test_jizhang_config_to_keyword_config():
    """to_keyword_config 返回可加密的配置字典"""
    config = JizhangConfig(
        trigger_words=["记账"],
        bank_whitelist=["微信"],
        db_key="key123",
        features={"sync": True},
    )
    kw_config = config.to_keyword_config()
    assert kw_config["trigger_words"] == ["记账"]
    assert kw_config["bank_whitelist"] == ["微信"]
    assert kw_config["db_key"] == "key123"
    assert kw_config["features"] == {"sync": True}
    # 不应包含 config_id / domain 等非 keyword 字段
    assert "config_id" not in kw_config
    assert "domain" not in kw_config


def test_jizhang_config_to_keyword_config_independent_copy():
    """to_keyword_config 返回独立副本"""
    config = JizhangConfig(trigger_words=["a"], bank_whitelist=["b"])
    kw = config.to_keyword_config()
    kw["trigger_words"].append("c")
    # 原配置不应被修改
    assert config.trigger_words == ["a"]


# ============================================================================
# 测试: JizhangConfigManager 初始化
# ============================================================================
def test_manager_init_default():
    """默认初始化"""
    mgr = JizhangConfigManager()
    assert mgr.base_path == Path("data/app")
    assert mgr._aes_key is None
    assert mgr._decoder is None
    assert mgr._cache == {}


def test_manager_init_custom_path(tmp_path):
    """自定义路径初始化"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    assert mgr.base_path == tmp_path


def test_manager_init_with_aes_key(tmp_path):
    """带 AES 密钥初始化"""
    mgr = JizhangConfigManager(base_path=str(tmp_path), aes_key=_TEST_AES_KEY)
    assert mgr._aes_key == _TEST_AES_KEY
    assert mgr._decoder is not None
    assert isinstance(mgr._decoder, KeywordDecoder)


def test_manager_init_invalid_aes_key(tmp_path):
    """无效 AES 密钥长度时不创建解码器"""
    mgr = JizhangConfigManager(base_path=str(tmp_path), aes_key=b"short")
    assert mgr._aes_key == b"short"
    assert mgr._decoder is None  # 密钥长度无效, 不创建解码器


def test_manager_init_empty_aes_key(tmp_path):
    """空 AES 密钥不创建解码器"""
    mgr = JizhangConfigManager(base_path=str(tmp_path), aes_key=None)
    assert mgr._decoder is None


# ============================================================================
# 测试: load_config 加载
# ============================================================================
def test_load_config_nonexistent(tmp_path):
    """加载不存在的配置返回默认禁用配置"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.load_config("c1")
    assert config.config_id == "c1"
    assert config.enabled is False  # 不存在时禁用


def test_load_config_basic(tmp_path):
    """加载基本配置 (无 keyword 解密)"""
    config_dir = tmp_path / "jizhang_c1"
    ini_content = """[jizhang]
domain = http://example.com/6802cishi/
enabled = 1
keyword =
trigger_words = 记账,入账
bank_whitelist = 工商银行,微信
"""
    _write_gbk_ini(config_dir / "config.ini", ini_content)

    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.load_config("c1")
    assert config.config_id == "c1"
    assert config.enabled is True
    assert config.domain == "http://example.com/6802cishi/"
    assert config.trigger_words == ["记账", "入账"]
    assert config.bank_whitelist == ["工商银行", "微信"]


def test_load_config_disabled(tmp_path):
    """加载禁用的配置"""
    config_dir = tmp_path / "jizhang_c2"
    ini_content = """[jizhang]
domain = http://example.com/6802cishi/
enabled = 0
"""
    _write_gbk_ini(config_dir / "config.ini", ini_content)

    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.load_config("c2")
    assert config.enabled is False


def test_load_config_with_keyword_decryption(tmp_path):
    """加载含 keyword 的配置 (有 AES 密钥时解密)"""
    # 先用 KeywordDecoder 加密一份配置
    decoder = KeywordDecoder(_TEST_AES_KEY)
    keyword_hex = decoder.encrypt({
        "trigger_words": ["记账", "收入"],
        "bank_whitelist": ["建设银行", "支付宝"],
        "db_key": "db_secret_123",
        "features": {"sync_enabled": True},
    })

    config_dir = tmp_path / "jizhang_c3"
    ini_content = f"""[jizhang]
domain = http://jizhang.example.com/6802cishi/
enabled = 1
keyword = {keyword_hex}
instance_id = c6802
"""
    _write_gbk_ini(config_dir / "config.ini", ini_content)

    mgr = JizhangConfigManager(base_path=str(tmp_path), aes_key=_TEST_AES_KEY)
    config = mgr.load_config("c3")
    assert config.config_id == "c3"
    assert config.enabled is True
    assert config.trigger_words == ["记账", "收入"]
    assert config.bank_whitelist == ["建设银行", "支付宝"]
    assert config.db_key == "db_secret_123"
    assert config.features == {"sync_enabled": True}
    assert config.instance_id == "c6802"
    assert config.keyword_hex == keyword_hex


def test_load_config_caches(tmp_path):
    """load_config 缓存结果 (第二次返回缓存)"""
    config_dir = tmp_path / "jizhang_c4"
    _write_gbk_ini(
        config_dir / "config.ini",
        "[jizhang]\nenabled = 1\ndomain = http://x.com/6802cishi/\n",
    )
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config1 = mgr.load_config("c4")
    config2 = mgr.load_config("c4")
    # 应返回同一对象 (缓存)
    assert config1 is config2


def test_load_config_clear_cache(tmp_path):
    """clear_cache 后重新从磁盘加载"""
    config_dir = tmp_path / "jizhang_c5"
    _write_gbk_ini(
        config_dir / "config.ini",
        "[jizhang]\nenabled = 1\ndomain = http://x.com/6802cishi/\n",
    )
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config1 = mgr.load_config("c5")
    mgr.clear_cache()
    config2 = mgr.load_config("c5")
    # 清缓存后应重新加载 (不同对象)
    assert config1 is not config2
    assert config1.config_id == config2.config_id


# ============================================================================
# 测试: domain 自动补全 /6802cishi/ 后缀
# ============================================================================
def test_load_config_domain_auto_suffix(tmp_path):
    """domain 不以 /6802cishi/ 结尾时自动补全"""
    config_dir = tmp_path / "jizhang_c6"
    ini_content = """[jizhang]
domain = http://example.com
enabled = 1
"""
    _write_gbk_ini(config_dir / "config.ini", ini_content)

    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.load_config("c6")
    assert config.domain == "http://example.com/6802cishi/"


def test_load_config_domain_already_has_suffix(tmp_path):
    """domain 已有后缀时不重复添加"""
    config_dir = tmp_path / "jizhang_c7"
    ini_content = """[jizhang]
domain = https://jizhang105.tztz.eu.org/6802cishi/
enabled = 1
"""
    _write_gbk_ini(config_dir / "config.ini", ini_content)

    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.load_config("c7")
    assert config.domain == "https://jizhang105.tztz.eu.org/6802cishi/"


def test_load_config_domain_trailing_slash(tmp_path):
    """domain 末尾有斜杠时正确补全"""
    config_dir = tmp_path / "jizhang_c8"
    ini_content = """[jizhang]
domain = http://example.com/
enabled = 1
"""
    _write_gbk_ini(config_dir / "config.ini", ini_content)

    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.load_config("c8")
    # 去掉末尾斜杠后加后缀
    assert config.domain == "http://example.com/6802cishi/"


def test_load_config_domain_empty(tmp_path):
    """domain 为空时不补全"""
    config_dir = tmp_path / "jizhang_c9"
    ini_content = """[jizhang]
enabled = 1
domain =
"""
    _write_gbk_ini(config_dir / "config.ini", ini_content)

    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.load_config("c9")
    assert config.domain == ""


# ============================================================================
# 测试: load_all 批量加载
# ============================================================================
def test_load_all_multiple(tmp_path):
    """批量加载多个配置"""
    for cid, domain in [("c1", "http://a.com"), ("c2", "http://b.com"), ("c12", "http://c.com")]:
        config_dir = tmp_path / f"jizhang_{cid}"
        _write_gbk_ini(
            config_dir / "config.ini",
            f"[jizhang]\nenabled = 1\ndomain = {domain}\n",
        )
    # 同时放入一个非 jizhang_ 目录, 应被忽略
    (tmp_path / "other_dir").mkdir()
    (tmp_path / "other_dir" / "config.ini").write_text("[s]\nk=v\n")

    mgr = JizhangConfigManager(base_path=str(tmp_path))
    configs = mgr.load_all()
    assert len(configs) == 3
    assert "c1" in configs
    assert "c2" in configs
    assert "c12" in configs
    assert configs["c1"].domain == "http://a.com/6802cishi/"
    assert configs["c2"].domain == "http://b.com/6802cishi/"
    assert configs["c12"].domain == "http://c.com/6802cishi/"


def test_load_all_empty(tmp_path):
    """空目录 load_all 返回空字典"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    configs = mgr.load_all()
    assert configs == {}


def test_load_all_nonexistent_dir(tmp_path):
    """根目录不存在时 load_all 返回空字典"""
    mgr = JizhangConfigManager(base_path=str(tmp_path / "nonexistent"))
    configs = mgr.load_all()
    assert configs == {}


# ============================================================================
# 测试: save_config 保存
# ============================================================================
def test_save_config_basic(tmp_path):
    """保存基本配置"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = JizhangConfig(
        config_id="c1",
        domain="http://example.com/6802cishi/",
        enabled=True,
        trigger_words=["记账"],
        bank_whitelist=["微信"],
    )
    mgr.save_config("c1", config)

    # 验证文件已创建
    ini_path = tmp_path / "jizhang_c1" / "config.ini"
    assert ini_path.exists()

    # 重新加载验证
    mgr2 = JizhangConfigManager(base_path=str(tmp_path))
    loaded = mgr2.load_config("c1")
    assert loaded.config_id == "c1"
    assert loaded.enabled is True
    assert loaded.domain == "http://example.com/6802cishi/"
    assert loaded.trigger_words == ["记账"]
    assert loaded.bank_whitelist == ["微信"]


def test_save_config_with_keyword_encryption(tmp_path):
    """保存含 keyword 加密的配置 (有 AES 密钥)"""
    mgr = JizhangConfigManager(base_path=str(tmp_path), aes_key=_TEST_AES_KEY)
    config = JizhangConfig(
        config_id="c2",
        domain="http://example.com/6802cishi/",
        enabled=True,
        trigger_words=["记账", "入账"],
        bank_whitelist=["工商银行"],
        db_key="my_db_key",
        features={"sync": True},
    )
    mgr.save_config("c2", config)

    # 重新加载验证 keyword 解密
    mgr2 = JizhangConfigManager(base_path=str(tmp_path), aes_key=_TEST_AES_KEY)
    loaded = mgr2.load_config("c2")
    assert loaded.trigger_words == ["记账", "入账"]
    assert loaded.bank_whitelist == ["工商银行"]
    assert loaded.db_key == "my_db_key"
    assert loaded.features == {"sync": True}


def test_save_config_disabled(tmp_path):
    """保存禁用配置"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = JizhangConfig(config_id="c3", enabled=False, domain="http://x.com/6802cishi/")
    mgr.save_config("c3", config)

    mgr2 = JizhangConfigManager(base_path=str(tmp_path))
    loaded = mgr2.load_config("c3")
    assert loaded.enabled is False


def test_save_config_creates_directory(tmp_path):
    """save_config 自动创建目录"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = JizhangConfig(config_id="c_new", domain="http://x.com/6802cishi/")
    mgr.save_config("c_new", config)
    assert (tmp_path / "jizhang_c_new" / "config.ini").exists()


def test_save_config_updates_cache(tmp_path):
    """save_config 更新缓存"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = JizhangConfig(config_id="c_cache", domain="http://x.com/6802cishi/")
    mgr.save_config("c_cache", config)
    # 缓存中应有该配置
    assert "c_cache" in mgr._cache
    assert mgr._cache["c_cache"].domain == "http://x.com/6802cishi/"


# ============================================================================
# 测试: create_config 创建新配置
# ============================================================================
def test_create_config_basic(tmp_path):
    """创建新配置"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.create_config("c_new", domain="http://example.com")
    assert config.config_id == "c_new"
    assert config.domain == "http://example.com/6802cishi/"  # 自动补全后缀
    # 文件已保存
    assert (tmp_path / "jizhang_c_new" / "config.ini").exists()


def test_create_config_with_kwargs(tmp_path):
    """create_config 通过 kwargs 设置字段"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.create_config(
        "c_kw",
        domain="http://x.com",
        trigger_words=["记账", "收入"],
        bank_whitelist=["微信", "支付宝"],
        enabled=False,
        db_key="secret",
        instance_id="c6801",
    )
    assert config.config_id == "c_kw"
    assert config.domain == "http://x.com/6802cishi/"
    assert config.trigger_words == ["记账", "收入"]
    assert config.bank_whitelist == ["微信", "支付宝"]
    assert config.enabled is False
    assert config.db_key == "secret"
    assert config.instance_id == "c6801"


def test_create_config_domain_suffix(tmp_path):
    """create_config 自动补全 domain 后缀"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.create_config("c_suf", domain="https://jizhang105.tztz.eu.org")
    assert config.domain == "https://jizhang105.tztz.eu.org/6802cishi/"


def test_create_config_features(tmp_path):
    """create_config 设置 features"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    config = mgr.create_config(
        "c_feat",
        domain="http://x.com",
        features={"sync": True, "split": False},
    )
    assert config.features == {"sync": True, "split": False}


# ============================================================================
# 测试: delete_config 删除配置
# ============================================================================
def test_delete_config_existing(tmp_path):
    """删除已存在的配置"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    mgr.create_config("c_del", domain="http://x.com")
    assert (tmp_path / "jizhang_c_del").exists()

    result = mgr.delete_config("c_del")
    assert result is True
    assert not (tmp_path / "jizhang_c_del").exists()


def test_delete_config_nonexistent(tmp_path):
    """删除不存在的配置返回 False"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    result = mgr.delete_config("nonexistent")
    assert result is False


def test_delete_config_clears_cache(tmp_path):
    """删除配置后清除缓存"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    mgr.create_config("c_cache_del", domain="http://x.com")
    assert "c_cache_del" in mgr._cache

    mgr.delete_config("c_cache_del")
    assert "c_cache_del" not in mgr._cache


# ============================================================================
# 测试: list_config_ids 列出配置 ID
# ============================================================================
def test_list_config_ids(tmp_path):
    """列出所有配置 ID"""
    for cid in ["c1", "c2", "c12"]:
        (tmp_path / f"jizhang_{cid}").mkdir()
    # 非 jizhang 目录应被忽略
    (tmp_path / "other").mkdir()

    mgr = JizhangConfigManager(base_path=str(tmp_path))
    ids = mgr.list_config_ids()
    assert sorted(ids) == ["c1", "c12", "c2"]


def test_list_config_ids_empty(tmp_path):
    """空目录 list_config_ids 返回空列表"""
    mgr = JizhangConfigManager(base_path=str(tmp_path))
    assert mgr.list_config_ids() == []


def test_list_config_ids_nonexistent_dir(tmp_path):
    """根目录不存在时返回空列表"""
    mgr = JizhangConfigManager(base_path=str(tmp_path / "nope"))
    assert mgr.list_config_ids() == []
