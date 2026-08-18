"""Agent Bus 控制节点（对称、托盘可见性、内嵌 MCP Server）—— 单 exe MVP 入口。

三种模式（命令行可选，MCP 可独立启动）：
  默认 / 无参数        托盘 GUI：局域网节点全局可见性 + 受控开关 + 节点命名
  --mcp                独立启动内嵌 MCP Server（供 Claude/Cursor 等作为 MCP Client
                       通过 JSON 配置连接；软件把自身能力封装为标准工具）
  --headless           不起托盘，后台运行（测试/服务用）

用途：
  - 连总线，具备受控能力（被遥控执行本机命令）+ 遥控能力（对他人执行命令）
  - 内嵌 MCP Server，把 发任务/查收件箱/文件读写/执行命令 封装成工具给 AI 用
  - 中心角色（--role hub）额外起 broker + bus_server，成为团队中心
"""
import argparse
import json
import logging
import os
import platform as _platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import discovery          # noqa: E402
from agent_bus.client import AgentBus, BusConfig  # noqa: E402
from agent_bus import provision          # noqa: E402
from agent_bus import schema             # noqa: E402

log = logging.getLogger("control_app")

# ---------------------------------------------------------------------------
# 路径 / 数据目录（兼容 PyInstaller 打包 & 源码运行）
# ---------------------------------------------------------------------------


def app_exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT_DIR


def app_data_dir() -> Path:
    d = app_exe_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_ips():
    try:
        return provision.get_local_ips()
    except Exception:
        return []


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    c = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(suffix=".tmp", prefix=path.stem + "_", dir=str(path.parent))
    try:
        os.write(fd, c.encode("utf-8"))
        os.close(fd)
        fd = None
        os.replace(tmp, str(path))
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# 控制节点
# ---------------------------------------------------------------------------


