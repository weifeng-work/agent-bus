"""控制面密码学单测（架构 §6.1）：canonical / HKDF / HMAC 验签。

用法: python tests/_test_crypto.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_bus import crypto  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main():
    print("== crypto 单测 ==")

    # canonical 确定性
    a = crypto.canonical({"b": 2, "a": [1, {"z": 0}]})
    b = crypto.canonical({"a": [1, {"z": 0}], "b": 2})
    check("canonical 键排序确定性", a == b, a)

    # HKDF 两端一致（模拟 bus_server 与 worker 各自本地派生）
    code = "ABC2345X"
    k1 = crypto.derive_pair_key(code)
    k2 = crypto.derive_pair_key(code)
    k3 = crypto.derive_pair_key("ABC2345Y")
    check("HKDF 同码派生一致", k1 == k2 and len(k1) == 32)
    check("HKDF 异码派生不同", k1 != k3)

    # 签名/验签
    payload = {"op": "shell_exec", "cmd": "dir C:\\", "timeout_seconds": 60}
    sig = crypto.hmac_sign(k1, payload)
    check("验签通过（原 payload）", crypto.hmac_verify(k1, payload, sig))
    check("验签失败（错 key）", not crypto.hmac_verify(k3, payload, sig))
    tampered = dict(payload, cmd="del C:\\*")
    check("验签失败（篡改 cmd）", not crypto.hmac_verify(k1, tampered, sig))
    check("验签失败（空签名）", not crypto.hmac_verify(k1, payload, ""))
    check("验签失败（非法 base64）", not crypto.hmac_verify(k1, payload, "!!not-b64!!"))

    # 签名覆盖范围：增加无关字段不影响原签名验证（如 control_sig 字段本身应排除）
    # 实际实现中 payload 序列化时剔除 control_sig——这里验证 canonical 不含该键的独立性
    signed = dict(payload, control_sig=sig)
    check("canonical 含 control_sig 与原不一致（实现需剔除）",
          crypto.canonical(signed) != crypto.canonical(payload))

    print(f"\n== 结果: {PASS} 通过 / {FAIL} 失败 ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
