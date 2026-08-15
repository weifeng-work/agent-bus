"""Agent Bus 通信 CLI —— 供任何具备 Bash/终端能力的智能体直接调用。

配置通过环境变量（或参数覆盖）:
  BUS_BROKER_HOST / BUS_BROKER_PORT / BUS_HTTP_BASE / BUS_AGENT_ID

子命令:
  register --name "名字" --caps code,files     注册并声明存在
  agents                                        查看在线智能体名单
  send --to <id> --text "任务" [--file a.pdf] [--session <sid>] [--wait 300]
  check [--timeout 3]                           拉取收件箱新消息
  reply --req-file req.json --text "结果"       回传任务结果
  upload <路径>                                 上传文件，返回 URL
  download <url|file_id> -o <保存路径>          下载文件
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import AgentBus, BusConfig  # noqa: E402
from agent_bus import files as bus_files  # noqa: E402


def out(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def make_bus(args) -> AgentBus:
    agent_id = args.id or BusConfig.load().agent_id
    if not agent_id:
        sys.exit("错误: 未指定 agent_id（用 --id 或环境变量 BUS_AGENT_ID）")
    return AgentBus(agent_id, name=getattr(args, "name", "") or agent_id,
                    capabilities=(getattr(args, "caps", "") or "").split(",") if getattr(args, "caps", None) else [],
                    config=BusConfig.load())


def cmd_register(args):
    bus = make_bus(args)
    bus.connect(register=True)
    out({"ok": True, "agent_id": bus.agent_id, "message": "已注册并开始心跳"})


def cmd_agents(args):
    cfg = BusConfig.load()
    agents = bus_files.list_agents_http(cfg.http_base)
    out(agents)


def cmd_send(args):
    bus = make_bus(args)
    bus.connect(register=True)
    attachments = []
    for f in (args.file or []):
        info = bus.upload(f)
        attachments.append(info["url"])
        print(f"已上传附件: {f} -> {info['url']}", file=sys.stderr)
    result = bus.send_task(
        args.to, args.text,
        attachments=attachments,
        session_id=args.session,
        wait=args.wait > 0,
        wait_timeout=args.wait or None,
    )
    if result is None:
        out({"ok": False, "error": "等待结果超时或未等待（--wait 0 为 fire-and-forget）"})
    else:
        out(result)
    bus.disconnect()


def cmd_check(args):
    bus = make_bus(args)
    bus.connect(register=True)
    msgs = bus.poll_inbox(timeout=args.timeout)
    out(msgs if msgs else [])
    bus.disconnect()


def cmd_reply(args):
    bus = make_bus(args)
    bus.connect(register=True)
    req = json.loads(Path(args.req_file).read_text(encoding="utf-8"))
    bus.reply_task(req, output_text=args.text, status=args.status,
                   session_id=args.session)
    out({"ok": True, "replied_to": req.get("sender_id"),
         "correlation_id": req.get("correlation_id")})
    bus.disconnect()


def cmd_upload(args):
    cfg = BusConfig.load()
    info = bus_files.upload_file(args.path, cfg.http_base, uploaded_by=BusConfig.load().agent_id)
    out(info)


def cmd_download(args):
    cfg = BusConfig.load()
    dest = bus_files.download_file(args.url, args.out or Path(args.url).name, cfg.http_base)
    out({"ok": True, "saved_to": str(Path(dest).resolve())})


def main():
    parser = argparse.ArgumentParser(description="Agent Bus 通信 CLI")
    parser.add_argument("--id", default=None, help="本节点 agent_id（默认取 BUS_AGENT_ID）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register"); p.add_argument("--name", default=""); p.add_argument("--caps", default="")
    p.set_defaults(fn=cmd_register)

    p = sub.add_parser("agents"); p.set_defaults(fn=cmd_agents)

    p = sub.add_parser("send")
    p.add_argument("--to", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--file", action="append", help="附件路径，可多次")
    p.add_argument("--session", default=None, help="延续对方会话的 session_id")
    p.add_argument("--wait", type=int, default=300, help="等待结果秒数，0 表示不等待")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("check")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("reply")
    p.add_argument("--req-file", required=True, help="收到的 task_request JSON 文件")
    p.add_argument("--text", required=True)
    p.add_argument("--status", default="success", choices=["success", "error"])
    p.add_argument("--session", default=None)
    p.set_defaults(fn=cmd_reply)

    p = sub.add_parser("upload"); p.add_argument("path"); p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("download")
    p.add_argument("url"); p.add_argument("-o", dest="out", default=None)
    p.set_defaults(fn=cmd_download)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
