"""
keyword 解密器单元测试

测试范围:
  - security/keyword_decoder.py : config.ini 中 [jizhang] 段 keyword 字段 AES 解密

测试内容:
  - KeywordDecoder 初始化与密钥校验
  - encrypt() 加密配置
  - decrypt() 解密 keyword
  - encrypt() -> decrypt() 往返一致性
  - 解密无效数据返回空配置
  - verify() 校验功能
  - 包含 trigger_words / bank_whitelist / db_key 的完整配置
  - 行式分段格式解析

加密方式: AES-ECB + PKCS7 填充, 密文以十六进制字符串存储。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from security.keyword_decoder import KeywordDecoder


# 测试用 AES 密钥 (16 字节)
_TEST_KEY = b"0123456789abcdef"
# 测试用 AES-256 密钥 (32 字节)
_TEST_KEY_32 = b"0123456789abcdef0123456789abcdef"


# ============================================================================
# 测试: 初始化与密钥校验
# ============================================================================
def test_init_with_16_byte_key():
    """16 字节密钥可正常初始化"""
    decoder = KeywordDecoder(_TEST_KEY)
    assert decoder is not None


def test_init_with_24_byte_key():
    """24 字节密钥可正常初始化"""
    decoder = KeywordDecoder(b"0123456789abcdef01234567")
    assert decoder is not None


def test_init_with_32_byte_key():
    """32 字节密钥可正常初始化"""
    decoder = KeywordDecoder(_TEST_KEY_32)
    assert decoder is not None


def test_init_invalid_key_length():
    """非 16/24/32 字节密钥抛 ValueError"""
    with pytest.raises(ValueError):
        KeywordDecoder(b"short")
    with pytest.raises(ValueError):
        KeywordDecoder(b"0123456789ab")  # 12 字节
    with pytest.raises(ValueError):
        KeywordDecoder(b"0123456789abcdef0123456789ab")  # 28 字节


def test_init_non_bytes_key():
    """非 bytes 类型密钥抛 TypeError"""
    with pytest.raises(TypeError):
        KeywordDecoder("0123456789abcdef")  # str 而非 bytes
    with pytest.raises(TypeError):
        KeywordDecoder(123456)


def test_init_bytearray_key():
    """bytearray 类型密钥也可接受"""
    decoder = KeywordDecoder(bytearray(_TEST_KEY))
    assert decoder is not None


# ============================================================================
# 测试: encrypt / decrypt 往返一致性
# ============================================================================
def test_encrypt_decrypt_roundtrip():
    """encrypt -> decrypt 往返一致"""
    decoder = KeywordDecoder(_TEST_KEY)
    config = {
        "trigger_words": ["记账", "入账"],
        "bank_whitelist": ["工商银行", "建设银行"],
        "db_key": "my_secret_db_key",
        "features": {"sync_enabled": True, "split_enabled": False},
    }
    encrypted = decoder.encrypt(config)
    assert isinstance(encrypted, str)
    assert len(encrypted) > 0

    decrypted = decoder.decrypt(encrypted)
    assert decrypted["trigger_words"] == ["记账", "入账"]
    assert decrypted["bank_whitelist"] == ["工商银行", "建设银行"]
    assert decrypted["db_key"] == "my_secret_db_key"
    assert decrypted["features"]["sync_enabled"] is True
    assert decrypted["features"]["split_enabled"] is False


def test_encrypt_returns_hex_string():
    """encrypt 返回十六进制字符串"""
    decoder = KeywordDecoder(_TEST_KEY)
    encrypted = decoder.encrypt({"trigger_words": ["test"]})
    # 十六进制字符串只包含 0-9a-f
    assert all(c in "0123456789abcdef" for c in encrypted)
    # 长度应为偶数 (每字节两个 hex 字符)
    assert len(encrypted) % 2 == 0


def test_encrypt_decrypt_empty_config():
    """空配置往返"""
    decoder = KeywordDecoder(_TEST_KEY)
    encrypted = decoder.encrypt({})
    decrypted = decoder.decrypt(encrypted)
    assert decrypted["trigger_words"] == []
    assert decrypted["bank_whitelist"] == []
    assert decrypted["db_key"] == ""
    assert decrypted["features"] == {}


def test_encrypt_decrypt_with_special_chars():
    """含特殊字符的配置往返"""
    decoder = KeywordDecoder(_TEST_KEY)
    config = {
        "trigger_words": ["记账'引号", "含\"双引号"],
        "bank_whitelist": ["微信支付", "支付宝"],
        "db_key": "key-with-special@chars!",
        "features": {},
    }
    encrypted = decoder.encrypt(config)
    decrypted = decoder.decrypt(encrypted)
    assert decrypted["trigger_words"] == ["记账'引号", '含"双引号']
    assert decrypted["bank_whitelist"] == ["微信支付", "支付宝"]
    assert decrypted["db_key"] == "key-with-special@chars!"


def test_encrypt_decrypt_long_config():
    """长配置往返"""
    decoder = KeywordDecoder(_TEST_KEY_32)
    config = {
        "trigger_words": [f"关键词{i}" for i in range(100)],
        "bank_whitelist": [f"银行{i}" for i in range(50)],
        "db_key": "x" * 200,
        "features": {f"feature_{i}": (i % 2 == 0) for i in range(30)},
    }
    encrypted = decoder.encrypt(config)
    decrypted = decoder.decrypt(encrypted)
    assert len(decrypted["trigger_words"]) == 100
    assert len(decrypted["bank_whitelist"]) == 50
    assert decrypted["db_key"] == "x" * 200
    assert len(decrypted["features"]) == 30


def test_encrypt_non_dict_raises():
    """encrypt 非 dict 抛 TypeError"""
    decoder = KeywordDecoder(_TEST_KEY)
    with pytest.raises(TypeError):
        decoder.encrypt("not a dict")
    with pytest.raises(TypeError):
        decoder.encrypt(["list", "not", "dict"])


# ============================================================================
# 测试: decrypt 无效数据返回空配置
# ============================================================================
def test_decrypt_empty_string():
    """空字符串返回空配置"""
    decoder = KeywordDecoder(_TEST_KEY)
    result = decoder.decrypt("")
    assert result["trigger_words"] == []
    assert result["bank_whitelist"] == []
    assert result["db_key"] == ""
    assert result["features"] == {}


def test_decrypt_whitespace_only():
    """纯空白字符串返回空配置"""
    decoder = KeywordDecoder(_TEST_KEY)
    result = decoder.decrypt("   \n\t  ")
    assert result["trigger_words"] == []


def test_decrypt_invalid_hex():
    """非法十六进制字符串返回空配置"""
    decoder = KeywordDecoder(_TEST_KEY)
    result = decoder.decrypt("zzzzzz")
    assert result["trigger_words"] == []
    assert result["bank_whitelist"] == []
    assert result["db_key"] == ""


def test_decrypt_odd_length_hex():
    """奇数长度十六进制返回空配置"""
    decoder = KeywordDecoder(_TEST_KEY)
    result = decoder.decrypt("abc")  # 奇数长度
    assert result["trigger_words"] == []


def test_decrypt_wrong_key():
    """密钥不匹配时返回空配置 (不抛异常)"""
    decoder1 = KeywordDecoder(_TEST_KEY)
    decoder2 = KeywordDecoder(b"abcdef0123456789")
    encrypted = decoder1.encrypt({"trigger_words": ["记账"]})
    # 用错误密钥解密
    result = decoder2.decrypt(encrypted)
    # 解密失败应返回空配置
    assert result["trigger_words"] == []


def test_decrypt_random_bytes():
    """随机字节解密返回空配置"""
    decoder = KeywordDecoder(_TEST_KEY)
    # 16 字节随机数据 (恰好一个 AES 块)
    result = decoder.decrypt("00112233445566778899aabbccddeeff")
    assert result["trigger_words"] == []


def test_decrypt_returns_independent_copy():
    """decrypt 返回的空配置是独立副本, 修改不影响其他"""
    decoder = KeywordDecoder(_TEST_KEY)
    r1 = decoder.decrypt("")
    r1["trigger_words"].append("test")
    r2 = decoder.decrypt("")
    assert r2["trigger_words"] == []


# ============================================================================
# 测试: verify 校验功能
# ============================================================================
def test_verify_valid_keyword():
    """有效 keyword verify 返回 True"""
    decoder = KeywordDecoder(_TEST_KEY)
    encrypted = decoder.encrypt({"trigger_words": ["记账"]})
    assert decoder.verify(encrypted) is True


def test_verify_empty_keyword():
    """空 keyword verify 返回 False"""
    decoder = KeywordDecoder(_TEST_KEY)
    assert decoder.verify("") is False
    assert decoder.verify("   ") is False


def test_verify_invalid_keyword():
    """无效 keyword verify 返回 False"""
    decoder = KeywordDecoder(_TEST_KEY)
    assert decoder.verify("zzzzzz") is False


def test_verify_wrong_key():
    """错误密钥 verify 返回 False"""
    decoder1 = KeywordDecoder(_TEST_KEY)
    decoder2 = KeywordDecoder(b"abcdef0123456789")
    encrypted = decoder1.encrypt({"trigger_words": ["记账"]})
    assert decoder2.verify(encrypted) is False


# ============================================================================
# 测试: 完整配置 (trigger_words / bank_whitelist / db_key / features)
# ============================================================================
def test_full_config_roundtrip():
    """完整配置往返测试"""
    decoder = KeywordDecoder(_TEST_KEY_32)
    full_config = {
        "trigger_words": ["记账", "入账", "收入", "支出"],
        "bank_whitelist": [
            "工商银行", "建设银行", "农业银行", "中国银行",
            "微信", "支付宝",
        ],
        "db_key": "aes-256-db-secret-key-12345",
        "features": {
            "sync_enabled": True,
            "split_enabled": True,
            "auto_reply": False,
            "max_retries": 3,
        },
    }
    encrypted = decoder.encrypt(full_config)
    decrypted = decoder.decrypt(encrypted)

    assert decrypted["trigger_words"] == full_config["trigger_words"]
    assert decrypted["bank_whitelist"] == full_config["bank_whitelist"]
    assert decrypted["db_key"] == full_config["db_key"]
    assert decrypted["features"] == full_config["features"]
    assert decrypted["features"]["sync_enabled"] is True
    assert decrypted["features"]["max_retries"] == 3


def test_config_with_empty_lists():
    """空列表配置往返"""
    decoder = KeywordDecoder(_TEST_KEY)
    config = {
        "trigger_words": [],
        "bank_whitelist": [],
        "db_key": "",
        "features": {},
    }
    encrypted = decoder.encrypt(config)
    decrypted = decoder.decrypt(encrypted)
    assert decrypted == config


def test_config_features_value_types():
    """features 字段值类型保持"""
    decoder = KeywordDecoder(_TEST_KEY)
    config = {
        "trigger_words": ["t"],
        "bank_whitelist": [],
        "db_key": "k",
        "features": {
            "bool_true": True,
            "bool_false": False,
            "int_val": 42,
            "float_val": 3.14,
            "str_val": "hello",
        },
    }
    encrypted = decoder.encrypt(config)
    decrypted = decoder.decrypt(encrypted)
    assert decrypted["features"]["bool_true"] is True
    assert decrypted["features"]["bool_false"] is False
    assert decrypted["features"]["int_val"] == 42
    assert decrypted["features"]["float_val"] == 3.14
    assert decrypted["features"]["str_val"] == "hello"


# ============================================================================
# 测试: 行式分段格式解析 (兼容原软件风格)
# ============================================================================
def test_decrypt_sectioned_format():
    """解密行式分段格式 (通过 encrypt 无法直接产生, 模拟手动构造)"""
    decoder = KeywordDecoder(_TEST_KEY)
    # 先用 JSON 格式加密 (encrypt 产出的是 JSON), 验证 JSON 路径
    encrypted = decoder.encrypt({
        "trigger_words": ["记账", "入账"],
        "bank_whitelist": ["工商银行"],
        "db_key": "db_secret",
        "features": {"sync": True},
    })
    decrypted = decoder.decrypt(encrypted)
    assert decrypted["trigger_words"] == ["记账", "入账"]


def test_parse_plaintext_json():
    """_parse_plaintext 解析 JSON 格式"""
    import json
    text = json.dumps({
        "trigger_words": ["a", "b"],
        "bank_whitelist": ["c"],
        "db_key": "k",
        "features": {"f": 1},
    })
    result = KeywordDecoder._parse_plaintext(text)
    assert result["trigger_words"] == ["a", "b"]
    assert result["bank_whitelist"] == ["c"]
    assert result["db_key"] == "k"
    assert result["features"]["f"] == 1


def test_parse_plaintext_sectioned_text():
    """_parse_plaintext 解析行式分段格式"""
    text = """[trigger_words]
