#!/usr/bin/env bash
# Agent Bus Linux 执行器节点接入脚本（通用发行版：Debian/Ubuntu/Fedora/RHEL/Arch 等）
# 角色：被动接活的 Worker——常驻进程守收件箱，收到任务自动拉起 codebuddy headless 执行并回传
# 附带：同时把通信 skill 装入 ~/.codebuddy/skills/（交互式会话也能主动协作）
# 用法: ./setup_linux.sh <broker服务器IP> [agent_id]
# 前置: 本机已安装 codebuddy CLI 并登录、python3 (>=3.10) 含 pip、git
#       （Fedora/RHEL 若无 pip: sudo dnf install python3-pip; Debian/Ubuntu: sudo apt install python3-pip）
set -e

BROKER_HOST="${1:?用法: setup_linux.sh <broker服务器IP> [agent_id]}"
AGENT_ID="${2:-codebuddy_$(hostname -s)}"
INSTALL_DIR="$HOME/agent-bus"
SKILL_DIR="$HOME/.codebuddy/skills/agent-bus"
ENV_FILE="$HOME/.config/agent-bus/bus.env"
REPO_URL="https://github.com/weifeng-work/agent-bus"

# 1. 稀疏拉取：只要 executor/ skill/ agent_bus/（执行器所需），不下载服务端等其余文件
if [ ! -d "$INSTALL_DIR/.git" ]; then
    git clone --depth 1 --filter=blob:none --sparse "$REPO_URL" "$INSTALL_DIR" 2>/dev/null \
        || git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
git sparse-checkout set executor skill agent_bus 2>/dev/null || true

# 2. 安装执行器最小依赖（--user 装入用户目录，不动系统环境）
python3 -m pip install --user --quiet paho-mqtt requests

# 3. 部署通信 skill 到 CodeBuddy 用户级 skills 目录（交互式会话自动可用）
mkdir -p "$SKILL_DIR/bus" "$HOME/.config/agent-bus"
cp skill/SKILL.md "$SKILL_DIR/SKILL.md"
cp skill/cli.py skill/mcp_server.py "$SKILL_DIR/bus/"
cp -r agent_bus "$SKILL_DIR/agent_bus"

# 4. 环境变量（执行器与 skill CLI 共用）
cat > "$ENV_FILE" <<EOF
export BUS_AGENT_ID="$AGENT_ID"
export BUS_BROKER_HOST="$BROKER_HOST"
export BUS_BROKER_PORT=1883
export BUS_HTTP_BASE="http://$BROKER_HOST:8000"
EOF

# 5. 后台启动执行器（自动注册 + 心跳 + 领任务）
pkill -f "codebuddy_executor.py" 2>/dev/null || true   # 重跑脚本时先停旧实例
nohup python3 executor/codebuddy_executor.py \
    --agent-id "$AGENT_ID" \
    --name "CodeBuddy@$(hostname -s)" \
    --broker-host "$BROKER_HOST" \
    --http-base "http://$BROKER_HOST:8000" \
    > "$HOME/agent-bus-executor.log" 2>&1 &

sleep 2
echo "====================================================="
echo " 执行器已启动（Worker 模式，可被远程召唤）"
echo "   agent_id   : $AGENT_ID"
echo "   broker     : $BROKER_HOST:1883"
echo "   面板       : http://$BROKER_HOST:8000/"
echo "   日志       : ~/agent-bus-executor.log"
echo "   通信 skill : $SKILL_DIR (交互式会话亦已具备协作能力)"
echo "   环境配置   : $ENV_FILE"
echo "====================================================="
