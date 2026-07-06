"""WeComCrypto 单元测试。

重点覆盖 2026-07-06 生产实证的空 receiveid 场景：企业微信「智能机器人」
URL 回调做地址验证时，echostr 解密后的 receiveid 是空字符串；decrypt 必须放行，
否则 URL 验证握手 500、回调保存失败。同时守住"非空 receiveid 仍校验 CorpID"
的跨企业重放防护不被弱化。
"""

import base64
import hashlib
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.services.wx_crypto import WeComCrypto, WeComCryptoError

# 仅供测试的 43 位 EncodingAESKey（非生产密钥）
TEST_AES_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
TEST_TOKEN = "testtoken"
TEST_CORP = "wwtestcorp0000000000"


def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def _encrypt(message: str, receive_id: str, aes_key: str = TEST_AES_KEY) -> str:
    """decrypt 的逆过程：拼 [16 随机][4 长度][msg][receiveid] → pkcs7 → AES-CBC → base64。"""
    key = base64.b64decode(aes_key + "=")
    msg = message.encode("utf-8")
    body = b"0123456789abcdef" + struct.pack("!I", len(msg)) + msg + receive_id.encode("utf-8")
    iv = key[:16]
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(_pkcs7_pad(body)) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def test_encoding_aes_key_length_validated():
    try:
        WeComCrypto(TEST_TOKEN, "tooshort", TEST_CORP)
    except WeComCryptoError:
        return
    raise AssertionError("非 43 位 EncodingAESKey 应被拒绝")


def test_decrypt_roundtrip_with_matching_corp_id():
    crypto = WeComCrypto(TEST_TOKEN, TEST_AES_KEY, TEST_CORP)
    assert crypto.decrypt(_encrypt("hello-echostr", TEST_CORP)) == "hello-echostr"


def test_decrypt_accepts_empty_receive_id_smart_robot_verification():
    # 企微智能机器人 URL 验证 echostr 的 receiveid 为空，必须放行（本次修复核心）
    crypto = WeComCrypto(TEST_TOKEN, TEST_AES_KEY, TEST_CORP)
    assert crypto.decrypt(_encrypt("8914615545705267199", "")) == "8914615545705267199"


def test_decrypt_rejects_nonempty_wrong_receive_id():
    # 非空但不匹配的 receiveid 仍必须拦（跨企业重放防护，安全不弱化）
    crypto = WeComCrypto(TEST_TOKEN, TEST_AES_KEY, TEST_CORP)
    try:
        crypto.decrypt(_encrypt("hi", "wwSOMEOTHERCORP00000"))
    except WeComCryptoError as exc:
        assert "CorpID" in str(exc)
        return
    raise AssertionError("非空且不匹配的 receiveid 应被拒绝")


def test_verify_signature_pass_and_fail():
    crypto = WeComCrypto(TEST_TOKEN, TEST_AES_KEY, TEST_CORP)
    ts, nonce, enc = "1783343920", "1783438926", "someencrypted"
    raw = "".join(sorted([TEST_TOKEN, ts, nonce, enc]))
    good = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    crypto.verify_signature(good, ts, nonce, enc)  # 不抛
    try:
        crypto.verify_signature("deadbeef", ts, nonce, enc)
    except WeComCryptoError:
        return
    raise AssertionError("错误签名应被拒绝")
