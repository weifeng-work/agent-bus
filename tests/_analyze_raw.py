"""Debian 侧分析 /tmp/cb_raw.json 顶层结构 + 验证 _result_to_text 提取效果。"""
import json
import sys

sys.path.insert(0, "/home/weifeng/agent-bus")
from executor.codebuddy_executor import _result_to_text, _assistant_texts, parse_codebuddy_output  # noqa: E402

raw = open("/tmp/cb_raw.json", encoding="utf-8").read()
data = json.loads(raw)
print("TOP:", type(data).__name__, "len:", len(data) if isinstance(data, list) else 1)
if isinstance(data, list):
    for i, e in enumerate(data):
        if isinstance(e, dict):
            extra = ""
            if e.get("type") == "result":
                r = e.get("result")
                extra = f" | result字段类型: {type(r).__name__}"
                if isinstance(r, str):
                    extra += f" 前80字符: {r[:80]!r}"
                elif isinstance(r, list):
                    extra += f" (序列化会话数组, len={len(r)})"
            print(f"  [{i}] type={e.get('type')} role={e.get('role','-')}{extra}")
        else:
            print(f"  [{i}] {type(e).__name__}")

print("\n== parse_codebuddy_output 提取结果 ==")
out, sid = parse_codebuddy_output(raw)
print("session_id:", sid)
print("提取长度:", len(out))
print("提取内容:")
print(out)
