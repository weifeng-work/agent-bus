"""_stdout_truncated / parse_codebuddy_output 快速单测。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from executor.codebuddy_executor import _stdout_truncated, parse_codebuddy_output  # noqa: E402

# 截断检测
assert _stdout_truncated('[{"a":1}') is True          # 未闭合
assert _stdout_truncated('[1,2]') is False            # 合法
assert _stdout_truncated('plain text') is False       # 非 JSON
assert _stdout_truncated('') is False                 # 空
assert _stdout_truncated('  [ {"x": "y"} ') is True   # 前导空白未闭合

# 真实截断形态（会话数组在字符串中间戛然而止）
real_shape = '[\n  {\n    "type": "message",\n    "role": "user",\n    "content": [\n      {\n        "type": "input_text",\n        "text": "<system-reminder'
assert _stdout_truncated(real_shape) is True, "真实截断形态应判定为 truncated"

# 解析器：标准事件数组
stream = (
    '[{"type":"message","role":"user","content":[{"type":"input_text","text":"task"}]},'
    '{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hello world"}]},'
    '{"type":"result","result":"final answer","session_id":"s1"}]'
)
out, sid = parse_codebuddy_output(stream)
assert out == "final answer" and sid == "s1", (out, sid)

# result 缺失时兜底取 assistant 文本
stream2 = (
    '[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"B1"}]},'
    '{"type":"message","role":"assistant","content":[{"type":"output_text","text":"B2"}]}]'
)
out2, sid2 = parse_codebuddy_output(stream2)
assert out2 == "B1\nB2" and sid2 is None, (out2, sid2)

# result 为序列化会话数组字符串（Debian 场景）
inner = (
    '[{"type":"message","role":"user","content":[{"type":"input_text","text":"NOISE"}]},'
    '{"type":"message","role":"assistant","content":[{"type":"output_text","text":"CLEAN"}]}]'
)
import json  # noqa: E402
stream3 = json.dumps([
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]},
    {"type": "result", "result": inner, "session_id": "s9"},
])
out3, sid3 = parse_codebuddy_output(stream3)
assert out3 == "CLEAN" and sid3 == "s9", (out3, sid3)

print("ALL PASS")
