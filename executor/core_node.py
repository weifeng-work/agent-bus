"""核心控制节点（Core Node）—— Layer 1 底层控制通道。

这是固化在受控机的、等价于无头 SSH 的核心控制通道，极少变动。

职责：
  - MQTT 总线连接、心跳/遗嘱、shell_exec 指令接收与执行（sender 身份检查）
  - 执行器进程树管理（根据指令拉起/停止 Layer 2 业务执行器）
  - 服务内 watchdog 线程：30s 刷新本地心跳文件，检测到过期则自杀（触发 SCM 重启）
  - 读取 state.json 判断运行/停用状态

与旧 comm_node.py 的区别：
  - 剥离全部 pystray GUI 代码与状态灯逻辑（移交到 tray_app.py）
  - 新增服务内 watchdog 自杀线程（防软挂死）
  - 完全 headless 运行，无任何 GUI 依赖

用法:
  python executor/core_node.py --role worker --agent-id node-pc1 \
    --executor-agent-id host-xxxx --executor codebuddy --install-dir C:\agent-bus

启动参数（与 comm_node.py 兼容）:
  python executor/core_node.py --role hub --shell-exec --target node-pc1 \
    --cmd "hostname" --timeout 30
"""
import argparse
import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import crypto  # noqa: E402
from agent_bus import state_machine  # noqa: E402

log = logging.getLogger("core_node")

# Windows 无控制台窗口标志
CW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATUS_FRESH_SECONDS = 60.0    # 状态文件新鲜阈值（心跳 30s，两倍余量）
TRAY_HB_INTERVAL = 30.0        # 心跳文件刷新周期
SUPERVISE_INTERVAL = 2.0       # 监督循环周期（秒级拉起）
WATCHDOG_STALE_SECONDS = 35.0  # 服务内 watchdog 判死阈值（超过此时间未刷新心跳即自杀）
SELF_WATCHDOG_INTERVAL = 5.0   # 服务内 watchdog 检查间隔

EXECUTOR_TYPES = ("codebuddy", "opencode", "workbuddy", "interactive")

