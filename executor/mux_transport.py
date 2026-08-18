"""MuxTransport: 终端复用器抽象层。

Linux 用 tmux，Windows 用 psmux（Rust 实现的 ConPTY 复用器，安装 tmux.exe 别名，
命令语义与 tmux 兼容）。上层代码只面对同一套 tmux 命令协议。

已实测固化的坑（验收 + 双 AI 评审结论）:
- 坑B: psmux 的 warm-session 机制写 ~/.psmux/ 发现文件，受限沙箱会拦截导致 server 起不来
       → 必须在 server 进程启动前注入 PSMUX_NO_WARM=1（本模块对所有子进程统一注入）
- 坑C: 杀光会话后 psmux 的 list-sessions 返回 exit=0 且无输出（真 tmux 报错 exit=1）
       → 健康检查不能只看退出码，要看"必有输出的命令"
- pipe-pane: psmux 未实现（pane_pipe 恒 0，日志 0 字节）
       → transcript 一律走 control mode %output 事件流，见 ControlClient
- kill-session 不杀子进程树: Windows 用 taskkill /T /F 兜底
"""
import glob
import os
import platform
import shutil
import subprocess
import threading
import time

from vt_screen import ScreenTracker, decode_tmux_escape

IS_WIN = platform.system() == "Windows"

# Windows: 子进程不闪控制台窗口
_CREATIONFLAGS = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
# Windows: 可视附着窗口需要独立的新控制台（人类观察 TUI 对话流用）
_VISIBLE_CONSOLE = 0x00000010 if IS_WIN else 0  # CREATE_NEW_CONSOLE


def find_mux_binary() -> str:
    """定位 tmux/psmux 二进制：PATH 优先，Windows 再兜底 winget 包目录。"""
    for name in ("tmux", "tmux.exe", "psmux", "psmux.exe"):
        path = shutil.which(name)
        if path:
            return path
    if IS_WIN:
        base = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
        for pattern in ("*psmux*", "*tmux*"):
            for d in glob.glob(os.path.join(base, pattern)):
                for exe in ("tmux.exe", "psmux.exe"):
                    p = os.path.join(d, exe)
                    if os.path.isfile(p):
                        return p
    raise FileNotFoundError(
        "找不到 tmux/psmux。Linux: apt install tmux；Windows: winget install psmux")


def mux_quote(text: str) -> str:
    """tmux 命令行风格的单引号转义（控制模式 stdin 下发命令用）。"""
    return "'" + text.replace("'", "'\\''") + "'"


