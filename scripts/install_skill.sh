#!/usr/bin/env bash
# Agent Bus 智能体用户（Skill 模式）安装 —— 通信 Skill 自动装入 CodeBuddy skills 目录
# 适合：交互式智能体（CodeBuddy / Claude Code 等）作为"主动协作者"接入总线
# 作用：装完后 CodeBuddy 会话里说"给某某机器的智能体发任务"即可自动触发本 skill
# 用法: ./install_skill.sh <中间架构IP> [agent_id]
set -e

BROKER_HOST="${1:?用法: install_skill.sh <中间架构IP> [agent_id]}"
AGENT_ID="${2:-$(hostname -s)}"
INSTALL_DIR="$HOME/agent-bus-skill"
SKILL_DIR="$HOME/.codebuddy/skills/agent-bus"
ENV_FILE="$HOME/.config/agent-bus/bus.env"
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

# 3. 部署 skill 到 CodeBuddy 用户级 skills 目录（自动发现，所有项目可用）
#    布局: SKILL.md + bus/{cli.py,mcp_server.py} + agent_bus/(SDK，与 bus/ 平级供 import)
mkdir -p "$SKILL_DIR/bus" "$HOME/.config/agent-bus"
cp skill/SKILL.md "$SKILL_DIR/SKILL.md"
cp skill/cli.py skill/mcp_server.py "$SKILL_DIR/bus/"
cp -r agent_bus "$SKILL_DIR/agent_bus"

# 4. 生成环境变量文件（固定路径，skill 命令模板依赖它）
cat > "$ENV_FILE" <<EOF
export BUS_AGENT_ID="$AGENT_ID"
export BUS_BROKER_HOST="$BROKER_HOST"
export BUS_BROKER_PORT=1883
export BUS_HTTP_BASE="http://$BROKER_HOST:8000"
EOF

cat <<EOF

=====================================================
 Skill 模式安装完成（智能体用户 · 无常驻进程）
   skill 位置 : $SKILL_DIR   (CodeBuddy 自动发现)
   运行时     : $SKILL_DIR/bus/cli.py
   环境配置   : $ENV_FILE
   源码副本   : $INSTALL_DIR

 下一步（二选一）:
   A. 直接对 CodeBuddy 说"给 xxx 机器的智能体发任务"——
      skill 会自动触发，它自己知道 source 环境变量并调 CLI
   B. 手动验证:
      source $ENV_FILE
      python3 $SKILL_DIR/bus/cli.py agents   # 应看到总线上在线的智能体

 注意: Skill 模式无守护进程，不能被远程召唤；
       需要被召唤请另跑 scripts/setup_linux.sh（两者可共存）
=====================================================
EOF