OUTPUT_LIMIT = 20000  # shell_exec 回执正文上限


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> dict:
    """解析 bus.env（KEY=VALUE 行），返回 dict；文件不存在返回空。"""
    env = {}
    if path and Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: dict):
    """原子写 JSON：临时文件 + rename，防止并发写损坏（B5）。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix=path.stem + "_", dir=str(path.parent))
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = None
        os.replace(tmp_path, str(path))
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def kill_process_tree(pid: int):
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception as e:
        log.warning("kill 进程树失败 pid=%s: %s", pid, e)


def _decode_bytes(b: bytes) -> str:
    if b is None:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 核心控制节点
# ---------------------------------------------------------------------------


class CoreNode:
    def __init__(self, args):
        self.args = args
        self.role = args.role
        self.agent_id = args.agent_id
        self.name = args.name or f"{self.role}@{os.getenv('COMPUTERNAME') or 'node'}"
        self.executor_type = args.executor
        self.install_dir = Path(args.install_dir).resolve()
        self.headless = True  # core_node 始终 headless（无 GUI）
        self.test_seconds = getattr(args, "test_seconds", 0)

        # 路径
        self.runtime_dir = self.install_dir / "data" / "runtime"
        self.data_dir = self.install_dir / "data"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.status_file = self.runtime_dir / "executor_status.json"
        self.hb_file = self.runtime_dir / "tray_heartbeat.ts"
        self.pid_file = self.runtime_dir / "tray_shell.pid"
        self.controlled_file = self.runtime_dir / "controlled.json"
        self.control_cfg_file = self.runtime_dir / "control_config.json"
        self.control_log = self.data_dir / "control.log"
        # B4：shell_exec 异步执行线程池，避免阻塞 MQTT 网络线程
        self._exec_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

        # 持久化开关
        ctrl_from_file = bool(load_json(self.controlled_file, {"on": True}).get("on", True))
        if args.controlled in ("on", "off"):
            self.controlled = args.controlled == "on"
            self._save_controlled()
        else:
            self.controlled = ctrl_from_file
        ccfg = load_json(self.control_cfg_file, {"shell_control": False})
        self.shell_control = bool(ccfg.get("shell_control", False))
        if getattr(args, "enable_shell_control", False):
            self.shell_control = True
        self._save_control_cfg()

        # bus.env
        self.bus_env = load_env_file(self.install_dir / "bus.env") or load_env_file(
            Path.home() / ".config" / "agent-bus" / "bus.env")
        self.broker_host = self.bus_env.get("BUS_BROKER_HOST", "127.0.0.1")
        self.broker_port = int(self.bus_env.get("BUS_BROKER_PORT", "1883"))
        self.http_base = self.bus_env.get("BUS_HTTP_BASE", f"http://{self.broker_host}:8000")

        # 执行器身份
        self.executor_agent_id = getattr(args, "executor_agent_id", "") or self.agent_id

        # 队列标识：优先从 bus.env 读取（避免 NSSM 命令行参数编码链损坏中文），
        # 回落命令行参数（兼容旧版直接调用）
        self.queue = self.bus_env.get("QUEUE_NAME", "") or getattr(args, "queue", "") or ""

        # 运行时状态
        self.child = None
        self._spawn_lock = threading.Lock()
        self._stop = threading.Event()
        self._respawn_count = 0
        self._last_spawn_ts = 0.0
        self._hb_thread = None
        self._self_watchdog_thread = None
        self.bus = None

        self._write_pid()

        # 读取 state.json 初始化状态
        self._current_state = state_machine.read_state(str(self.install_dir))
        log.info("当前状态机状态: %s", self._current_state)

    # ---------------- 状态机 ----------------

    def _reload_state(self):
        """重新读取 state.json 状态。"""
        self._current_state = state_machine.read_state(str(self.install_dir))
        return self._current_state

    def _is_active(self):
        return self._current_state == state_machine.STATE_ACTIVE

    # ---------------- 总线连接 ----------------

    def connect_bus(self):
        if getattr(self.args, "no_bus", False):
            return
        try:
            from agent_bus import AgentBus, BusConfig
            cfg = BusConfig.load(broker_host=self.broker_host,
                                 broker_port=self.broker_port,
                                 http_base=self.http_base, agent_id=self.agent_id)
            caps = ["supervise", "route"] if self.role == "hub" else \
                   ["supervise", "shell", "fs", "executor_activate"]
            self.bus = AgentBus(self.agent_id, name=self.name, capabilities=caps,
                                executor="comm_node", config=cfg)
            self.bus.on_message = self.handle_message
            self.bus.connect(register=True, timeout=8)
            log.info("[%s] 节点已连接总线", self.agent_id)
        except Exception as e:
            log.error("节点总线连接失败（控制面暂不可用）: %s", e)
            self.bus = None

    # ---------------- 持久化 ----------------

    def _write_pid(self):
        try:
            self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except Exception as e:
            log.warning("pid 文件写入失败: %s", e)

    def _save_controlled(self):
        save_json(self.controlled_file, {"on": self.controlled, "ts": time.time()})

    def _save_control_cfg(self):
        save_json(self.control_cfg_file, {"shell_control": self.shell_control,
                                          "updated_at": time.time()})

    # ---------------- 执行器子进程管理 ----------------

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env["BUS_STATUS_FILE"] = str(self.status_file)
        env.setdefault("BUS_BROKER_HOST", self.broker_host)
        env.setdefault("BUS_BROKER_PORT", str(self.broker_port))
        env.setdefault("BUS_HTTP_BASE", self.http_base)
        return env

    def _child_cmd(self) -> list:
        if self.args.child_cmd:
            return self.args.child_cmd
        ex = self.executor_type
        script = self.install_dir / "executor" / f"{ex}_executor.py"
        return [sys.executable, str(script), "--agent-id", self.executor_agent_id,
                "--name", self.name]

    def spawn_child(self):
        with self._spawn_lock:
            if self.child is not None and self.child.poll() is None:
                return
            self._last_spawn_ts = time.time()
            cmd = self._child_cmd()
            log.info("拉起执行器子进程: %s", " ".join(cmd) if isinstance(cmd, str) else cmd)
            try:
                if isinstance(cmd, str):
                    self.child = subprocess.Popen(cmd, shell=True, env=self._child_env(),
                                                  creationflags=CW)
                else:
                    self.child = subprocess.Popen(
                        cmd, cwd=str(self.install_dir), env=self._child_env(),
                        creationflags=CW)
                self._respawn_count += 1
            except Exception as e:
                log.error("拉起子进程失败: %s", e)
                self.child = None

    def ensure_child_stopped(self):
        if self.child is not None:
            try:
                if self.child.poll() is None:
                    kill_process_tree(self.child.pid)
            except Exception as e:
                log.warning("停止子进程异常: %s", e)
            self.child = None

    # ---------------- 服务内 Watchdog（防软挂死，D3） ----------------

    def _self_watchdog_loop(self):
        """服务内 watchdog 线程：检查心跳文件是否过期。

        心跳文件是纯文本时间戳（str(time.time())），由 tray_heartbeat_loop 写入。
        如果超过 WATCHDOG_STALE_SECONDS 未刷新，说明进程可能软挂死，
        主动退出进程（os._exit），触发 SCM 重启。
        """
        while not self._stop.is_set():
            try:
                # 读取心跳文件（纯文本时间戳）
                hb_ts = 0.0
                if self.hb_file.exists():
                    raw = self.hb_file.read_text(encoding="utf-8").strip()
                    if raw:
                        hb_ts = float(raw)
                age = time.time() - hb_ts
                if age > WATCHDOG_STALE_SECONDS and self._is_active():
                    log.critical("服务内 watchdog 检测到心跳过期（%.1fs > %.1fs），主动退出",
                                 age, WATCHDOG_STALE_SECONDS)
                    # 写入日志后退出，SCM 会自动重启
                    if hasattr(os, "_exit"):
                        os._exit(1)
                    else:
                        sys.exit(1)
            except Exception:
                pass
            self._stop.wait(SELF_WATCHDOG_INTERVAL)

    # ---------------- 心跳线程 ----------------

    def tray_heartbeat_loop(self):
        """本地心跳文件（供服务内 watchdog 判活）。"""
        while not self._stop.is_set():
            try:
                self.hb_file.write_text(str(time.time()), encoding="utf-8")
            except Exception:
                pass
            self._stop.wait(TRAY_HB_INTERVAL)

    # ---------------- 监督循环 ----------------

    def supervision_loop(self):
        while not self._stop.is_set():
            try:
                # 每次循环重新读取 state.json，感知状态变化
                self._reload_state()

                if self._is_active() and self.role == "worker":
                    if self.controlled:
                        if self.child is None or self.child.poll() is not None:
                            log.warning("执行器子进程不在/已退出，秒级拉起")
                            self.spawn_child()
                    else:
                        self.ensure_child_stopped()
                elif not self._is_active() and self.role == "worker":
                    # disabled 状态：确保执行器进程停
                    self.ensure_child_stopped()
                    if self.bus:
                        try:
                            self.bus.disconnect()
                        except Exception:
                            pass
                        self.bus = None
                    log.info("状态机 disabled：执行器已停，总线已断开")
            except Exception:
                log.exception("监督循环异常")
            self._stop.wait(SUPERVISE_INTERVAL)

    # ---------------- 自修复 / 诊断 ----------------

    def self_heal(self) -> str:
        log.info("自修复开始")
        report = []
        try:
            report.append(f"python: {sys.executable} ({sys.version.split()[0]})")
            missing = []
            for mod in ("paho.mqtt.client", "requests", "fastapi"):
                try:
                    __import__(mod)
                except ImportError:
                    missing.append(mod)
            report.append(f"依赖缺失: {missing or '无'}")
            if missing:
                pip_names = ["paho-mqtt" if m == "paho.mqtt.client" else m for m in missing]
                r = subprocess.run([sys.executable, "-m", "pip", "install", *pip_names],
                                   capture_output=True, timeout=300)
                report.append(f"pip 安装: exit={r.returncode}")
            import urllib.request
            ok = False
            try:
                with urllib.request.urlopen(f"{self.http_base}/api/health", timeout=3) as r:
                    ok = r.status == 200
            except Exception as e:
                report.append(f"broker 探测失败: {e}")
            report.append(f"broker http: {'可达' if ok else '不可达'} ({self.http_base})")
            self.ensure_child_stopped()
            self.spawn_child()
            report.append("执行器已重启")
        except Exception as e:
            report.append(f"自修复异常: {e}")
        summary = "\n".join(report)
        log.info("自修复完成:\n%s", summary)
        return summary

    # ---------------- 消息处理（控制面） ----------------

    def handle_message(self, msg: dict):
        if msg.get("type") != "task_request":
            return
        payload = msg.get("payload") or {}
        op = payload.get("op", "run")
        if op == "shell_exec":
            # B4：异步提交到线程池，避免阻塞 MQTT 网络线程
            self._exec_pool.submit(self._handle_shell_exec, msg)
        elif op in ("executor_activate", "executor_deactivate", "upgrade"):
            self._reply(msg, status="error", error=f"op={op} 未实现（后续里程碑）")
        else:
            log.info("[%s] 收到任务消息 op=run from=%s",
                     self.agent_id, msg.get("sender_id"))

    def _handle_shell_exec(self, msg: dict):
        payload = dict(msg.get("payload") or {})
        payload.pop("control_sig", "")
        cmd = payload.get("cmd", "")
        sender_id = msg.get("sender_id", "")

        # 1. sender 身份检查
        if not crypto.is_hub_message(sender_id):
            log.warning("[%s] 拒绝非 hub 控制消息 from=%s", self.agent_id, sender_id)
            self._reply(msg, status="error", error="拒绝：控制消息仅接受 hub 身份发送")
            return

        # 2. shell_control 开关
        if not self.shell_control:
            self._reply(msg, status="error", error="shell_control_disabled")
            self._log_control(msg, payload, -1, "拒绝：shell 受控能力未开启")
            return

        # 3. 执行
        cwd = payload.get("cwd") or None
        timeout = float(payload.get("timeout_seconds", 60))
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                               timeout=timeout, creationflags=CW)
            out = _decode_bytes(r.stdout) + ("\n" + _decode_bytes(r.stderr) if r.stderr else "")
            out = out[:OUTPUT_LIMIT]
            self._reply(msg, status="success" if r.returncode == 0 else "error",
                        output_text=out,
                        error=None if r.returncode == 0 else f"exit_code={r.returncode}")
            self._log_control(msg, payload, r.returncode, out[:200])
        except subprocess.TimeoutExpired:
            self._reply(msg, status="timeout", output_text="",
                        error=f"命令超时（>{timeout}s），已终止")
            self._log_control(msg, payload, -2, "超时")
        except Exception as e:
            self._reply(msg, status="error", output_text="", error=str(e))
            self._log_control(msg, payload, -3, str(e))
        log.info("[%s] shell_exec 完成 cmd=%s", self.agent_id, cmd[:80])

    def _reply(self, msg, status, output_text="", error=None):
        if self.bus:
            self.bus.reply_task(msg, output_text=output_text, status=status, error=error)
        else:
            log.warning("[%s] 无总线连接，无法回执 status=%s", self.agent_id, status)

    def _log_control(self, msg, payload, exit_code, summary):
        """追加写入 control.log（三处留存之一，D8）。

        尝试写入默认路径（data/control.log），如果不可写（如 SYSTEM 服务
        在某些受限环境无法写用户目录）则回退到系统临时目录。
        """
        try:
            line = json.dumps({
                "ts": time.time(), "sender": msg.get("sender_id"),
                "op": payload.get("op"), "cmd": payload.get("cmd"),
                "exit": exit_code, "summary": summary,
            }, ensure_ascii=False)
            try:
                with open(self.control_log, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except (PermissionError, OSError):
                # 回退到系统临时目录，确保审计不丢失
                import tempfile
                fallback = Path(tempfile.gettempdir()) / "agent-bus-control.log"
                with open(fallback, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                log.warning("control.log 回退到临时目录: %s", fallback)
        except Exception:
            pass

    # ---------------- 主流程 ----------------

    def run(self):
        # 日志落盘
        try:
            fh = logging.FileHandler(self.data_dir / "tray_shell.log",
                                    encoding="utf-8", delay=True)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s"))
            log.addHandler(fh)
        except Exception:
            pass

        # 服务模式：如果 state.json 是 disabled，则在此休眠等待启用
        # 这一步由 agent_service.py 的 _wait_for_enable 完成，但 core_node
        # 自身也保留此逻辑，确保直接运行 core_node.py 时也能正确处理 disabled 状态
        if self.role == "worker" and not self._is_active():
            log.info("状态机 disabled：等待启用事件...")
            self._wait_for_active()
            if not self._is_active():
                # 等待超时（测试模式）或 _stop 被设置，直接退出
                log.info("disabled 等待结束，未变成 active，退出")
                return
            log.info("检测到状态机 active，继续启动")

        self.connect_bus()

        # 启动线程
        threads = [
            threading.Thread(target=self.supervision_loop, daemon=True),
            threading.Thread(target=self.tray_heartbeat_loop, daemon=True),
            threading.Thread(target=self._self_watchdog_loop, daemon=True),
        ]
        for t in threads:
            t.start()

        if self.controlled and self.role == "worker" and self._is_active():
            self.spawn_child()

        self.run_headless()

    def _wait_for_active(self, poll_interval: float = 2.0):
        """disabled 状态下休眠，轮询等待 state.json 变为 active。

        在 headless 测试模式下（--test-seconds），也响应超时退出。
        """
        deadline = time.time() + self.test_seconds if self.test_seconds else None
        while not self._stop.is_set():
            self._reload_state()
            if self._is_active():
                return
            if deadline and time.time() > deadline:
                log.info("[headless] 测试时限到，disabled 等待超时退出")
                return
            self._stop.wait(poll_interval)

    def run_headless(self):
        log.info("[headless] 核心控制节点启动 role=%s agent=%s executor=%s controlled=%s state=%s",
                 self.role, self.agent_id, self.executor_type, self.controlled,
                 self._current_state)
        try:
            deadline = time.time() + self.test_seconds if self.test_seconds else None
            while not self._stop.is_set():
                if deadline and time.time() > deadline:
                    log.info("[headless] 测试时限到，退出（respawn=%s）",
                             self._respawn_count)
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    def shutdown(self):
        self._stop.set()
        self.ensure_child_stopped()
        if self.bus:
            try:
                self.bus.disconnect()
            except Exception:
                pass
        log.info("核心控制节点已退出")


# ---------------------------------------------------------------------------
# hub 一次性发命令
# ---------------------------------------------------------------------------


def run_hub_shell_exec(node: CoreNode, args):
    payload = {"op": "shell_exec", "cmd": args.cmd,
               "timeout_seconds": args.timeout}

    from agent_bus.schema import make_task_request
    req = make_task_request(node.agent_id, args.target, instruction="",
                            timeout_seconds=args.timeout)
    req["payload"] = payload

    node.connect_bus()
    if not node.bus:
        print("error: hub 无法连接总线")
        sys.exit(1)
    result = node.bus.send_msg(args.target, req, wait=True,
                               wait_timeout=args.timeout + 30)
    node.bus.disconnect()
    if result is None:
        print("error: 超时未收到回执")
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("status") == "success" else 1)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Agent Bus 核心控制节点（Layer 1 底层控制通道）")
    ap.add_argument("--role", choices=("worker", "hub"), default="worker")
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--executor", choices=EXECUTOR_TYPES, default="codebuddy")
    ap.add_argument("--executor-agent-id", default="")
    ap.add_argument("--install-dir", default=str(ROOT_DIR))
    ap.add_argument("--no-bus", action="store_true")
    ap.add_argument("--child-cmd", default="")
    ap.add_argument("--controlled", choices=("on", "off"), default="")
    ap.add_argument("--test-seconds", type=int, default=0)
    ap.add_argument("--queue", default="")
    ap.add_argument("--enable-shell-control", action="store_true")
    ap.add_argument("--shell-exec", action="store_true")
    ap.add_argument("--target", default="")
    ap.add_argument("--cmd", default="")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if args.role == "hub" and args.shell_exec:
        if not args.target or not args.cmd:
            ap.error("--shell-exec 需要 --target 与 --cmd")
        run_hub_shell_exec(CoreNode(args), args)
        return
    node = CoreNode(args)
    try:
        node.run()
    finally:
        node.shutdown()


if __name__ == "__main__":
    main()