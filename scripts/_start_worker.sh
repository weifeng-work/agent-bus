#!/bin/bash
# 启动 debian_worker 执行器（脱离会话，写 PID 文件）
cd /home/weifeng/agent-bus
export NVM_DIR="$HOME/.config/nvm"
. "$NVM_DIR/nvm.sh"
set -a; . ~/.config/agent-bus/bus.env; set +a
pkill -f "codebuddy_executor.py --agent-id debian_worker" 2>/dev/null
sleep 1
nohup python3 executor/codebuddy_executor.py --agent-id debian_worker --name "Debian Worker" \
  >> data/codebuddy_executor.log 2>&1 < /dev/null &
echo $! > data/debian_worker.pid
sleep 4
if kill -0 "$(cat data/debian_worker.pid)" 2>/dev/null; then
  echo "WORKER-ALIVE pid=$(cat data/debian_worker.pid)"
  tail -1 data/codebuddy_executor.log
else
  echo "WORKER-DEAD"
  tail -5 data/codebuddy_executor.log
fi
