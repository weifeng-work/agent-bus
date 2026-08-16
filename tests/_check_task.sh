#!/bin/bash
# 查看执行器日志 + 最近任务目录的原始输出状态
tail -8 /home/weifeng/agent-bus/data/codebuddy_executor.log
echo "==="
cd /home/weifeng/agent-bus/data/executor_work || exit 1
d=$(ls -t | grep '^task_' | head -1)
echo "DIR=$d"
ls -la "$d"
for f in "$d"/stdout_raw*.json; do
  [ -f "$f" ] || continue
  b=$(wc -c < "$f")
  if python3 -m json.tool "$f" > /dev/null 2>&1; then v=VALID; else v=INVALID-truncated; fi
  echo "$f: $b bytes, $v"
done
