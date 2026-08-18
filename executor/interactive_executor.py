"""交互式执行器：用终端复用器（tmux/psmux）在交互 TUI 模式下托管 CLI 智能体。

与一次性执行器（codebuddy_executor 等）的分工:
- 一次性执行器: 生产主力，-p/run 非交互，exit code 即完成信号（确定性）
- 交互式执行器: 可观测与人机协同——TUI 全程存活可直播、可中途注入输入
  （session_input）、可处理纯交互流程（trust 对话框/登录/确认）

链路:
  task_request → 确保 TUI 会话就绪 → 注入指令（paste-buffer 优先）
    → 监听 control mode %output 流（pyte 重建逻辑屏幕）
    → 完成检测（信号优先级: pane 死亡 > 新回复块+屏幕静止 > 长时静止兜底 > 硬超时）
    → 流式 task_progress 直播 → 提取答案 → reply_task

用法:
  python executor/interactive_executor.py --agent-id codebuddy_tui1 --cli codebuddy
  python executor/interactive_executor.py --agent-id mock_tui1 --cli codebuddy --mock
"""
import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_bus import AgentBus, BusConfig, make_task_progress  # noqa: E402
from agent_bus.files import download_file  # noqa: E402

from mux_transport import MuxTransport, ControlClient  # noqa: E402
from agent_profiles import get_profile, extract_answer  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("interactive_executor")

SHELL_READY_WAIT = 3.0        # 会话创建后等 shell 初始化
TUI_BOOT_TIMEOUT = 240.0      # TUI 启动（含 trust 对话框）总超时
SAMPLE_INTERVAL = 0.5         # 完成检测采样间隔
LONG_STABLE_MULTIPLIER = 3.0  # 无新回复块时的静止判定放大系数
GRACEFUL_WAIT = 5.0           # 超时 C-c 后的宽限