class ControlNode:
    """对称控制节点：总线连接 + 受控能力 + 发现 + 配置。"""

    def __init__(self, args):
        self.args = args
        self.role = args.role
        self.data_dir = app_data_dir()
        self.config_file = self.data_dir / "node_config.json"
        self.cfg = load_json(self.config_file, {})

        self.agent_id = args.agent_id or self.cfg.get("agent_id") or self._default_id()
        self.name = args.name or self.cfg.get("name") or self._default_id()
        self.shell_control = bool(self.cfg.get("shell_control", True))
        if args.no_control:
            self.shell_control = False
        self.master_flag = (self.role == "hub")

        self.mqtt_port = args.broker_port or 1883
        self.http_port = args.http_port or 8000
        self.discovery_port = args.discovery_port or discovery.DISCOVERY_PORT

        self.broker_proc = None
        self.server_proc = None
        self.bus = None
        self._stop = threading.Event()
        self._found_nodes = []
        self._advertiser = None
        self._rebooted_center = False

    def _default_id(self):
        host = os.getenv("COMPUTERNAME") or socket.gethostname() or "node"
        return f"ctl-{host.lower()}"

    # ---- 配置持久化 ----
    def _save_config(self):
        save_json(self.config_file, {
            "agent_id": self.agent_id, "name": self.name,
            "shell_control": self.shell_control, "updated_at": time.time(),
        })

    def set_name(self, new_name: str):
        if not new_name or not new_name.strip():
            return
        self.name = new_name.strip()
        if self.bus:
            try:
                self.bus.name = self.name
            except Exception:
                pass
        self._save_config()

    def set_shell_control(self, on: bool):
        self.shell_control = bool(on)
        self._save_config()

    # ---- 中心角色端口兜底 ----
    def _select_ports(self):
        if self.role != "hub":
            return
        if not self.args.broker_port:
            self.mqtt_port = discovery.pick_mqtt_port()
        if not self.args.http_port:
            self.http_port = discovery.pick_http_port()
        if not self.args.discovery_port:
            self.discovery_port = discovery.pick_discovery_port()
        log.info("中心端口 mqtt=%s http=%s discovery=%s",
                 self.mqtt_port, self.http_port, self.discovery_port)

    # ---- 发现 ----
    def _beacon(self) -> dict:
        return {
            "proto": discovery.PROTO, "ver": discovery.PROTO_VER,
            "type": "sym_ctl",
            "agent_id": self.agent_id, "name": self.name,
            "host_name": _platform.node(),
            "ips": local_ips(),
            "mqtt_host": self._broker_host, "mqtt_port": self.mqtt_port,
            "http_port": self.http_port,
            "controlled": self.shell_control,
            "discovery_port": self.discovery_port,
            "is_master": self.master_flag,
        }

    def start_advertising(self):
        self._advertiser = discovery.ControlAdvertiser(self._beacon, self.discovery_port)
        self._advertiser.start()

    def refresh_peers(self):
        self._found_nodes = discovery.scan_control_nodes(
            timeout=1.2, discovery_port=self.discovery_port)

    def peers_loop(self):
        while not self._stop.wait(4.0):
            try:
                self.refresh_peers()
                if getattr(self, "on_peers", None):
                    self.on_peers(self._found_nodes)
            except Exception:
                pass

    # ---- 总线 ----
    def connect(self, broker_host=None):
        host = broker_host or getattr(self, "_broker_host", "127.0.0.1")
        self._broker_host = host
        cfg = BusConfig.load(broker_host=host, broker_port=self.mqtt_port,
                             http_base=f"http://{host}:{self.http_port}",
                             agent_id=self.agent_id)
        caps = ["supervise", "shell"] + (["route", "master"] if self.role == "hub" else [])
        self.bus = AgentBus(self.agent_id, name=self.name, capabilities=caps,
                            executor="control", config=cfg)
        self.bus.on_message = self._on_message
        self.bus.connect(register=True, timeout=8)

    # ---- 消息分发：受控能力（shell_exec） ----
    def _on_message(self, msg: dict):
        try:
            if not msg or not isinstance(msg, dict):
                return
            op = (msg.get("payload") or {}).get("op")
            if op == "shell_exec":
                self._handle_shell_exec(msg)
        except Exception as e:
            log.warning("消息处理异常: %s", e)

    def _handle_shell_exec(self, msg: dict):
        payload = dict(msg.get("payload") or {})
        cmd = payload.get("cmd", "")
        sender = msg.get("sender_id", "")
        # 受控能力开关（托盘可关闭）
        if not self.shell_control:
            self._reply(msg, status="error", error="shell_control_disabled")
            return
        if not sender:
            self._reply(msg, status="error", error="缺少 sender_id")
            return
        cwd = payload.get("cwd") or None
        timeout = float(payload.get("timeout_seconds", 60))
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                               timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = self._decode(r.stdout)
            if r.stderr:
                out += ("\n" + self._decode(r.stderr))
            self._reply(msg, status="success" if r.returncode == 0 else "error",
                        output_text=out[:20000],
                        error=None if r.returncode == 0 else f"exit={r.returncode}")
        except subprocess.TimeoutExpired:
            self._reply(msg, status="timeout", error="命令超时")
        except Exception as e:
            self._reply(msg, status="error", error=str(e))

    @staticmethod
    def _decode(b: bytes) -> str:
        if b is None:
            return ""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return b.decode(enc)
            except Exception:
                continue
        return b.decode("utf-8", errors="replace")

    def _reply(self, msg, status, output_text="", error=None):
        if self.bus:
            try:
                self.bus.reply_task(msg, output_text=output_text, status=status, error=error)
            except Exception as e:
                log.warning("回执失败: %s", e)

    # ---- 中心：broker + server ----
    def _broker_exe(self) -> str:
        """找到可用 mosquitto：系统已装优先，否则从内置便携包解压到数据目录。"""
        import shutil
        # 1. 系统装有
        exe = shutil.which("mosquitto")
        if exe:
            return exe
        for base in (r"C:\Program Files\mosquitto", r"C:\mosquitto"):
            p = Path(base) / "mosquitto.exe"
            if p.exists():
                return str(p)
        # 2. 内置便携包（打包后从 sys._MEIPASS 解压到 app_data_dir/runtime/mosquitto）
        mosq_dir = app_data_dir() / "runtime" / "mosquitto"
        exe = mosq_dir / "mosquitto.exe"
        if exe.exists():
            return str(exe)
        # 3. 首启：从内置 zip 解压便携 mosquitto
        self._unpack_mosquitto(mosq_dir)
        return str(exe) if exe.exists() else ""

    def _unpack_mosquitto(self, dest: Path):
        import zipfile
        dest.mkdir(parents=True, exist_ok=True)
        # 打包内嵌路径：sys._MEIPASS/mosquitto/mosquitto.zip；源码路径 build/mosquitto.zip
        candidates = []
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.append(Path(bundle) / "mosquitto" / "mosquitto.zip")
        candidates.append(ROOT_DIR / "build" / "mosquitto.zip")
        for z in candidates:
            if z and z.exists():
                log.info("解压内置 mosquitto: %s → %s", z, dest)
                with zipfile.ZipFile(z) as zf:
                    zf.extractall(dest)
                return
        raise RuntimeError("未找到内置 mosquitto.zip（打包失败或源码缺 build/mosquitto.zip）")

    def start_center(self):
        if self.role != "hub":
            return
        # ---- broker ----
        exe = self._broker_exe()
        if not exe:
            raise RuntimeError("mosquitto 不可用（本机未装且内置包缺失）")
        if self._tcp_open(self.mqtt_port):
            log.info("mqtt %s 已有 broker（复用）", self.mqtt_port)
        else:
            conf_dir = app_data_dir() / "runtime"
            conf_dir.mkdir(parents=True, exist_ok=True)
            conf = conf_dir / "mosquitto.conf"
            conf.write_text(
                f"listener {self.mqtt_port} 0.0.0.0\nallow_anonymous true\n",
                encoding="utf-8")
            self.broker_proc = subprocess.Popen(
                [exe, "-c", str(conf)],
                cwd=str(conf.parent),
                stdout=open(app_data_dir() / "broker.log", "ab"),
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log.info("broker 已启动 pid=%s :%s", self.broker_proc.pid, self.mqtt_port)

        # ---- bus_server（编程式线程内联启动） ----
        try:
            from server.bus_server import serve_bus_server
            db = str(app_data_dir() / "bus.db")
            files_dir = str(app_data_dir() / "files")
            static_dir = self._static_dir()
            store, bridge, app_, uv_server = serve_bus_server(
                host="0.0.0.0", port=self.http_port,
                broker_host="127.0.0.1", broker_port=self.mqtt_port,
                db=db, files_dir=files_dir, static_dir=static_dir)
            self.server_store = store
            self._bridge = bridge
            self._uv_server = uv_server
            threading.Thread(target=uv_server.run, daemon=True).start()
            log.info("bus_server 内联已启动 :%s", self.http_port)
        except Exception as e:
            log.error("bus_server 内联启动失败: %s", e)

    @staticmethod
    def _static_dir() -> str:
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            p = Path(bundle) / "server" / "static"
            if p.is_dir():
                return str(p)
        p = ROOT_DIR / "server" / "static"
        return str(p)

    @staticmethod
    def _tcp_open(port: int, host="127.0.0.1") -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            return False

    # ---- 运行 ----
    def run(self):
        self._select_ports()
        self._broker_host = "127.0.0.1"
        if self.role == "hub":
            self.start_center()
        self.connect(self._broker_host)
        self.start_advertising()
        threading.Thread(target=self.peers_loop, daemon=True).start()

    def shutdown(self):
        self._stop.set()
        if self._advertiser:
            try:
                self._advertiser.stop()
            except Exception:
                pass
        if self.bus:
            try:
                self.bus.disconnect()
            except Exception:
                pass
        log.info("控制节点已退出")


# ---------------------------------------------------------------------------
# 内嵌 MCP Server：把控制节点能力封装成 MCP 工具（可独立 --mcp 启动）
# ---------------------------------------------------------------------------

class EmbeddedMCPServer:
    """把控制节点能力暴露为 MCP 工具，供 Claude Desktop/Cursor 等作为 MCP Client
    通过 JSON 配置连接。FastMCP 走 stdin/stdout 传输，天然适配 MCP 客户端。"""

    def __init__(self, node: ControlNode):
        self.node = node

    def tools(self, mcp):
        from mcp.server.fastmcp import FastMCP

        @mcp.tool()
        def list_online_agents() -> str:
            """查看总线上一在线智能体及其在线状态。"""
            try:
                return json.dumps(node.bus.list_agents(), ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": str(e)})

        @mcp.tool()
        def send_task(target_id: str, instruction: str, file_paths: str = "",
                      wait_seconds: int = 300) -> str:
            """给另一个智能体发任务。wait_seconds>0 阻塞等结果；0 异步发完即回。"""
            try:
                atts = []
                for p in filter(None, [x.strip() for x in file_paths.split(",")]):
                    atts.append(node.bus.upload(p)["url"])
                result = node.bus.send_task(
                    target_id, instruction, attachments=atts,
                    wait=wait_seconds > 0, wait_timeout=wait_seconds or None)
                return json.dumps(result, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": str(e)})

        @mcp.tool()
        def check_inbox(timeout_seconds: float = 3.0) -> str:
            """拉取发给我（本控制节点 agent_id）的新消息。"""
            try:
                msgs = node.bus.poll_inbox(timeout=timeout_seconds)
                return json.dumps(msgs, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": str(e)})

        @mcp.tool()
        def run_command(command: str, cwd: str = "", timeout_seconds: int = 60) -> str:
            """在本机执行命令行命令并返回输出（受控能力）。"""
            try:
                r = subprocess.run(command, shell=True,
                                   cwd=cwd or None, capture_output=True,
                                   timeout=timeout_seconds,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = ControlNode._decode(r.stdout)
                if r.stderr:
                    out += "\n" + ControlNode._decode(r.stderr)
                return json.dumps({"exit": r.returncode, "output": out[:20000]},
                                  ensure_ascii=False)
            except subprocess.TimeoutExpired:
                return json.dumps({"error": "timeout"})
            except Exception as e:
                return json.dumps({"error": str(e)})

        @mcp.tool()
        def read_file(path: str) -> str:
            """读取本机文本文件内容（用于把代码文件内容交给智能体）。"""
            try:
                p = Path(path)
                if not p.exists():
                    return json.dumps({"error": f"不存在: {path}"})
                data = p.read_bytes()
                text = data.decode("utf-8", errors="replace")[:50000]
                return json.dumps({"path": str(p), "content": text}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": str(e)})

        @mcp.tool()
        def upload_file(path: str) -> str:
            """上传本地文件到总线文件服务，返回可放入消息的 URL。"""
            try:
                return json.dumps(node.bus.upload(path), ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": str(e)})

        @mcp.tool()
        def download_file(url: str, dest: str) -> str:
            """下载总线上的文件到本机路径。"""
            try:
                return json.dumps({"saved_to": node.bus.download(url, dest)})
            except Exception as e:
                return json.dumps({"error": str(e)})

        @mcp.tool()
        def reply_task(request_json: str, output_text: str, status: str = "success") -> str:
            """回传任务结果。request_json 传 check_inbox 收到的原始请求。"""
            try:
                req = json.loads(request_json)
                node.bus.reply_task(req, output_text=output_text, status=status)
                return json.dumps({"ok": True})
            except Exception as e:
                return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# 托盘
# ---------------------------------------------------------------------------

def run_tray(node: "ControlNode"):
    import pystray
    from PIL import Image, ImageDraw

    def _make_icon(on_count: int) -> "Image":
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        color = (29, 201, 129, 255) if on_count > 0 else (200, 200, 200, 255)
        d.ellipse([4, 4, 28, 28], fill=color)
        return img

    # ---- 回调（定义在 build_menu 之前，闭包运行时解析） ----
    def _menu_rename(icon=None, item=None):
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk(); root.withdraw()
        new = simpledialog.askstring("设置节点名", "输入节点名（默认电脑名）:",
                                     initialvalue=node.name)
        node.set_name(new)

    def _menu_toggle_shell(icon=None, item=None):
        node.set_shell_control(not node.shell_control)

    def _menu_exec(peer):
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk(); root.withdraw()
        cmd = simpledialog.askstring("远程执行", f"在 {peer.get('name')} 执行命令:",
                                     initialvalue="echo hello")
        if cmd:
            threading.Thread(target=_exec_worker, args=(node, peer, cmd), daemon=True).start()

    def _open_http(peer):
        import webbrowser
        webbrowser.open(f"http://{peer.get('host_ip')}:{peer.get('http_port')}/")

    def _open_center(icon=None, item=None):
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{node.http_port}/")

    def _quit(icon, item, *a):
        icon.stop()

    def _build_menu():
        items = [
            pystray.MenuItem(f"Agent Bus [{node.role}] {node.name}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置节点名…", _menu_rename, enabled=True),
            pystray.MenuItem(
                "受控能力（允许被遥控执行命令）", _menu_toggle_shell,
                checked=lambda it: node.shell_control),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("局域网节点数: %d" % len(node._found_nodes), None, enabled=False),
        ]
        for n in node._found_nodes[:20]:
            sub = pystray.Menu(
                pystray.MenuItem("在此电脑执行命令", lambda i, it, nn=n: _menu_exec(nn)),
                pystray.MenuItem("打开面板", lambda i, it, nn=n: _open_http(nn)),
            )
            items.append(pystray.MenuItem(f"{n.get('name')} @ {n.get('host_name','')}",
                                          submenu=sub))
        items.append(pystray.Menu.SEPARATOR)
        if node.role == "hub":
            items.append(pystray.MenuItem("打开中心面板", _open_center))
        items.append(pystray.MenuItem("退出", _quit))
        return pystray.Menu(*items)

    icon = pystray.Icon("agent-bus-ctl", _make_icon(0), "Agent Bus", None)

    def _refresh(peers):
        icon.icon = _make_icon(len(peers))
        icon.title = f"Agent Bus [{node.role}] {node.name} · 发现 {len(peers)} 节点"
        try:
            icon.menu = _build_menu()
            icon.update_menu()
        except Exception:
            pass

    node.on_peers = _refresh
    icon.menu = _build_menu()
    icon.run()


def _exec_worker(node, peer, cmd):
    if not node.bus or not peer.get("agent_id"):
        return
    try:
        from agent_bus.schema import make_task_request
        req = make_task_request(node.agent_id, peer["agent_id"],
                                instruction="", timeout_seconds=120)
        req["payload"] = {"op": "shell_exec", "cmd": cmd, "timeout_seconds": 120}
        result = node.bus.send_msg(peer["agent_id"], req, wait=True, wait_timeout=150)
        out = ""
        if result:
            out = result.get("result", {}).get("output_text", "") or result.get("error", "")
        _toast(node, f"远程 {peer.get('name')}", out[:300] or "(无输出)")
    except Exception as e:
        _toast(node, "远程命令失败", str(e))


def _toast(node, title, msg):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk(); root.withdraw()
        messagebox.showinfo(title, msg or "(空)")
    except Exception:
        log.info("[%s] %s", title, msg)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Agent Bus 控制节点（对称 + 托盘 + 内嵌 MCP）")
    ap.add_argument("--role", choices=("worker", "hub"), default="worker")
    ap.add_argument("--agent-id", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--broker-port", type=int, default=0)
    ap.add_argument("--http-port", type=int, default=0)
    ap.add_argument("--discovery-port", type=int, default=0)
    ap.add_argument("--broker-host", default="", help="连外部中心 broker 的地址（worker 用）")
    ap.add_argument("--no-control", action="store_true", help="启动时关闭受控能力")
    ap.add_argument("--headless", action="store_true", help="不起托盘（后台/测试）")
    ap.add_argument("--mcp", action="store_true", help="以 MCP Server 模式启动（stdin/stdout）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    node = ControlNode(args)

    # ----- MCP 模式：独立启动，供智能体 JSON 配置调用 -----
    if args.mcp:
        node._select_ports()
        node._broker_host = args.broker_host or "127.0.0.1"
        node.connect(node._broker_host)
        if node.role == "hub":
            node.start_center()
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("agent-bus-control")
        EmbeddedMCPServer(node).tools(mcp)
        log.info("MCP Server 就绪 agent=%s 传输=stdio", node.agent_id)
        mcp.run()
        node.shutdown()
        return

    # ----- GUI / headless 模式 -----
    node.run()
    if args.headless:
        log.info("headless 模式运行中")
        try:
            while not node._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    else:
        try:
            run_tray(node)
        except Exception as e:
            log.error("托盘异常: %s", e)
        except KeyboardInterrupt:
            pass
    node.shutdown()


if __name__ == "__main__":
    main()