class MuxTransport:
    """同步命令面：会话生命周期 + 输入注入 + 屏幕抓取。"""

    def __init__(self, binary: str = None, cols: int = 160, rows: int = 50):
        self.binary = binary or find_mux_binary()
        self.cols = cols
        self.rows = rows
        self._env = dict(os.environ)
        if IS_WIN:
            # 坑B: 须在 server 启动前生效（环境变量随首个子进程继承给 server）
            self._env["PSMUX_NO_WARM"] = "1"
        self._env.setdefault("TERM", "xterm-256color")

    # ---------- 基础命令执行 ----------

    def _run(self, *args, timeout: float = 20.0) -> subprocess.CompletedProcess:
        """跑一条 mux 命令。超时/异常一律返回失败态 CompletedProcess，不抛出（评审 P1）。"""
        try:
            return subprocess.run(
                [self.binary, *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=self._env,
                timeout=timeout, creationflags=_CREATIONFLAGS,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, returncode=-1,
                                               stderr=f"mux command timeout >{timeout}s")
        except OSError as e:
            return subprocess.CompletedProcess(args, returncode=-1, stderr=str(e))

    def server_alive(self) -> bool:
        """健康检查（坑C: 不看退出码，看必有输出的命令）。"""
        r = self._run("list-sessions", timeout=10)
        return bool((r.stdout or "").strip())

    # ---------- 会话生命周期 ----------

    def has_session(self, name: str) -> bool:
        return self._run("has-session", "-t", name, timeout=10).returncode == 0

    def start_session(self, name: str, cols: int = None, rows: int = None) -> bool:
        """创建 detached 会话，跑默认 shell；几何 pin 死（评审共识: 防 viewer resize 错位）。"""
        if self.has_session(name):
            return True
        r = self._run(
            "new-session", "-d", "-s", name,
            "-x", str(cols or self.cols), "-y", str(rows or self.rows),
        )
        return r.returncode == 0

    def kill_session(self, name: str) -> bool:
        return self._run("kill-session", "-t", name, timeout=15).returncode == 0

    def attach_visible(self, name: str) -> bool:
        """为会话打开人类可见的交互附着窗口（直接观察 TUI 对话流）。

        - 仅 Windows（Linux 无人值守节点直接返回 False，调用方不视为错误）
        - cmd /c 载体: kill-session 后附着客户端退出，窗口自动关闭，无需 teardown 配合
        - mode con 预置与创建时一致的几何（评审共识: 几何 pin 死防 viewer resize 错位）
        - 窗口标题 = 会话名（sanitize 掉 cmd 元字符）
        """
        if not IS_WIN:
            return False
        safe_title = "".join(c for c in name if c not in '&|<>^"')
        cmd = (f"title {safe_title} & "
               f"mode con cols={self.cols} lines={self.rows} & "
               f"{self.binary} attach-session -t {name}")
        try:
            if " " in self.binary:
                # 路径含空格: /S /c 让 cmd 把整条带引号命令串原样执行
                subprocess.Popen(["cmd.exe", "/S", "/c", f'"{cmd}"'],
                                 env=self._env, creationflags=_VISIBLE_CONSOLE)
            else:
                subprocess.Popen(["cmd.exe", "/c", cmd],
                                 env=self._env, creationflags=_VISIBLE_CONSOLE)
            return True
        except OSError:
            return False

    def pane_pid(self, target: str):
        """取 pane 主进程 PID（进程树看护用）。"""
        r = self._run("list-panes", "-t", target, "-F", "#{pane_pid}")
        if r.returncode == 0 and r.stdout.strip():
            try:
                return int(r.stdout.strip().splitlines()[0])
            except ValueError:
                return None
        return None

    def kill_process_tree(self, pid: int) -> bool:
        """强杀进程树（kill-session 不连带子进程，评审坑）。"""
        if not pid:
            return False
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20,
                               creationflags=_CREATIONFLAGS)
            else:
                # POSIX: 优先杀进程组（TUI 的 shell 子进程会残留，评审坑）
                import signal
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            return True
        except Exception:
            return False

    # ---------- 输入注入 ----------

    def send_keys(self, target: str, keys: str):
        """发键名（Enter / C-c / Esc 等 tmux 键名）。"""
        self._run("send-keys", "-t", target, keys)

    def paste_text(self, target: str, text: str) -> bool:
        """长文本注入首选：set-buffer + paste-buffer。

        走 tmux 缓冲区而非逐键注入：多行安全（bracketed paste 语义由
        tmux 按 pane 应用是否启用自动包裹），不受 send-keys 键名解释干扰。
        用唯一缓冲区名，避免多执行器并发写默认缓冲区互相 clobber（评审坑）。
        """
        buf = "agentbus_paste"
        if self._run("set-buffer", "-b", buf, "--", text).returncode != 0:
            return False
        ok = self._run("paste-buffer", "-b", buf, "-t", target).returncode == 0
        self._run("delete-buffer", "-b", buf)
        return ok

    def send_literal(self, target: str, text: str, chunk: int = 8000) -> bool:
        """兜底：分块 send-keys -l（按块避免超 PTY 缓冲截断；去换行防提前提交）。"""
        flat = text.replace("\r", " ").replace("\n", " ")
        for i in range(0, len(flat), chunk):
            if self._run("send-keys", "-t", target, "-l", flat[i:i + chunk]).returncode != 0:
                return False
            time.sleep(0.05)
        return True

    def inject_text(self, target: str, text: str) -> bool:
        """统一入口: 先 paste-buffer，失败退 send-keys -l 分块。"""
        if self.paste_text(target, text):
            return True
        return self.send_literal(target, text)

    # ---------- 屏幕抓取（对账/兜底） ----------

    def capture_pane(self, target: str, history: int = 0) -> str:
        args = ["capture-pane", "-t", target, "-p"]
        if history:
            args += ["-S", f"-{history}"]
        r = self._run(*args)
        return r.stdout or ""


