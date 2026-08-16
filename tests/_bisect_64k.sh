#!/bin/bash
# 二分定位 64KB 截断的触发变量：cwd vs prompt 长度
export NVM_DIR="$HOME/.config/nvm"
. "$NVM_DIR/nvm.sh"

chk() {  # $1=file $2=label
  b=$(wc -c < "$1")
  if python3 -m json.tool "$1" > /dev/null 2>&1; then v=VALID; else v=TRUNCATED; fi
  echo "$2: $b bytes, $v"
}

echo "== D: executor_work cwd + 短 prompt =="
cd /home/weifeng/agent-bus/data/executor_work
codebuddy -p "回复 ok 两个字母即可" --output-format json -y > /tmp/cb_d.out 2>/dev/null
chk /tmp/cb_d.out "D"

echo "== E: 仓库根 cwd + 长 prompt（核实任务原文长度级）=="
cd /home/weifeng/agent-bus
LONG=$(python3 - <<'PYEOF'
body = """【Linux 兼容性代码核实】请在 Debian 上完成代码级核实：

0. 确认 git log -1 是 23ba2b9 或更新；工作目录 /home/weifeng/agent-bus。

A. provision.get_local_ips() 实测（agent_bus/provision.py）：
   python3 -c "from agent_bus import provision; print(provision.get_local_ips())"
   预期含真实局域网 IP。并报告 socket.getaddrinfo 返回什么、hostname -I 兜底是否生效。

B. discovery 定向广播实测：写 5 行脚本向 192.168.31.255:41830 发 JSON，同机另一 socket 能否收到。

C. setup_host.ensure_deps 的 PEP 668：python3 -m pip install --dry-run paho-mqtt 是否报 externally-managed？

D. ensure_broker_linux：which mosquitto；无 sudo 下能否启动临时 listener。

E. join_team.py 只读走查：路径/getpass/setx 仅 nt/UTF-8 有无硬伤。

回复格式：0/A/B/C/D/E 逐条 PASS 或 FAIL(+证据摘要)。"""
print(body)
PYEOF
)
codebuddy -p "$LONG" --output-format json -y > /tmp/cb_e.out 2>/dev/null
chk /tmp/cb_e.out "E"
