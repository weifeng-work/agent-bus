"""通信节点（Comm Node）—— 用户机执行器自愈闭环（需求1）+ 通信/执行解耦（架构 v0.4）。

三层自愈:
  [OS 计划任务] 分钟级 → 拉起本进程（scripts/watchdog.py 判活兜底）
  [本进程]      秒级   → 监督/拉起执行器子进程（崩溃即重启）
  [执行器]      承载   → 智能体任务逻辑（executor/<type>_executor.py）

角色（--role）:
  worker   受控电脑：执行器宿主 + （M3 起）shell/fs 底层操作
  hub      主控电脑：逻辑总机（M2 实现）；本文件先提供自愈/监督/熔断/状态公共骨架

状态灯（真实 bus 状态，非进程存活，架构 §4.1）:
  绿 = 子进程在 + 状态文件新鲜(<60s) + status=connected
  黄 = 子进程在 + 状态文件过期 或 status=reconnecting
  灰 = 已停止（受控开关关闭 / 子进程不在）

受控开关 = 熔断（架构 §4.2）: 关闭即 kill 执行器进程树 + 写 stopped + 灯灰。

用法:
  python executor/comm_node.py --role worker --agent-id node-pc1 --name "PC1" --executor codebuddy
  python executor/comm_node.py --headless --child-cmd "python -c \"import time;time.sleep(3)\"" --test-seconds 10
    # headless 测试：无托盘 UI，打印状态转换，N 秒自动退出

GUI 依赖（pystray/pillow）懒加载：--headless 免装。
"""
import argparse
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

log = logging.getLogger("comm_node")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATUS_FRESH_SECONDS = 60.0    # 状态文件新鲜阈值（心跳 30s，两倍余量）
TRAY_HB_INTERVAL = 30.0        # 托盘心跳文件刷新周期
WATCHDOG_STALE_SECONDS = 150.0 # watchdog 判死阈值
SUPERVISE_INTERVAL = 2.0       # 监督循环周期（秒级拉起）

EXECUTOR_TYPES = ("codebuddy", "opencode", "workbuddy", "interactive")

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
        self.shell_control = bool(ccfg.get("shell_control", False))  # M3 使用，M1 仅落盘
        self._save_control_cfg()

        # bus.env → 子进程环境注入
        self.bus_env = load_env_file(self.install_dir / "bus.env") or load_env_file(
            Path.home() / ".config" / "agent-bus" / "bus.env")
        self.broker_host = self.bus_env.get("BUS_BROKER_HOST", "127.0.0.1")
        self.broker_port = int(self.bus_env.get("BUS_BROKER_PORT", "1883"))
        self.http_base = self.bus_env.get("BUS_HTTP_BASE", f"http://{self.broker_host}:8000")

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

        self._write_pid()

        if not self.headless:
            self._init_gui()

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
        return [sys.executable, str(script), "--agent-id", self.agent_id,
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
                if self.controlled:
                    if self.child is None or self.child.poll() is not None:
                        log.warning("执行器子进程不在/已退出，秒级拉起")
                        self.spawn_child()
                    self.set_lamp(self.compute_lamp())
                else:
                    # 熔断：确保执行器停
                    self.ensure_child_stopped()
                    self.set_lamp("gray")
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

    # ---------------- 消息分派（M2 起使用，M1 骨架） ----------------

    def handle_message(self, msg: dict):
        """控制/对话消息分派骨架。M1 仅记录；shell_exec/executor_*/upgrade 后续里程碑实现。"""
        op = (msg.get("payload") or {}).get("op", "run")
        log.info("收到消息 type=%s op=%s from=%s", msg.get("type"), op, msg.get("sender_id"))
        # M3+: 验签（control_sig）→ op 分派；M5: upgrade；M6: elevated
        # M1 无控制类处理，执行器消息由执行器子进程直接收（本节点不做代理转发）

    # ---------------- 主流程 ----------------

    def run(self):
        # 监督 + 心跳线程（GUI/headless 共用）
        threads = [
            threading.Thread(target=self.supervision_loop, daemon=True),
            threading.Thread(target=self.tray_heartbeat_loop, daemon=True),
        ]
        for t in threads:
            t.start()
        if self.controlled:
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
        log.info("通信节点已退出")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Agent Bus 通信节点（三层自愈 + 监督 + 熔断）")
    ap.add_argument("--role", choices=("worker", "hub"), default="worker",
                    help="worker=受控（默认）/ hub=主控总机（M2）")
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--executor", choices=EXECUTOR_TYPES, default="codebuddy",
                    help="监督并拉起的执行器类型")
    ap.add_argument("--install-dir", default=str(ROOT_DIR))
    ap.add_argument("--headless", action="store_true", help="无托盘 UI（测试/服务模式）")
    ap.add_argument("--child-cmd", default="", help="测试用：自定义子进程命令（覆盖执行器）")
    ap.add_argument("--controlled", choices=("on", "off"), default="",
                    help="显式覆盖受控开关（默认读 controlled.json；off=熔断）")
    ap.add_argument("--test-seconds", type=int, default=0,
                    help="headless 测试时限（秒），到点自动退出")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    node = CommNode(args)
    try:
        node.run()
    finally:
        node.shutdown()


if __name__ == "__main__":
    main()