记账
入账
[bank_whitelist]
工商银行
建设银行
[db_key]
my_db_key
[features]
sync_enabled=true
split_enabled=1
auto_reply=false
"""
    result = KeywordDecoder._parse_plaintext(text)
    assert result["trigger_words"] == ["记账", "入账"]
    assert result["bank_whitelist"] == ["工商银行", "建设银行"]
    assert result["db_key"] == "my_db_key"
    assert result["features"]["sync_enabled"] is True
    assert result["features"]["split_enabled"] is True
    assert result["features"]["auto_reply"] is False


def test_parse_plaintext_empty():
    """_parse_plaintext 空文本返回空配置"""
    result = KeywordDecoder._parse_plaintext("")
    assert result["trigger_words"] == []
    assert result["bank_whitelist"] == []
    assert result["db_key"] == ""
    assert result["features"] == {}


def test_parse_plaintext_with_comments():
    """行式格式支持 # 注释"""
    text = """[trigger_words]
# 这是注释
记账
入账
"""
    result = KeywordDecoder._parse_plaintext(text)
    assert result["trigger_words"] == ["记账", "入账"]


def test_parse_value_bool():
    """_parse_value 解析布尔值"""
    assert KeywordDecoder._parse_value("true") is True
    assert KeywordDecoder._parse_value("True") is True
    assert KeywordDecoder._parse_value("yes") is True
    assert KeywordDecoder._parse_value("on") is True
    assert KeywordDecoder._parse_value("1") is True
    assert KeywordDecoder._parse_value("false") is False
    assert KeywordDecoder._parse_value("False") is False
    assert KeywordDecoder._parse_value("no") is False
    assert KeywordDecoder._parse_value("off") is False
    assert KeywordDecoder._parse_value("0") is False


