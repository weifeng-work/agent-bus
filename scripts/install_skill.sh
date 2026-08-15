#!/usr/bin/env bash
# Agent Bus 智能体用户（Skill 模式）最小安装 —— 只装通信 Skill，不含服务端/执行器
# 适合：交互式智能体（CodeBuddy / Claude Code 等）作为"主动协作者"接入总线
# 用法: ./install_skill.sh <中间架构IP> [agent_id]
set -e

BROKER_HOST="${1:?用法: install_skill.sh <中间架构IP> [agent_id]}"
AGENT_ID="${2:-$(hostname -s)}"
INSTALL_DIR="$HOME/agent-bus-skill"
REPO_URL="https://github.com/weifeng-work/agent-bus"

# 1. 稀疏拉取：只要 skill/（通信规则+CLI）与 agent_bus/（SDK 依赖），其余文件不下载
if [ ! -d "$INSTALL_DIR/.git" ]; then
    git clone --depth 1 --filter=blob:none --sparse "$REPO_URL" "$INSTALL_DIR" 2>/dev/null \
        || git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
git sparse-checkout set skill agent_bus 2>/dev/null || true

# 2. 最小依赖（--user 装入用户目录，不动系统环境）
python3 -m pip install --user --quiet paho-mqtt requests

# 3. 生成环境变量文件，智能体调用 CLI 时自动生效
cat > "$INSTALL_DIR/bus.env" <<EOF
export BUS_AGENT_ID="$AGENT_ID"
export BUS_BROKER_HOST="$BROKER_HOST"
export BUS_BROKER_PORT=1883
export BUS_HTTP_BASE="http://$BROKER_HOST:8000"
EOF

cat <<EOF

=====================================================
 Skill 模式安装完成（智能体用户 · 无常驻进程）
   位置: $INSTALL_DIR
   规则: $INSTALL_DIR/skill/SKILL.md
   环境: $INSTALL_DIR/bus.env

 激活方式（二选一）:
   A. 让智能体每次执行前:  source $INSTALL_DIR/bus.env
   B. 把 bus.env 内容写入智能体的启动环境/规则文件

 验证:
   source $INSTALL_DIR/bus.env
   python3 $INSTALL_DIR/skill/cli.py agents    # 应看到总线上在线的智能体
=====================================================
EOF
