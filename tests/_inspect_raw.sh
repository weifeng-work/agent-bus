#!/bin/bash
# 分析最近任务目录的 stdout_raw.json 真实结构
cd /home/weifeng/agent-bus/data/executor_work
d=$(ls -t | grep '^task_' | head -1)
echo "DIR=$d"
ls -la "$d"
python3 - "$d/stdout_raw.json" <<'PYEOF'
import json, sys
p = sys.argv[1]
raw = open(p, encoding="utf-8").read()
print("bytes:", len(raw))
try:
    j = json.loads(raw)
    n = len(j) if isinstance(j, list) else 1
    print("VALID-JSON, TOP:", type(j).__name__, "len:", n)
    items = j if isinstance(j, list) else [j]
    for i, e in enumerate(items):
        if not isinstance(e, dict):
            print(f"[{i}] {type(e).__name__}")
            continue
        extra = ""
        t = e.get("type")
        if t == "result":
            r = e.get("result")
            if isinstance(r, str):
                extra = f" | result:str len={len(r)}"
                # result 字符串本身是不是 JSON？
                s = r.strip()
                if s[:1] in "[{":
                    try:
                        inner = json.loads(s)
                        extra += " (内嵌JSON: " + type(inner).__name__ + ")"
                    except ValueError as ve:
                        extra += " (内嵌JSON解析失败: " + str(ve) + ")"
                print(f"[{i}] type=result{extra}")
                print("    result head:", repr(r[:150]))
                continue
            else:
                extra = f" | result:{type(r).__name__}"
        elif t == "message":
            c = e.get("content")
            ctypes = [x.get("type") for x in c if isinstance(x, dict)] if isinstance(c, list) else type(c).__name__
            extra = f" | content:{ctypes}"
        print(f"[{i}] type={t} role={e.get('role','-')}{extra}")
except ValueError as ex:
    print("INVALID-JSON:", ex)
    print("HEAD:", repr(raw[:200]))
    print("TAIL:", repr(raw[-200:]))
PYEOF
