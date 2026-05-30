import base64
import hashlib
import struct
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCryptoError(ValueError):
    pass


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        if len(encoding_aes_key) != 43:
            raise WeComCryptoError("企业微信 EncodingAESKey 必须是 43 位")
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        self.corp_id = corp_id

    def verify_signature(
        self, signature: str, timestamp: str, nonce: str, encrypted: str
    ) -> None:
        raw = "".join(sorted([self.token, timestamp, nonce, encrypted]))
        expected = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        if expected != signature:
            raise WeComCryptoError("企业微信签名校验失败")

    def decrypt(self, encrypted: str) -> str:
        iv = self.key[:16]
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()
        decrypted = self._strip_pkcs7(decrypted)
        message_length = struct.unpack("!I", decrypted[16:20])[0]
        message = decrypted[20 : 20 + message_length]
        receive_id = decrypted[20 + message_length :].decode("utf-8")
        if receive_id != self.corp_id:
            raise WeComCryptoError("企业微信 CorpID 不匹配")
        return message.decode("utf-8")

    @staticmethod
    def extract_encrypt(xml_text: str) -> str:
        root = ET.fromstring(xml_text)
        node = root.find("Encrypt")
        if node is None or not node.text:
            raise WeComCryptoError("企业微信消息缺少 Encrypt 字段")
        return node.text

    @staticmethod
    def _strip_pkcs7(data: bytes) -> bytes:
        pad = data[-1]
        if pad < 1 or pad > 32:
            raise WeComCryptoError("企业微信消息填充异常")
        return data[:-pad]
