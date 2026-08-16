#!/bin/bash
# 验证 64KB 截断：同一个大输出任务分别跑 json / stream-json 对照
export NVM_DIR="$HOME/.config/nvm"
. "$NVM_DIR/nvm.sh"
cd /home/weifeng/agent-bus

PROMPT='依次运行 cat agent_bus/provision.py、cat agent_bus/discovery.py、cat server/bus_server.py 三个命令，然后只回复一行"done"。不要在回复里粘贴源码。'

echo "== A: --output-format json =="
codebuddy -p "$PROMPT" --output-format json -y > /tmp/cb_big_json.out 2>/tmp/cb_big_json.err
echo "exit=$? bytes=$(wc -c < /tmp/cb_big_json.out)"
python3 -c "import json; json.load(open('/tmp/cb_big_json.out'))" 2>&1 | tail -1

echo "== B: --output-format stream-json =="
codebuddy -p "$PROMPT" --output-format stream-json -y > /tmp/cb_big_stream.out 2>/tmp/cb_big_stream.err
echo "exit=$? bytes=$(wc -c < /tmp/cb_big_stream.out)"
python3 -c "import json; json.load(open('/tmp/cb_big_stream.out'))" 2>&1 | tail -1

echo "== 结构速览 =="
python3 - <<'PYEOF'
import json
for name in ("json", "stream"):
    p = f"/tmp/cb_big_{name}.out"
    try:
        raw = open(p, encoding="utf-8").read()
        d = json.loads(raw)
        if isinstance(d, list):
            types = [e.get("type") for e in d if isinstance(e, dict)]
            res = [e for e in d if e.get("type") == "result"]
            print(name, ": VALID list len", len(d), "| types:", types[:8], "... result:", len(res))
    except ValueError as e:
        print(name, ": INVALID ->", e)
PYEOF