def _sha16(text: str) -> str:
    """屏幕内容短哈希（变更检测用，非加密）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_prompt(req: dict, cli_path: Path, attachment_names=None) -> str:
    """单行任务信封（多行文本在 TUI 注入路径有提前提交风险，v1 保持单行）。"""
    payload = req.get("payload", {})
    instruction = (payload.get("instruction") or "").replace("\r", " ").replace("\n", " ")
    prompt = (
        f"【任务信封】发起方={req.get('sender_id')}; "
        f"任务编号={str(req.get('correlation_id', ''))[:8]}。"
        f"【任务指令】{instruction}"
    )
    if attachment_names:
        prompt += f"【附件】已下载到工作目录: {', '.join(attachment_names)}。"
    prompt += (
        f"【协作】如需与其他智能体协作，可用 Bash 调: "
        f'python "{cli_path}" send --to <agent_id> --text "任务指令" --wait 300。'
        f"【完成方式】直接执行，把最终结论写在回复里，不要包含无关内容。"
    )
    return prompt


class InteractiveExecutor:
    def __init__(self, args):
        self.args = args
        self.mock = args.mock
        self.profile = get_profile(args.cli)
        self.workdir = Path(args.workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.cli_path = (ROOT_DIR / "skill" / "cli.py").resolve()
        self.session_name = f"agentbus_{args.agent_id}"
        self.mux = None if self.mock else MuxTransport()
        self.ctrl: ControlClient = None
        self.session_ready = False
        cfg = BusConfig.load(
            broker_host=args.broker_host, broker_port=args.broker_port,
            http_base=args.http_base, agent_id=args.agent_id,
        )
        self.bus = AgentBus(
            args.agent_id, name=args.name,
            capabilities=["interactive", "tui"] if not self.mock else ["mock"],
            executor=f"interactive_{args.cli}" if not self.mock else "mock",
            config=cfg,
        )
        self._seq = 0
        self._deferred = []  # 任务期内到达的新任务缓存（单飞执行器）

    # ---------- 总线侧 ----------

    def _publish(self, topic: str, msg: dict):
        self.bus._client.publish(topic, json.dumps(msg), qos=1)

    def _progress(self, req: dict, phase: str, text: str):
        self._seq += 1
        msg = make_task_progress(
            self.bus.agent_id, req, seq=self._seq, phase=phase,
            output_text=text, session_id=self.session_name,
        )
        self._publish(req.get("reply_to") or f"agent/{req.get('sender_id')}/inbox", msg)

    # ---------- TUI 会话管理 ----------

    def _ensure_session(self):
        """确保 TUI 会话存活且就绪（跨任务复用，挂了自动重拉）。"""
        if self.session_ready and self.ctrl and self.ctrl.alive \
                and self.mux.has_session(self.session_name):
            return
        self._teardown()
        log.info("拉起交互会话 %s (cmd=%s)", self.session_name, self.profile.launch_command)
        if not self.mux.start_session(self.session_name):
            raise RuntimeError(f"无法创建 mux 会话 {self.session_name}")
        if not self.args.no_visible and self.mux.attach_visible(self.session_name):
            log.info("已打开可视附着窗口: %s", self.session_name)
        self.ctrl = ControlClient(self.mux, self.session_name,
                                  cols=self.mux.cols, rows=self.mux.rows)
        self.ctrl.bind_pane()
        time.sleep(SHELL_READY_WAIT)
        # 在 pane shell 中启动 TUI（send-keys -l 防键名解释）
        self.ctrl.text(self.profile.launch_command)
        self.ctrl.keys("Enter")
        self._wait_tui_ready()
        self.session_ready = True
        log.info("TUI 就绪: %s", self.profile.key)

    def _wait_tui_ready(self):
        """启动状态机: 处理 trust/登录等对话框，等主界面就绪。"""
        deadline = time.time() + TUI_BOOT_TIMEOUT
        last_screen = ""
        stable_since = time.time()
        while time.time() < deadline:
            screen = self.ctrl.tracker.text()
            if screen != last_screen:
                last_screen = screen
                stable_since = time.time()
            dialog_key = self.profile.match_dialog(screen)
            if dialog_key:
                log.info("启动对话框出现，注入键: %s", dialog_key)
                self.ctrl.keys(dialog_key)
                time.sleep(2.0)
                continue
            if self.profile.is_ready(screen):
                return
            # 兜底: 长时间完全静止且无对话框 → 视为就绪（profile 可能不准）
            if time.time() - stable_since > 30 and screen.strip():
                log.warning("profile ready 未命中但屏幕已静止 30s，按就绪处理")
                return
            time.sleep(1.0)
        raise TimeoutError(f"TUI 启动超时（>{TUI_BOOT_TIMEOUT}s）")

    def _teardown(self):
        """拆会话 + 杀进程树（kill-session 不连带子进程）。"""
        self.session_ready = False
        if self.ctrl:
            self.ctrl.close()
            self.ctrl = None
        if self.mux and self.mux.has_session(self.session_name):
            pid = self.mux.pane_pid(self.session_name)
            self.mux.kill_session(self.session_name)
            if pid:
                self.mux.kill_process_tree(pid)

    # ---------- 任务执行 ----------

    def handle_request(self, req: dict):
        task_id = req.get("task_id", "")
        timeout = min(int(req.get("timeout_seconds", 600)), int(self.args.timeout))
        payload = req.get("payload", {})

        task_dir = self.workdir / f"task_{task_id[:8]}"
        task_dir.mkdir(parents=True, exist_ok=True)
        local_files = []
        for url in payload.get("attachment_urls") or []:
            try:
                name = Path(url.split("?")[0]).name
                dest = task_dir / f"att_{len(local_files)}_{name}"
                download_file(url, str(dest), self.bus.cfg.http_base)
                local_files.append(dest)
            except Exception as e:
                log.warning("附件下载失败 %s: %s", url, e)

        prompt = build_prompt(req, self.cli_path,
                              [str(p) for p in local_files] or None)

        if self.mock:
            self._run_mock(req, prompt)
            return

        self._seq = 0  # seq 按 task 归零（评审: 消费者按 correlation 期待 1..n）
        try:
            self._ensure_session()
        except Exception as e:
            self._progress(req, "failed", f"TUI 会话拉起失败: {e}")
            self.bus.reply_task(req, output_text="", status="error",
                                error=f"ensure session failure: {e}",
                                session_id=self.session_name)
            return
        started = time.time()  # TUI 启动不吃任务时限预算（评审坑）
        self._progress(req, "started", f"TUI 会话就绪: {self.session_name}")

        try:
            output, status, error = self._run_interactive(req, prompt, timeout, started)
        except Exception as e:
            log.exception("交互执行异常")
            output, status, error = "", "error", f"interactive executor failure: {e}"
            self._teardown()
        if status != "success":
            self._progress(req, status if status in ("failed", "timeout", "cancelled") else "failed", "")

        self.bus.reply_task(req, output_text=output, status=status, error=error,
                            artifacts=[], session_id=self.session_name)

    def _answer_region(self, screen: str) -> str:
        """输入框之上的回复区文本（hash/增量判定用，隔离输入框与状态栏抖动）。"""
        start, _ = self.profile.input_box_region(screen)
        return "\n".join(screen.splitlines()[:start])

    def _extract_answer(self, mark: int, screen: str, max_lines: int = 400) -> str:
        """提取答案: 优先锚点后的完整滚动文本（长回复防截断），失败退当前屏幕。

        防抓多: 锚点 mark 在注入指令前打下，text_since(mark) 天然不含
        前序任务的旧回复；extract_answer 内部取"最后一个回复块"进一步收敛。
        防抓少: 滚出固定屏幕的历史行由 ScreenTracker 滚动累积器回捞。
        max_lines 兜底防超大回复撑爆总线 payload。
        """
        try:
            full = self.ctrl.tracker.text_since(mark, tail=max_lines)
        except Exception:
            full = ""
        ans = extract_answer(full, self.profile) if full else ""
        if not ans:
            ans = extract_answer(screen, self.profile)
        return ans

    def _run_interactive(self, req, prompt, timeout, started):
        profile = self.profile
        screen = self.ctrl.tracker.text()
        bullets_before = len(profile.bullet_lines(screen))
        baseline_hash = _sha16(self._answer_region(screen))
        mark = self.ctrl.tracker.mark()  # 滚动累积水位锚点（注入前，防抓旧/防截断）

        # 注入指令（paste-buffer 优先，多行安全），自适应提交
        if not self.mux.inject_text(self.ctrl.target, prompt):
            self._teardown()  # 清残留，防污染下一复用任务（评审坑）
            return "", "error", "指令注入失败（paste-buffer 与 send-keys 均失败）"
        if not self._submit_with_verify(prompt):
            self._teardown()
            return "", "error", "指令提交失败（输入框文本未被 TUI 接收提交，可能被对话框占用）"
        log.info("指令已提交 (%d 字符)，开始监测完成", len(prompt))

        min_wait = profile.min_answer_wait
        stable_target = profile.stable_seconds
        region_prev = _sha16(self._answer_region(self.ctrl.tracker.text()))
        stable_since = time.time()
        activity_after_inject = False
        answer_changed = False
        bullets_seen = bullets_before
        last_progress_at = 0.0
        sample_n = 0
        deadline = started + timeout
        task_id = req.get("task_id")

        while True:
            time.sleep(SAMPLE_INTERVAL)
            sample_n += 1
            now = time.time()
            tracker = self.ctrl.tracker

            # ---- 任务期内收件: session_input 直达 + task_cancel（评审 P0）----
            for msg in self.bus.poll_inbox(0):
                t = msg.get("type")
                if t == "session_input":
                    self.handle_session_input(msg)
                elif t == "task_cancel" and msg.get("task_id") == task_id:
                    log.info("收到取消请求，优雅中断")
                    self.ctrl.keys("C-c")
                    time.sleep(GRACEFUL_WAIT)
                    self._teardown()
                    return "", "cancelled", f"任务已被发起方取消: {msg.get('reason') or ''}"
                elif t == "task_request":
                    # 单飞执行器: 任务期内新任务排队不丢（留在队列外自行缓存）
                    self._deferred.append(msg)

            screen = tracker.text()
            region_hash = _sha16(self._answer_region(screen))
            if region_hash != region_prev:
                region_prev = region_hash
                stable_since = now
                activity_after_inject = True
                if region_hash != baseline_hash:
                    answer_changed = True  # 回复区相对任务起点有实质增量
            bullets_seen = max(bullets_seen, len(profile.bullet_lines(screen)))
            stable_for = now - stable_since

            # 直播节流
            if now - last_progress_at >= self.args.progress_interval:
                last_progress_at = now
                phase = "running"
                # y/n 确认类子 prompt 检测（评审建议: 主/子 prompt 区分）
                box_start, _ = profile.input_box_region(screen)
                if any(("[y/n]" in ln.lower()) or ("(y/n)" in ln.lower())
                       or ln.rstrip().endswith("?")
                       for ln in screen.splitlines()[box_start:]):
                    phase = "input_needed"
                self._progress(req, phase, tracker.tail(25))

            # 信号 1: pane/会话死亡（降频探测，评审: 0.5s 一次子进程太重）
            if (not self.ctrl.alive) or (sample_n % 8 == 0
                                         and not self.mux.has_session(self.session_name)):
                ans = self._extract_answer(mark, screen)
                return (ans or tracker.tail(20)), "error", "TUI 会话已退出（崩溃或被关闭）"

            # 信号 2: 新回复块出现 + 回复区静止（主判据）
            if (now - started) >= min_wait and activity_after_inject \
                    and bullets_seen > bullets_before and stable_for >= stable_target:
                self._progress(req, "done", "")
                return self._extract_answer(mark, screen), "success", None

            # 信号 3: 回复区有实质增量 + 长时静止（兜底；须有"确已执行"证据，评审 P1）
            if (now - started) >= min_wait + stable_target * LONG_STABLE_MULTIPLIER \
                    and stable_for >= stable_target * LONG_STABLE_MULTIPLIER \
                    and answer_changed:
                log.warning("无新回复块但回复区长时静止，走兜底提取")
                self._progress(req, "done", "")
                return self._extract_answer(mark, screen), "success", None

            # 信号 4: 硬超时 → 优雅中断（C-c → 宽限 → 强杀）
            if now >= deadline:
                log.warning("硬超时，尝试优雅中断")
                self.ctrl.keys("C-c")
                time.sleep(GRACEFUL_WAIT)
                if self.mux.has_session(self.session_name):
                    screen2 = self.ctrl.tracker.text()
                    ans = self._extract_answer(mark, screen2)
                    self._teardown()
                    return (ans or ""), "timeout", f"执行超时（>{timeout}s），会话已重置"
                return "", "timeout", f"执行超时（>{timeout}s）"

    # ---------- 提交与输入框状态 ----------

    def _prompt_still_in_box(self, screen: str, prompt: str) -> bool:
        """判断指令文本是否还躺在输入框里（未提交）。

        实测坑1: Ink 类 TUI 粘贴后首枚 Enter 可能被启动期对话框（update 提示/
        trust 二次确认）吞掉，指令滞留输入框。
        实测坑2: transcript 会用 "> " 前缀回显已提交消息，不能全屏搜索，
        必须锚定输入框区域（两条分隔线之间）再匹配。
        """
        lines = screen.splitlines()
        start, end = self.profile.input_box_region(screen)
        head = prompt[:10]
        return any(head in ln for ln in lines[start:end])

    def _submit_with_verify(self, prompt: str, max_retry: int = 3) -> bool:
        """发 Enter 并验证输入框已清空；未清空则重发（自适应提交）。"""
        for attempt in range(max_retry):
            self.ctrl.keys("Enter")
            time.sleep(2.0)
            screen = self.ctrl.tracker.text()
            if not self._prompt_still_in_box(screen, prompt):
                return True
            log.warning("输入框仍含未提交文本，重发 Enter (%d/%d)", attempt + 1, max_retry)
        return not self._prompt_still_in_box(self.ctrl.tracker.text(), prompt)

    def _run_mock(self, req, prompt):
        """联调模式: 不拉 TUI，验证 MQTT 链路/进度流/结果闭环。"""
        self._progress(req, "started", "[mock] 交互会话已就绪")
        for i in range(2):
            time.sleep(1.0)
            self._progress(req, "running", f"[mock] 执行中 {i + 1}/2: {prompt[:80]}")
        time.sleep(0.5)
        self._progress(req, "done", "")
        self.bus.reply_task(
            req, output_text=f"[mock] 交互执行完成: {prompt[:120]}",
            status="success", artifacts=[], session_id=self.session_name,
        )

    # ---------- 中途输入（人工干预/追问） ----------

    def handle_session_input(self, msg: dict):
        if msg.get("session_id") != self.session_name:
            log.warning("session_input 目标会话不匹配: %s", msg.get("session_id"))
            return
        if self.mock or not (self.ctrl and self.ctrl.alive):
            log.warning("无活跃会话，忽略 session_input")
            return
        text = msg.get("text") or ""
        special = msg.get("special")
        if text:
            self.mux.inject_text(self.ctrl.target, text)
        if special:
            self.ctrl.keys(special)
        log.info("已注入中途输入: text=%d字符 special=%s", len(text), special)

    # ---------- 主循环 ----------

    def run(self):
        self.bus.connect(register=True)
        log.info("交互执行器就绪 cli=%s mock=%s session=%s workdir=%s",
                 self.args.cli, self.mock, self.session_name, self.workdir)
        try:
            while True:
                incoming = self.bus.poll_inbox(timeout=2.0)
                if self._deferred:
                    incoming = self._deferred + incoming
                    self._deferred = []
                for msg in incoming:
                    t = msg.get("type")
                    if t == "task_request":
                        try:
                            self.handle_request(msg)
                        except Exception:
                            log.exception("任务处理异常")
                            self.bus.reply_task(msg, output_text="", status="error",
                                                error="executor internal error")
                    elif t == "session_input":
                        self.handle_session_input(msg)
        except KeyboardInterrupt:
            pass
        finally:
            if not self.mock:
                self._teardown()
            self.bus.disconnect()


def main():
    parser = argparse.ArgumentParser(description="交互式 TUI 执行器")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--cli", default="codebuddy", choices=["codebuddy", "opencode"])
    parser.add_argument("--mock", action="store_true", help="模拟执行，不拉 TUI（联调用）")
    parser.add_argument("--no-visible", action="store_true",
                        help="不打开可视附着窗口（默认打开；无人值守/headless 节点使用）")
    parser.add_argument("--workdir", default=str(ROOT_DIR / "data" / "interactive_work"))
    parser.add_argument("--timeout", type=int, default=1800, help="单任务硬超时（秒）")
    parser.add_argument("--progress-interval", type=float, default=3.0, help="进度直播间隔（秒）")
    parser.add_argument("--broker-host", default=None)
    parser.add_argument("--broker-port", type=int, default=None)
    parser.add_argument("--http-base", default=None)
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    InteractiveExecutor(args).run()


if __name__ == "__main__":
    main()
