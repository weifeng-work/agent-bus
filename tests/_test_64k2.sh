#!/bin/bash
# 验证假设：回答正文含大段源码时 stdout 是否在 65536 截断
export NVM_DIR="$HOME/.config/nvm"
. "$NVM_DIR/nvm.sh"
cd /home/weifeng/agent-bus

echo "== C: 回答正文粘贴全部源码（json 格式）=="
codebuddy -p "把 agent_bus/provision.py 和 agent_bus/discovery.py 两个文件的完整源码原样粘贴在你的回复正文里" \
  --output-format json -y > /tmp/cb_paste_json.out 2>/tmp/cb_paste_json.err
echo "exit=$? bytes=$(wc -c < /tmp/cb_paste_json.out)"
python3 -c "import json; d=json.load(open('/tmp/cb_paste_json.out')); print('VALID, len', len(d))" 2>&1 | tail -1