def test_parse_value_int():
    """_parse_value 解析整数"""
    assert KeywordDecoder._parse_value("42") == 42
    assert KeywordDecoder._parse_value("-5") == -5


def test_parse_value_float():
    """_parse_value 解析浮点数"""
    assert KeywordDecoder._parse_value("3.14") == 3.14
    assert KeywordDecoder._parse_value("-0.5") == -0.5


def test_parse_value_string():
    """_parse_value 解析字符串"""
    assert KeywordDecoder._parse_value("hello") == "hello"
    assert KeywordDecoder._parse_value("工商银行") == "工商银行"


# ============================================================================
# 测试: _hex_to_bytes
# ============================================================================
def test_hex_to_bytes():
    """_hex_to_bytes 十六进制转字节"""
    assert KeywordDecoder._hex_to_bytes("48656c6c6f") == b"Hello"


def test_hex_to_bytes_with_spaces():
    """_hex_to_bytes 容忍空白"""
    assert KeywordDecoder._hex_to_bytes("48 65 6c 6c 6f") == b"Hello"


def test_hex_to_bytes_uppercase():
    """_hex_to_bytes 容忍大写"""
    assert KeywordDecoder._hex_to_bytes("48656C6C6F") == b"Hello"


def test_hex_to_bytes_odd_length_raises():
    """_hex_to_bytes 奇数长度抛 ValueError"""
    with pytest.raises(ValueError):
        KeywordDecoder._hex_to_bytes("abc")
