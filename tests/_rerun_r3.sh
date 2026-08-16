#!/bin/bash
# 复现长任务（带工具调用）场景，保存原始 stdout 供解析器分析
export NVM_DIR="$HOME/.config/nvm"
. "$NVM_DIR/nvm.sh"
cd /home/weifeng/agent-bus
codebuddy -p "运行命令 hostname -I 和 uname -r，然后用一行文字报告两个结果" \
  --output-format json -y > /tmp/cb_r3.json 2>/tmp/cb_r3.err
echo "exit=$?"
wc -c /tmp/cb_r3.json
echo "--- stderr tail ---"
tail -3 /tmp/cb_r3.err
echo "--- structure ---"
python3 - <<'PYEOF'
import json
raw = open("/tmp/cb_r3.json", encoding="utf-8").read()
try:
    d = json.loads(raw)
    print("VALID-JSON, TOP:", type(d).__name__, "len:", len(d) if isinstance(d, list) else 1)
except ValueError as e:
    print("INVALID-JSON:", e)
    raise SystemExit
for i, e in enumerate(d if isinstance(d, list) else [d]):
    if isinstance(e, dict):
        extra = ""
        if e.get("type") == "result":
            r = e.get("result")
            if isinstance(r, str):
                extra = f" | result: str, len={len(r)}, head={r[:60]!r}"
            else:
                extra = f" | result: {type(r).__name__}"
        print(f"[{i}] type={e.get('type')} role={e.get('role','-')}{extra}")
PYEOF
