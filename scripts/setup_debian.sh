#!/usr/bin/env bash
# Agent Bus Debian/Linux 节点一键安装脚本
# 用法: ./setup_debian.sh <broker服务器IP> [agent_id]
# 前置: 本机已安装 codebuddy CLI 并登录、python3 (>=3.10)、git
set -e

BROKER_HOST="${1:?用法: setup_debian.sh <broker服务器IP> [agent_id]}"
AGENT_ID="${2:-codebuddy_$(hostname -s)}"
INSTALL_DIR="$HOME/agent-bus"
REPO_URL="https://github.com/weifeng-work/agent-bus"

# 1. 获取代码
if [ ! -d "$INSTALL_DIR" ]; then
    git clone "$REPO_URL" "$INSTALL_DIR"
else
    git -C "$INSTALL_DIR" pull --ff-only || echo "仓库已存在，跳过更新"
fi
cd "$INSTALL_DIR"

# 2. 安装执行器最小依赖（不动系统环境）
python3 -m pip install --user --quiet paho-mqtt requests

# 3. 后台启动执行器（自动注册 + 心跳 + 领任务）
pkill -f "codebuddy_executor.py" 2>/dev/null || true   # 重跑脚本时先停旧实例
nohup python3 executor/codebuddy_executor.py \
    --agent-id "$AGENT_ID" \
    --name "CodeBuddy@$(hostname -s)" \
    --broker-host "$BROKER_HOST" \
    --http-base "http://$BROKER_HOST:8000" \
    > "$HOME/agent-bus-executor.log" 2>&1 &

sleep 2
echo "====================================================="
echo " 执行器已启动"
echo "   agent_id   : $AGENT_ID"
echo "   broker     : $BROKER_HOST:1883"
echo "   面板       : http://$BROKER_HOST:8000/"
echo "   日志       : ~/agent-bus-executor.log"
echo "====================================================="
