"""控制面密码学原语（架构 v0.4 §6.1）——验签≠加密，仅 HMAC。

- canonical(payload): 确定性序列化（递归排序键），签名/验签共用同一表示
- hmac_sign / hmac_verify: HMAC-SHA256，控制消息防伪造
- derive_pair_key: HKDF-SHA256(安装码) → 配对密钥 K（两端本地各自派生）

注意：本模块只做验签（微秒级），不做消息加密——局域网可信边界内明文传输。
"""
import base64
import hashlib
import hmac
import json

HKDF_INFO = b"agent-bus-ctrl-v1"


def canonical(obj) -> str:
    """确定性 JSON 序列化：键递归排序，去除空白。

    签名与验签必须对同一 payload 表示计算，否则验签必失败。
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def derive_pair_key(pair_code: str) -> bytes:
    """K = HKDF-SHA256(安装码)。两端（bus_server/worker）各自本地调用，结果一致。"""
    ikm = pair_code.encode("utf-8")
    # HKDF-Extract: PRK = HMAC-SHA256(salt=0, ikm)
    prk = hmac.new(b"\x00" * hashlib.sha256().digest_size, ikm, hashlib.sha256).digest()
    # HKDF-Expand: OKM = HMAC(PRK, info || 0x01)
    okm = hmac.new(prk, HKDF_INFO + b"\x01", hashlib.sha256).digest()
    return okm


def hmac_sign(key: bytes, payload: dict) -> str:
    """对 payload 的 canonical 形式签名，返回 base64 签名串。"""
    sig = hmac.new(key, canonical(payload).encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("ascii")


def hmac_verify(key: bytes, payload: dict, signature: str) -> bool:
    """恒时比较验签（防时序侧信道）。"""
    if not signature:
        return False
    try:
        expected = base64.b64decode(signature.encode("ascii"))
    except Exception:
        return False
    actual = hmac.new(key, canonical(payload).encode("utf-8"), hashlib.sha256).digest()
    return hmac.compare_digest(actual, expected)
