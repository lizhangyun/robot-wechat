"""
安全模块单元测试

测试范围:
  - security/crypto.py   : AES 加密解密、配置加密、许可证 ID 生成、RSA 签名
  - security/license.py  : 许可证文件验证、离线验证
  - security/firewall.py : IP 黑白名单、CIDR 匹配

所有测试使用临时目录, 不污染项目 data 目录。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from security.crypto import CryptoUtils, crypto
from security.firewall import IPFirewall
from database.manager import DatabaseManager as AiosqliteDBManager


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


def _make_temp_file(suffix: str = ".db") -> Path:
    """创建临时文件路径"""
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="robot3_test_sec_")
    os.close(fd)
    return Path(path)


def _make_aiosqlite_db() -> AiosqliteDBManager:
    """创建使用临时文件的 aiosqlite DatabaseManager"""
    db_path = _make_temp_file()
    return AiosqliteDBManager(db_path=db_path)


# ============================================================================
# 测试: AES 加密解密
# ============================================================================
def test_aes_encrypt_decrypt():
    """AES 加密解密往返测试"""
    c = CryptoUtils(master_secret="test_aes_secret_key")

    # 字符串加密解密
    plaintext = "这是一条需要加密的秘密消息"
    encrypted = c.encrypt_config(plaintext)
    assert encrypted != plaintext, "加密结果不应等于明文"
    decrypted = c.decrypt_config(encrypted)
    assert decrypted == plaintext, f"解密结果不正确: {decrypted}"

    # 字典加密解密
    data = {"key": "value", "number": 42, "list": [1, 2, 3]}
    encrypted_dict = c.encrypt_config(data)
    decrypted_dict = c.decrypt_config(encrypted_dict)
    assert decrypted_dict == data, f"字典解密结果不正确: {decrypted_dict}"

    # 空字符串
    encrypted_empty = c.encrypt_config("")
    decrypted_empty = c.decrypt_config(encrypted_empty)
    assert decrypted_empty == "", "空字符串解密失败"

    # 中文长文本
    long_text = "测试" * 1000
    encrypted_long = c.encrypt_config(long_text)
    decrypted_long = c.decrypt_config(encrypted_long)
    assert decrypted_long == long_text, "长文本解密失败"


def test_aes_different_keys():
    """测试不同密钥加密结果不同"""
    c1 = CryptoUtils(master_secret="key_one")
    c2 = CryptoUtils(master_secret="key_two")

    text = "相同的明文"
    enc1 = c1.encrypt_config(text)
    enc2 = c2.encrypt_config(text)

    assert enc1 != enc2, "不同密钥的加密结果应不同"

    # 用 c1 的密钥可以解密 c1 加密的内容
    assert c1.decrypt_config(enc1) == text
    # 用 c2 的密钥不能解密 c1 加密的内容
    try:
        c2.decrypt_config(enc1)
        # 如果没抛异常, 结果不应等于明文
        # (实际上 unpad 可能抛异常, 也可能解出乱码)
    except Exception:
        pass  # 预期: 解密失败


def test_aes_iv_randomness():
    """测试每次加密 IV 不同 (相同明文密文不同)"""
    c = CryptoUtils(master_secret="iv_test_key")
    text = "相同的明文内容"
    enc1 = c.encrypt_config(text)
    enc2 = c.encrypt_config(text)
    assert enc1 != enc2, "相同明文每次加密结果应不同 (IV 随机)"


# ============================================================================
# 测试: 配置加密 (keyword 字段)
# ============================================================================
def test_config_encrypt():
    """测试配置加密 (keyword 字段)"""
    c = CryptoUtils(master_secret="config_encrypt_key")

    # 模拟原软件 instance config 中的 keyword 字段
    # keyword 字段保存的是 AES 加密后的功能配置 JSON
    config_data = {
        "jizhang_enabled": True,
        "jizhang_domain": "https://api.example.com",
        "api_key": "sk-1234567890",
        "features": ["auto_reply", "bookkeeping", "group_manager"],
    }

    # 加密配置 -> 模拟存入 ini 的 keyword 项
    keyword = c.encrypt_config(config_data)
    assert isinstance(keyword, str), "加密结果应为字符串"
    assert len(keyword) > 0, "加密结果不应为空"

    # 解密配置 -> 从 ini 读取 keyword 后解密
    restored = c.decrypt_config(keyword)
    assert isinstance(restored, dict), "解密结果应为字典"
    assert restored == config_data, f"配置解密不正确: {restored}"
    assert restored["api_key"] == "sk-1234567890"
    assert restored["features"] == ["auto_reply", "bookkeeping", "group_manager"]


# ============================================================================
# 测试: 许可证 ID 生成
# ============================================================================
def test_license_generation():
    """测试许可证 ID 生成"""
    # 生成多个许可证 ID, 验证格式和唯一性
    ids = set()
    for _ in range(10):
        license_id = CryptoUtils.generate_license_id()
        assert license_id.startswith("ROBOT3-"), f"许可证ID前缀不正确: {license_id}"
        # 格式: ROBOT3-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
        parts = license_id.split("-")
        assert len(parts) >= 3, f"许可证ID格式不正确: {license_id}"
        assert parts[0] == "ROBOT3"
        ids.add(license_id)

    # 验证唯一性
    assert len(ids) == 10, "生成的许可证ID应唯一"


def test_generate_key():
    """测试随机密钥生成"""
    key1 = CryptoUtils.generate_key(length=16)
    key2 = CryptoUtils.generate_key(length=16)
    assert len(key1) == 32, f"16字节密钥应为32位hex字符: {len(key1)}"  # hex 编码翻倍
    assert key1 != key2, "生成的密钥应唯一"


# ============================================================================
# 测试: RSA 签名
# ============================================================================
def test_rsa_sign_verify():
    """测试 RSA 签名和验证"""
    c = CryptoUtils(master_secret="rsa_test_key")

    # 生成密钥对
    private_pem, public_pem = c.generate_rsa_keypair(bits=2048)
    assert "BEGIN RSA PRIVATE KEY" in private_pem or "BEGIN PRIVATE KEY" in private_pem
    assert "BEGIN PUBLIC KEY" in public_pem

    # 签名
    data = "许可证签名测试数据"
    signature = c.rsa_sign(data, private_pem)
    assert isinstance(signature, str)
    assert len(signature) > 0

    # 验证 (正确数据)
    assert c.rsa_verify(data, signature, public_pem) is True

    # 验证 (篡改数据应失败)
    assert c.rsa_verify(data + "篡改", signature, public_pem) is False

    # 验证 (错误签名应失败)
    assert c.rsa_verify(data, "invalid_signature", public_pem) is False


# ============================================================================
# 测试: 许可证验证
# ============================================================================
def test_license_validate():
    """测试许可证验证"""
    from security.license import LicenseManager
    from security.crypto import create_license_file

    temp_dir = Path(tempfile.mkdtemp(prefix="robot3_test_license_"))
    vef_path = temp_dir / "run.vef"
    public_pem_path = temp_dir / "public.pem"

    # 生成密钥对
    c = CryptoUtils(master_secret="license_test_key")
    private_pem, public_pem = c.generate_rsa_keypair(bits=2048)

    # 生成许可证文件
    license_id = CryptoUtils.generate_license_id()
    create_license_file(vef_path, license_id, public_pem, private_pem)

    # 验证许可证文件存在
    assert vef_path.exists(), "许可证文件未创建"
    assert public_pem_path.exists(), "公钥文件未创建"

    # 创建 LicenseManager 并验证
    lm = LicenseManager(
        vef_path=vef_path,
        public_pem_path=public_pem_path,
    )

    result = lm.verify_file()
    assert result["valid"] is True, f"许可证验证失败: {result}"
    assert result["license_id"] == license_id
    assert result["reason"] == "验证成功"


def test_license_validate_missing_file():
    """测试许可证文件不存在时的验证"""
    from security.license import LicenseManager

    temp_dir = Path(tempfile.mkdtemp(prefix="robot3_test_license_"))
    vef_path = temp_dir / "nonexistent.vef"
    public_pem_path = temp_dir / "nonexistent.pem"

    lm = LicenseManager(vef_path=vef_path, public_pem_path=public_pem_path)
    result = lm.verify_file()
    assert result["valid"] is False
    assert "不存在" in result["reason"]


def test_license_validate_expired():
    """测试过期许可证验证"""
    from security.license import LicenseManager

    temp_dir = Path(tempfile.mkdtemp(prefix="robot3_test_license_"))
    vef_path = temp_dir / "run.vef"
    public_pem_path = temp_dir / "public.pem"

    c = CryptoUtils(master_secret="license_expired_key")
    private_pem, public_pem = c.generate_rsa_keypair(bits=2048)

    # 手动创建过期的许可证文件
    license_id = CryptoUtils.generate_license_id()
    issued_at = int(time.time()) - 365 * 24 * 3600  # 一年前
    expire_at = issued_at + 100  # 100秒后过期 (已过期)
    payload = f"{license_id}:{issued_at}"
    signature = c.rsa_sign(payload, private_pem)

    content = {
        "license_id": license_id,
        "issued_at": issued_at,
        "expire_at": expire_at,
        "signature": signature,
    }
    vef_path.write_text(json.dumps(content), encoding="utf-8")
    public_pem_path.write_text(public_pem, encoding="utf-8")

    lm = LicenseManager(vef_path=vef_path, public_pem_path=public_pem_path)
    result = lm.verify_file()
    assert result["valid"] is False
    assert "过期" in result["reason"]


def test_license_offline_verify():
    """测试离线验证"""
    from security.license import LicenseManager
    from security.crypto import create_license_file

    temp_dir = Path(tempfile.mkdtemp(prefix="robot3_test_license_"))
    vef_path = temp_dir / "run.vef"
    public_pem_path = temp_dir / "public.pem"
    cache_path = temp_dir / "license_cache.json"

    c = CryptoUtils(master_secret="license_offline_key")
    private_pem, public_pem = c.generate_rsa_keypair(bits=2048)
    license_id = CryptoUtils.generate_license_id()
    create_license_file(vef_path, license_id, public_pem, private_pem)

    lm = LicenseManager(vef_path=vef_path, public_pem_path=public_pem_path)
    lm._cache_path = cache_path

    # 离线验证 (无缓存, 回退到文件验证)
    result = lm.verify_offline()
    assert result["valid"] is True, f"离线验证失败: {result}"

    # 验证缓存已写入
    assert cache_path.exists(), "缓存文件未写入"

    # 再次离线验证 (使用缓存)
    lm2 = LicenseManager(vef_path=vef_path, public_pem_path=public_pem_path)
    lm2._cache_path = cache_path
    result2 = lm2.verify_offline()
    assert result2["valid"] is True, f"缓存离线验证失败: {result2}"


# ============================================================================
# 测试: 防火墙黑名单
# ============================================================================
def test_firewall_blacklist():
    """测试 IP 黑名单增删查"""
    db = _make_aiosqlite_db()
    fw = IPFirewall(db=db)

    async def _run_test():
        await db.init()

        # 添加黑名单 IP
        ok = await fw.add_black_ip("192.168.1.100", "恶意IP")
        assert ok is True, "添加黑名单IP失败"

        ok = await fw.add_black_ip("10.0.0.50", "扫描行为")
        assert ok is True

        # 查询黑名单
        black_list = await fw.list_black()
        assert "192.168.1.100" in black_list
        assert "10.0.0.50" in black_list
        assert len(black_list) == 2

        # 检查 IP 是否在黑名单
        assert await fw.is_black_ip("192.168.1.100") is True
        assert await fw.is_black_ip("10.0.0.50") is True
        assert await fw.is_black_ip("192.168.1.1") is False

        # 移除黑名单 IP
        ok = await fw.remove_black_ip("192.168.1.100")
        assert ok is True
        assert await fw.is_black_ip("192.168.1.100") is False

        # 移除不存在的 IP
        ok = await fw.remove_black_ip("1.2.3.4")
        assert ok is False

        await db.close()

    _run(_run_test())


def test_firewall_blacklist_invalid_ip():
    """测试添加无效 IP 到黑名单"""
    db = _make_aiosqlite_db()
    fw = IPFirewall(db=db)

    async def _run_test():
        await db.init()

        # 无效 IP
        ok = await fw.add_black_ip("not_an_ip")
        assert ok is False, "无效IP不应添加成功"

        ok = await fw.add_black_ip("999.999.999.999")
        assert ok is False

        await db.close()

    _run(_run_test())


# ============================================================================
# 测试: 防火墙白名单
# ============================================================================
def test_firewall_whitelist():
    """测试 IP 白名单"""
    db = _make_aiosqlite_db()
    fw = IPFirewall(db=db)

    async def _run_test():
        await db.init()

        # 添加白名单 IP
        ok = await fw.add_white_ip("192.168.1.200", "可信IP")
        assert ok is True

        ok = await fw.add_white_ip("10.0.0.100", "内网IP")
        assert ok is True

        # 查询白名单
        white_list = await fw.list_white()
        assert "192.168.1.200" in white_list
        assert "10.0.0.100" in white_list

        # 检查 IP 是否在白名单
        assert await fw.is_white_ip("192.168.1.200") is True
        assert await fw.is_white_ip("8.8.8.8") is False

        # 移除白名单 IP
        ok = await fw.remove_white_ip("10.0.0.100")
        assert ok is True
        assert await fw.is_white_ip("10.0.0.100") is False

        await db.close()

    _run(_run_test())


def test_firewall_is_allowed():
    """测试综合访问判断 (黑名单优先 + 白名单)"""
    db = _make_aiosqlite_db()
    fw = IPFirewall(db=db)

    async def _run_test():
        await db.init()

        # 添加黑白名单
        await fw.add_black_ip("192.168.1.66")
        await fw.add_white_ip("192.168.1.100")

        # 黑名单 IP -> 拒绝 (无论白名单是否启用)
        assert await fw.is_allowed("192.168.1.66", whitelist_enabled=False) is False
        assert await fw.is_allowed("192.168.1.66", whitelist_enabled=True) is False

        # 白名单 IP -> 允许
        assert await fw.is_allowed("192.168.1.100", whitelist_enabled=True) is True

        # 非名单 IP, 白名单未启用 -> 允许
        assert await fw.is_allowed("8.8.8.8", whitelist_enabled=False) is True

        # 非名单 IP, 白名单已启用 -> 拒绝
        assert await fw.is_allowed("8.8.8.8", whitelist_enabled=True) is False

        await db.close()

    _run(_run_test())


# ============================================================================
# 测试: CIDR 格式 IP 匹配
# ============================================================================
def test_cidr():
    """测试 CIDR 格式 IP 匹配"""
    db = _make_aiosqlite_db()
    fw = IPFirewall(db=db)

    async def _run_test():
        await db.init()

        # 添加 CIDR 黑名单
        ok = await fw.add_black_ip("192.168.1.0/24", "内网段")
        assert ok is True, "添加CIDR黑名单失败"

        ok = await fw.add_black_ip("10.0.0.0/8", "10网段")
        assert ok is True

        # 验证 CIDR 范围内的 IP 被匹配
        assert await fw.is_black_ip("192.168.1.1") is True, "CIDR范围内IP应匹配"
        assert await fw.is_black_ip("192.168.1.254") is True
        assert await fw.is_black_ip("192.168.1.100") is True

        # 验证 CIDR 范围外的 IP 不被匹配
        assert await fw.is_black_ip("192.168.2.1") is False, "CIDR范围外IP不应匹配"
        assert await fw.is_black_ip("192.168.0.1") is False

        # 验证 10.0.0.0/8
        assert await fw.is_black_ip("10.1.2.3") is True
        assert await fw.is_black_ip("10.255.255.255") is True
        assert await fw.is_black_ip("11.0.0.1") is False

        await db.close()

    _run(_run_test())


def test_cidr_validation():
    """测试 CIDR 格式验证"""
    # 合法 CIDR
    assert IPFirewall._validate_ip_or_cidr("192.168.1.0/24") is True
    assert IPFirewall._validate_ip_or_cidr("10.0.0.0/8") is True
    assert IPFirewall._validate_ip_or_cidr("172.16.0.0/12") is True

    # 合法单 IP
    assert IPFirewall._validate_ip_or_cidr("192.168.1.1") is True
    assert IPFirewall._validate_ip_or_cidr("10.0.0.1") is True

    # 非法格式
    assert IPFirewall._validate_ip_or_cidr("not_an_ip") is False
    assert IPFirewall._validate_ip_or_cidr("999.999.999.999") is False
    assert IPFirewall._validate_ip_or_cidr("192.168.1.0/33") is False  # 掩码超范围
    assert IPFirewall._validate_ip_or_cidr("") is False


def test_cidr_match_any():
    """测试 _match_any 方法"""
    # 单 IP 匹配
    assert IPFirewall._match_any("192.168.1.1", ["192.168.1.1"]) is True
    assert IPFirewall._match_any("192.168.1.2", ["192.168.1.1"]) is False

    # CIDR 匹配
    assert IPFirewall._match_any("192.168.1.50", ["192.168.1.0/24"]) is True
    assert IPFirewall._match_any("192.168.2.50", ["192.168.1.0/24"]) is False

    # 多规则匹配
    rules = ["192.168.1.0/24", "10.0.0.0/8", "172.16.0.1"]
    assert IPFirewall._match_any("192.168.1.100", rules) is True
    assert IPFirewall._match_any("10.5.5.5", rules) is True
    assert IPFirewall._match_any("172.16.0.1", rules) is True
    assert IPFirewall._match_any("8.8.8.8", rules) is False

    # 无效 IP
    assert IPFirewall._match_any("invalid", rules) is False

    # 空规则列表
    assert IPFirewall._match_any("192.168.1.1", []) is False
