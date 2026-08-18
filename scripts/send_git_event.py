# -*- coding: utf-8 -*-
"""git_event 参考发送器（G2，协议见 docs/protocol.md §2.7 / docs/git_central_repo.md §6）。

一次性广播 Git 中心仓协调事件到 bus/git_event（QoS 1），bus_server 经既有 bus/#
订阅全量入库，面板时间线可检索。本脚本不注册节点、不占用 inbox。

用法:
  python scripts/send_git_event.py --event pushed --branch task/8a3f2b1c
  python scripts/send_git_event.py --event merged --branch task/8a3f2b1c --commit edd6948 --note "reviewer_main 合入"
  --sender 缺省取环境变量 BUS_AGENT_ID 或本机名。
"""
import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import BusConfig  # noqa: E402

TOPIC = "bus/git_event"
EVENTS = ("pushed", "review_request", "merged", "pull_advisory")


def main():
    p = argparse.ArgumentParser(description="广播 git_event 协调事件")
    p.add_argument("--event", required=True, choices=EVENTS)
    p.add_argument("--repo", default="agent-bus")
    p.add_argument("--branch", default="")
    p.add_argument("--commit", default="")
    p.add_argument("--note", default="")
    p.add_argument("--sender", default="")
    p.add_argument("--broker-host", default=None)
    p.add_argument("--broker-port", type=int, default=None)
    args = p.parse_args()

    cfg = BusConfig.load(broker_host=args.broker_host, broker_port=args.broker_port)
    sender = args.sender or BusConfig().agent_id or f"git-event@{platform.node()}"

    payload = {"event": args.event, "repo": args.repo}
    if args.branch:
        payload["branch"] = args.branch
    if args.commit:
        payload["commit"] = args.commit
    if args.note:
        payload["note"] = args.note
    msg = {
        "protocol_version": "1.0",
        "type": "git_event",
        "sender_id": sender,
        "payload": payload,
        "ts": time.time(),
    }

    import paho.mqtt.client as mqtt
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.connect(cfg.broker_host, cfg.broker_port, keepalive=10)
    info = c.publish(TOPIC, json.dumps(msg, ensure_ascii=False), qos=1)
    info.wait_for_publish(timeout=5)
    c.disconnect()
    print(f"[ok] git_event({args.event}) -> {TOPIC} @ {cfg.broker_host}:{cfg.broker_port}")
    print(json.dumps(msg, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
