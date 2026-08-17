"""通信节点（Comm Node）—— 用户机执行器自愈闭环（需求1）+ 通信/执行解耦（架构 v0.4）。

三层自愈:
  [OS 计划任务] 分钟级 → 拉起本进程（scripts/watchdog.py 判活兜底）
  [本进程]      秒级   → 监督/拉起执行器子进程（崩溃即重启）
  [执行器]      承载   → 智能体任务逻辑（executor/<type>_executor.py）

角色（--role）:
  worker   受控电脑：执行器宿主 + shell/fs 底层操作（需求2/M3）
  hub      主控电脑：控制消息发送端（--shell-exec 子命令，需求2）；逻辑总机 M2 扩展

状态灯（真实 bus 状态，非进程存活，架构 §4.1）:
  绿 = 子进程在 + 状态文件新鲜(<60s) + status=connected
  黄 = 子进程在 + 状态文件过期 或 status=reconnecting
  灰 = 已停止（受控开关关闭 / 子进程不在）

受控开关 = 熔断（架构 §4.2）: 关闭即 kill 执行器进程树 + 写 stopped + 灯灰。

控制面（需求2/M3，架构 §6）:
  - 配对: 一次性安装码（8 位短码）→ 本地派生 K=HKDF(码) → POST /api/pair proof 校验
    → K 存 ~/.config/agent-bus/control.json（worker 验签用）
  - shell_exec: hub 用 K 签名（HMAC）发到 worker inbox → worker 验签 + shell_control
    开关 → 执行（超时硬杀）→ task_result 回执 + control.log + 通知气泡

用法:
  worker:  python executor/comm_node.py --role worker --agent-id node-pc1 \
              --executor-agent-id host-xxxx --executor codebuddy [--pair-code ABC2345X]
  hub 发命令: python executor/comm_node.py --role hub --shell-exec --target node-pc1 \
              --cmd "dir C:\\" [--timeout 30]
  测试:  python executor/comm_node.py --headless --no-bus --child-cmd "..." --test-seconds 10

GUI 依赖（pystray/pillow）懒加载：--headless 免装。
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import provision  # noqa: E402
from agent_bus import crypto  # noqa: E402

log = logging.getLogger("comm_node")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATUS_FRESH_SECONDS = 60.0    # 状态文件新鲜阈值（心跳 30s，两倍余量）
TRAY_HB_INTERVAL = 30.0        # 托盘心跳文件刷新周期
WATCHDOG_STALE_SECONDS = 150.0 # watchdog 判死阈值
SUPERVISE_INTERVAL = 2.0       # 监督循环周期（秒级拉起）

EXECUTOR_TYPES = ("codebuddy", "opencode", "workbuddy", "interactive")

OUTPUT_LIMIT = 20000  # shell_exec 回执正文上限

STATUS_COLORS = {"green": (46, 204, 113), "yellow": (241, 196, 15), "gray": (127, 140, 141)}

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
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def kill_process_tree(pid: int):
    """杀进程树。Windows 用 taskkill /T /F（含子进程），其他平台 terminate。"""
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
    """shell 输出解码：UTF-8 优先，回退 GBK（Windows cmd 默认代码页 936）再 latin-1。"""
    if b is None:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 通信节点
# ---------------------------------------------------------------------------


class CommNode:
    def __init__(self, args):
        self.args = args
        self.role = args.role
        self.agent_id = args.agent_id
        self.name = args.name or f"{self.role}@{os.getenv('COMPUTERNAME') or 'node'}"
        self.executor_type = args.executor
        self.install_dir = Path(args.install_dir).resolve()
        self.headless = args.headless
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

        # 持久化开关
        ctrl_from_file = bool(load_json(self.controlled_file, {"on": True}).get("on", True))
        if args.controlled in ("on", "off"):
            self.controlled = args.controlled == "on"  # 显式覆盖（测试/部署用）
            self._save_controlled()
        else:
            self.controlled = ctrl_from_file
        ccfg = load_json(self.control_cfg_file, {"shell_control": False})
        self.shell_control = bool(ccfg.get("shell_control", False))
        if getattr(args, "enable_shell_control", False):
            self.shell_control = True  # 安装时一次性开启（需求2 §6.3）
        self._save_control_cfg()

        # bus.env → 子进程环境注入
        self.bus_env = load_env_file(self.install_dir / "bus.env") or load_env_file(
            Path.home() / ".config" / "agent-bus" / "bus.env")
        self.broker_host = self.bus_env.get("BUS_BROKER_HOST", "127.0.0.1")
        self.broker_port = int(self.bus_env.get("BUS_BROKER_PORT", "1883"))
        self.http_base = self.bus_env.get("BUS_HTTP_BASE", f"http://{self.broker_host}:8000")

        # 执行器身份（与节点身份分离，架构 §3）
        self.executor_agent_id = getattr(args, "executor_agent_id", "") or self.agent_id

        # 控制面：配对密钥 K（worker 验签 / hub 签名）
        self.control_key = b""      # bytes
        self.control_key_b64 = ""   # base64（落盘）
        self.worker_keys = {}       # hub 侧: worker_id -> key_b64（读 control_keys.json）
        if self.role == "worker":
            self._load_or_pair()
        else:
            self._load_hub_keys()

        # 运行时状态
        self.child = None            # 执行器子进程 Popen
        self._spawn_lock = threading.Lock()
        self.lamp = "gray"           # green / yellow / gray
        self._lamp_lock = threading.Lock()
        self._stop = threading.Event()
        self._respawn_count = 0
        self._last_spawn_ts = 0.0
        self._hb_thread = None
        self._gui = None             # GUI 模式专用（pystray）
        self.bus = None              # 节点自身 MQTT 连接（M3 起）

        self._write_pid()

        if not self.headless:
            self._init_gui()

    # ---------------- 控制面配对（需求2 §6.1） ----------------

    def _load_or_pair(self):
        """worker：读本地 control.json（K）；无则用 --pair-code 配对一次。

        未配对不阻塞安装/运行：节点照常上线，控制面暂不可用；
        可在托盘菜单『输入配对码』或下次带 --pair-code 补配对。
        """
        cfg_dir = Path.home() / ".config" / "agent-bus"
        self.control_file = cfg_dir / "control.json"
        ctrl = load_json(self.control_file, {})
        code = getattr(self.args, "pair_code", "") or ""
        if ctrl.get("key_b64") and ctrl.get("agent_id") == self.agent_id:
            self.control_key_b64 = ctrl["key_b64"]
            self.control_key = base64.b64decode(self.control_key_b64)
            log.info("控制配对已就绪（K 已存在）")
            return
        if ctrl.get("key_b64"):
            log.warning("本地 K 属于 %s，与当前节点 %s 不匹配，需重新配对",
                        ctrl.get("agent_id"), self.agent_id)
        if code:
            ok, msg = self.pair_with_password(code)
            if ok:
                return
            log.error("配对失败: %s", msg)
            return
        log.info("未配对：控制面暂不可用。可在托盘菜单『输入配对码』补配对，"
                 "或下次启动带 --pair-code <密码>")

    def pair_with_password(self, passphrase: str):
        """人工/CLI 输入配对密码 → 本地派生 K → /api/pair → 存 control.json。

        密码只在内存中派生 K，不落网、不落盘；配对成功后密码即作废（一次性）。
        返回 (ok, message)。
        """
        if not passphrase:
            return False, "配对密码为空"
        key = crypto.derive_pair_key(passphrase)
        pw_hash = hashlib.sha256(passphrase.encode("utf-8")).hexdigest()
        claim = {"agent_id": self.agent_id, "device_name": self.name,
                 "code_hash": pw_hash}
        proof = crypto.hmac_sign(key, claim)
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{self.http_base}/api/pair",
                data=json.dumps(claim | {"proof": proof}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return False, f"配对请求失败（bus_server 不可达）: {e}"
        if body.get("ok"):
            self.control_key = key
            self.control_key_b64 = base64.b64encode(key).decode("ascii")
            save_json(self.control_file, {"agent_id": self.agent_id,
                                          "key_b64": self.control_key_b64,
                                          "paired_at": time.time()})
            log.info("控制配对成功（密码一次性，已作废），K 存 %s", self.control_file)
            return True, "配对成功"
        return False, f"配对被拒: {body.get('detail', body)}"

    def _load_hub_keys(self):
        """hub：读 bus_server 写出的 runtime/control_keys.json（同机共享）。"""
        keys_file = self.runtime_dir / "control_keys.json"
        self.worker_keys = load_json(keys_file, {})
        log.info("hub 已加载 %d 个 worker 配对密钥", len(self.worker_keys))

    # ---------------- 总线连接（M3 起，节点自身收发控制消息） ----------------

    def connect_bus(self):
        """节点自身连接总线（worker 收 shell_exec；hub 发控制消息）。

        失败不致命：监督/自愈照常，控制面暂不可用（log 提示）。
        """
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
            if self.role == "hub":
                self.set_lamp("green")
        except Exception as e:
            log.error("节点总线连接失败（控制面暂不可用）: %s", e)
            self.bus = None
            if self.role == "hub":
                self.set_lamp("gray")

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
            # 测试用：--child-cmd 走 shell（Windows 兼容）
            return self.args.child_cmd
        ex = self.executor_type
        script = self.install_dir / "executor" / f"{ex}_executor.py"
        return [sys.executable, str(script), "--agent-id", self.executor_agent_id,
                "--name", self.name]

    def spawn_child(self):
        """拉起执行器子进程（秒级重启入口）。防重入：已有存活子进程则跳过（加锁原子）。"""
        with self._spawn_lock:
            if self.child is not None and self.child.poll() is None:
                return
            self._last_spawn_ts = time.time()
            cmd = self._child_cmd()
            log.info("拉起执行器子进程: %s", " ".join(cmd) if isinstance(cmd, str) else cmd)
            try:
                if isinstance(cmd, str):
                    self.child = subprocess.Popen(cmd, shell=True, env=self._child_env())
                else:
                    self.child = subprocess.Popen(
                        cmd, cwd=str(self.install_dir), env=self._child_env())
                self._respawn_count += 1
            except Exception as e:
                log.error("拉起子进程失败: %s", e)
                self.child = None

    def ensure_child_stopped(self):
        """熔断/退出时：确保执行器进程树已死。"""
        if self.child is not None:
            try:
                if self.child.poll() is None:
                    kill_process_tree(self.child.pid)
            except Exception as e:
                log.warning("停止子进程异常: %s", e)
            self.child = None

    # ---------------- 状态判定（真实 bus 状态） ----------------

    def read_status(self) -> dict:
        """读执行器状态文件，返回 {status, health, ts}（缺失/损坏返回默认）。"""
        return load_json(self.status_file, {"status": "unknown", "health": "unknown", "ts": 0.0})

    def compute_lamp(self) -> str:
        """绿/黄/灰判定（架构 §4.1）。"""
        child_alive = self.child is not None and self.child.poll() is None
        if not self.controlled or not child_alive:
            return "gray"
        st = self.read_status()
        fresh = (time.time() - float(st.get("ts", 0))) < STATUS_FRESH_SECONDS
        if st.get("status") == "connected" and fresh:
            return "green"
        return "yellow"

    def set_lamp(self, lamp: str):
        with self._lamp_lock:
            if lamp != self.lamp:
                self.lamp = lamp
                log.info("[灯] %s", lamp.upper())
                if self._gui:
                    self._gui.update_lamp(lamp)

    # ---------------- 监督循环（秒级） ----------------

    def supervision_loop(self):
        while not self._stop.is_set():
            try:
                if self.role == "worker":
                    if self.controlled:
                        if self.child is None or self.child.poll() is not None:
                            log.warning("执行器子进程不在/已退出，秒级拉起")
                            self.spawn_child()
                        self.set_lamp(self.compute_lamp())
                    else:
                        # 熔断：确保执行器停
                        self.ensure_child_stopped()
                        self.set_lamp("gray")
                else:
                    # hub：灯反映自身总线连接（绿=已连 bus，灰=未连）
                    self.set_lamp("green" if self.bus else "gray")
            except Exception:
                log.exception("监督循环异常")
            self._stop.wait(SUPERVISE_INTERVAL)

    def tray_heartbeat_loop(self):
        """本地心跳文件（供 watchdog 判活）；进程存活即持续刷新。"""
        while not self._stop.is_set():
            try:
                self.hb_file.write_text(str(time.time()), encoding="utf-8")
            except Exception:
                pass
            self._stop.wait(TRAY_HB_INTERVAL)

    # ---------------- 自修复 / 诊断 ----------------

    def self_heal(self) -> str:
        """自修复：检查 Python/依赖/broker 可达性 → 重启执行器。返回报告。"""
        log.info("自修复开始")
        report = []
        try:
            # 1. Python
            report.append(f"python: {sys.executable} ({sys.version.split()[0]})")
            # 2. 关键依赖
            missing = []
            for mod in ("paho.mqtt.client", "requests", "fastapi"):
                try:
                    __import__(mod)
                except ImportError:
                    missing.append(mod)
            report.append(f"依赖缺失: {missing or '无'}")
            if missing:
                r = subprocess.run([sys.executable, "-m", "pip", "install", *missing],
                                   capture_output=True, timeout=300)
                report.append(f"pip 安装: exit={r.returncode}")
            # 3. broker 可达性（HTTP /api/health）
            import urllib.request
            ok = False
            try:
                with urllib.request.urlopen(f"{self.http_base}/api/health", timeout=3) as r:
                    ok = r.status == 200
            except Exception as e:
                report.append(f"broker 探测失败: {e}")
            report.append(f"broker http: {'可达' if ok else '不可达'} ({self.http_base})")
            # 4. 重启执行器
            self.ensure_child_stopped()
            self.spawn_child()
            report.append("执行器已重启")
        except Exception as e:
            report.append(f"自修复异常: {e}")
        summary = "\n".join(report)
        log.info("自修复完成:\n%s", summary)
        if self._gui:
            self._gui.notify("自修复完成", summary[:200])
        return summary

    def collect_diagnostics(self) -> Path:
        """一键收集诊断包：日志+状态+配置 → zip，并打开所在目录。"""
        diag_dir = self.data_dir / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        zip_path = diag_dir / f"agent-bus-diag-{ts}.zip"
        info_lines = [
            f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"host: {os.getenv('COMPUTERNAME', '')} / {os.getenv('USERNAME', '')}",
            f"role: {self.role}  agent_id: {self.agent_id}",
            f"python: {sys.version.split()[0]}",
            f"lamp: {self.lamp}  controlled: {self.controlled}  shell_control: {self.shell_control}",
            f"child: {'alive' if self.child and self.child.poll() is None else 'dead'}",
            f"install_dir: {self.install_dir}",
        ]
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("info.txt", "\n".join(info_lines) + "\n")
                for pat in ("*.log", "*.log.err", "*.err.log"):
                    for f in self.data_dir.glob(pat):
                        try:
                            z.write(f, arcname=f"logs/{f.name}")
                        except Exception:
                            pass
                for f in (self.status_file, self.hb_file, self.pid_file,
                          self.controlled_file, self.control_cfg_file, self.control_log):
                    if f and Path(f).exists():
                        try:
                            z.write(f, arcname=f"runtime/{Path(f).name}")
                        except Exception:
                            pass
                for f in ("bus.env", "device.json"):
                    p = Path.home() / ".config" / "agent-bus" / f
                    if p.exists():
                        z.write(p, arcname=f"config/{f}")
        except Exception as e:
            log.error("诊断包生成失败: %s", e)
        log.info("诊断包已生成: %s", zip_path)
        if self._gui:
            self._gui.open_folder(diag_dir)
        return zip_path

    # ---------------- 受控开关（熔断） ----------------

    def set_controlled(self, on: bool):
        if on == self.controlled:
            return
        self.controlled = on
        self._save_controlled()
        log.info("受控开关 -> %s", "开" if on else "关（熔断）")
        if not on:
            self.ensure_child_stopped()
            self.set_lamp("gray")
        else:
            self.spawn_child()
        if self._gui:
            self._gui.update_menu()

    # ---------------- 托盘 UI（懒加载） ----------------

    def _init_gui(self):
        try:
            import pystray  # noqa: F401
        except ImportError:
            log.error("GUI 模式需要 pystray + pillow：pip install pystray pillow；"
                      "或用 --headless 运行")
            raise SystemExit(2)
        from executor._tray import TrayGui  # 延迟导入，仅 GUI 模式加载
        self._gui = TrayGui(self)

    def notify(self, title: str, message: str):
        if self._gui:
            self._gui.notify(title, message)

    # ---------------- 消息处理（控制面，需求2 §6） ----------------

    def handle_message(self, msg: dict):
        """控制/对话消息分派。M3 实现 shell_exec；executor_*/upgrade 后续里程碑。"""
        if msg.get("type") != "task_request":
            return
        payload = msg.get("payload") or {}
        op = payload.get("op", "run")
        if op == "shell_exec":
            self._handle_shell_exec(msg)
        elif op in ("executor_activate", "executor_deactivate", "upgrade"):
            self._reply(msg, status="error", error=f"op={op} 未实现（后续里程碑）")
        else:
            log.info("[%s] 收到任务消息 op=run from=%s（执行器直接处理，本节点不代收）",
                     self.agent_id, msg.get("sender_id"))

    def _handle_shell_exec(self, msg: dict):
        payload = dict(msg.get("payload") or {})
        sig = payload.pop("control_sig", "")
        cmd = payload.get("cmd", "")
        # 1. 验签（架构 §6.1：无 K 无法伪造合法控制消息）
        if not self.control_key:
            self._reply(msg, status="error", error="未配对（无 K），控制面不可用")
            return
        if not crypto.hmac_verify(self.control_key, payload, sig):
            log.warning("[%s] 拒绝伪造控制消息 from=%s", self.agent_id, msg.get("sender_id"))
            self._reply(msg, status="error", error="验签失败：签名无效或来源不可信")
            return
        # 2. shell_control 开关（需求2 §6.3：装后默认关，开启后免二次确认）
        if not self.shell_control:
            self._reply(msg, status="error", error="shell_control_disabled")
            self._log_control(msg, payload, -1, "拒绝：shell 受控能力未开启")
            return
        # 3. 执行（超时硬杀）
        cwd = payload.get("cwd") or None
        timeout = float(payload.get("timeout_seconds", 60))
        started = time.time()
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                               timeout=timeout)
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
        # 4. 受控可见性（需求2 §6.4：通知气泡，强制）
        self.notify("受控操作", f"主控 {msg.get('sender_id')} 正在执行: {cmd[:120]}")
        log.info("[%s] shell_exec 完成 cmd=%s", self.agent_id, cmd[:80])

    def _reply(self, msg, status, output_text="", error=None):
        if self.bus:
            self.bus.reply_task(msg, output_text=output_text, status=status, error=error)
        else:
            log.warning("[%s] 无总线连接，无法回执 status=%s", self.agent_id, status)

    def _log_control(self, msg, payload, exit_code, summary):
        """本地 control.log（需求2 §6.4 三处留存之一）。"""
        try:
            line = json.dumps({
                "ts": time.time(), "sender": msg.get("sender_id"),
                "op": payload.get("op"), "cmd": payload.get("cmd"),
                "exit": exit_code, "summary": summary,
            }, ensure_ascii=False)
            with open(self.control_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # ---------------- 主流程 ----------------

    def run(self):
        # 节点自身总线连接（worker 收 shell_exec / hub 发控制消息）
        self.connect_bus()
        # 监督 + 心跳线程（GUI/headless 共用）
        threads = [
            threading.Thread(target=self.supervision_loop, daemon=True),
            threading.Thread(target=self.tray_heartbeat_loop, daemon=True),
        ]
        for t in threads:
            t.start()
        if self.controlled and self.role == "worker":
            self.spawn_child()
        if self.headless or not self._gui:
            self.run_headless()
        else:
            self._gui.run()  # 阻塞：托盘消息循环（主线程）

    def run_headless(self):
        """无 GUI 测试模式：监督循环前台运行，打印状态转换，--test-seconds 自动退出。"""
        log.info("[headless] 通信节点启动 role=%s agent=%s executor=%s controlled=%s",
                 self.role, self.agent_id, self.executor_type, self.controlled)
        try:
            deadline = time.time() + self.test_seconds if self.test_seconds else None
            while not self._stop.is_set():
                if deadline and time.time() > deadline:
                    log.info("[headless] 测试时限到，退出（respawn=%s 灯=%s）",
                             self._respawn_count, self.lamp)
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
        log.info("通信节点已退出")


# ---------------------------------------------------------------------------
# hub 一次性发命令（需求2：主控 CLI 形态）
# ---------------------------------------------------------------------------


def run_hub_shell_exec(node: CommNode, args):
    """hub 发 shell_exec：读配对 K → 签名 → 发 MQTT → 等回执 → 打印。"""
    key_b64 = node.worker_keys.get(args.target)
    if not key_b64:
        print(f"error: worker {args.target} 未配对（runtime/control_keys.json 无记录）")
        sys.exit(1)
    key = base64.b64decode(key_b64)
    payload = {"op": "shell_exec", "cmd": args.cmd,
               "timeout_seconds": args.timeout}
    sig = crypto.hmac_sign(key, payload)
    signed_payload = dict(payload, control_sig=sig)

    from agent_bus.schema import make_task_request
    req = make_task_request(node.agent_id, args.target, instruction="",
                            timeout_seconds=args.timeout)
    req["payload"] = signed_payload

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
    ap = argparse.ArgumentParser(description="Agent Bus 通信节点（自愈 + 监督 + 熔断 + 控制面）")
    ap.add_argument("--role", choices=("worker", "hub"), default="worker",
                    help="worker=受控（默认）/ hub=主控")
    ap.add_argument("--agent-id", required=True,
                    help="节点自身身份（worker 收控制消息、hub 发送用）")
    ap.add_argument("--name", default="")
    ap.add_argument("--executor", choices=EXECUTOR_TYPES, default="codebuddy",
                    help="worker 监督并拉起的执行器类型")
    ap.add_argument("--executor-agent-id", default="",
                    help="执行器子进程身份（默认读 device.json，其次 = --agent-id）")
    ap.add_argument("--install-dir", default=str(ROOT_DIR))
    ap.add_argument("--headless", action="store_true", help="无托盘 UI（测试/服务模式）")
    ap.add_argument("--no-bus", action="store_true",
                    help="不连 MQTT（纯监督测试用，控制面不可用）")
    ap.add_argument("--child-cmd", default="", help="测试用：自定义子进程命令（覆盖执行器）")
    ap.add_argument("--controlled", choices=("on", "off"), default="",
                    help="显式覆盖受控开关（默认读 controlled.json；off=熔断）")
    ap.add_argument("--test-seconds", type=int, default=0,
                    help="headless 测试时限（秒），到点自动退出")
    # 控制面（需求2）
    ap.add_argument("--pair-code", default="",
                    help="worker 一次性安装码（配对后即作废，不落盘）")
    ap.add_argument("--enable-shell-control", action="store_true",
                    help="安装时开启 shell 受控能力（默认关）")
    ap.add_argument("--shell-exec", action="store_true",
                    help="hub 子命令：发送 shell_exec 并等待回执")
    ap.add_argument("--target", default="", help="--shell-exec 目标 worker 节点 id")
    ap.add_argument("--cmd", default="", help="--shell-exec 要执行的命令")
    ap.add_argument("--timeout", type=int, default=60, help="shell_exec 超时（秒）")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if args.role == "hub" and args.shell_exec:
        if not args.target or not args.cmd:
            ap.error("--shell-exec 需要 --target 与 --cmd")
        args.headless = True  # 一次性 CLI 模式，无托盘
        run_hub_shell_exec(CommNode(args), args)
        return
    node = CommNode(args)
    try:
        node.run()
    finally:
        node.shutdown()


if __name__ == "__main__":
    main()