class ControlClient:
    """tmux -C attach 控制模式客户端: %output 增量事件流 → ScreenTracker。

    替代 pipe-pane（psmux 未实现）做 transcript/直播源：
    - 单通道: 命令应答与通知混在同一 stdout 流，按事件前缀分流（本客户端只发
      send-keys 类即发即弃命令，应答可安全忽略；查询类命令需 %begin/%end 配对，
      后续按需扩展）
    - %output 数据是原始 VT 流，含 octal 转义，须按字节流连续喂（vt_screen 处理）
    """

    def __init__(self, transport: MuxTransport, session: str,
                 cols: int = 160, rows: int = 50):
        self.transport = transport
        self.session = session
        self.target = session  # 单窗格会话，target 即 session 名
        self.tracker = ScreenTracker(cols, rows)
        self.alive = True
        self._pane_id = None
        self._proc = subprocess.Popen(
            [transport.binary, "-C", "attach-session", "-t", session],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=transport._env,
            creationflags=_CREATIONFLAGS,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        # 评审坑: kill/pipe 断开会在迭代中抛异常，裸线程带 traceback 静默死
        try:
            for raw in self._proc.stdout:
                if not self.alive:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("%"):
                    continue
                parts = line.split(" ", 2)
                kind = parts[0]
                if kind == "%output" and len(parts) >= 2:
                    pane = parts[1]
                    if self._pane_id and pane != self._pane_id:
                        continue  # 只跟踪本 pane（单窗格场景冗余防御）
                    data = parts[2] if len(parts) > 2 else ""
                    self.tracker.feed(decode_tmux_escape(data))
                elif kind == "%exit":
                    self.alive = False
                    break
                # 其余通知（%session-changed/%window-add 等）MVP 忽略
        except (OSError, ValueError):
            pass  # stdout 管道关闭/解码边界，正常退出路径
        finally:
            self.alive = False

    def bind_pane(self):
        """查询并绑定当前 pane id（过滤他人 pane 输出）。"""
        r = self.transport._run("list-panes", "-t", self.session, "-F", "#{pane_id}")
        if r.returncode == 0 and r.stdout.strip():
            self._pane_id = r.stdout.strip().splitlines()[0]

    def _cmd(self, line: str):
        """通过控制模式 stdin 下发命令。"""
        try:
            self._proc.stdin.write((line + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.alive = False

    def keys(self, key: str):
        """发键名: Enter / C-c / Esc / Up ...

        注意: 走 subprocess argv 而非控制模式 stdin——实测 psmux 对
        stdin 下发的带引号转义 send-keys -l 解析不可靠，argv 路径稳定。
        """
        self.transport.send_keys(self.target, key)

    def text(self, text: str):
        """发字面文本: paste-buffer 优先，send-keys -l 分块兜底。"""
        self.transport.inject_text(self.target, text)

    def close(self):
        self.alive = False
        try:
            self._cmd("detach-client")
            time.sleep(0.3)
        except Exception:
            pass
        try:
            self._proc.kill()
            self._proc.wait(timeout=3)  # 回收，不留 zombie/句柄（评审坑）
        except Exception:
            pass
        try:
            self._reader.join(timeout=2)
        except Exception:
            pass
