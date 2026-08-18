"""TraeWork CN（原 Trae Solo CN）节点执行器：通过 CDP 把总线任务注入 TraeWork 桌面端并回收结果。

原理（CDP 桥接，适用于无可用 CLI 的桌面智能体）:
  收到 task_request
    → 构造协作提示词（含任务信封: 发起方/执行方/correlation_id 短码 + 结果文件契约）
    → CDP: 聚焦 composer(contenteditable) → Input.insertText 注入全文
      → 点击 .chat-input-v2-send-button（失败则回退 ENTER）
    → 提交校验: 轮询 DOM 直到出现含任务短码的 .user-message
    → 回收双保险:
       a) 指令契约: Trae 把最终结论写入约定结果文件（轮询文件出现+稳定）
       b) DOM 观察: .turn__agent-message 回复文本连续两轮稳定且无运行指示
    → reply_task 回传给发起方

前提（一次性配置，均已完成则跳过）:
  - TraeWork CN 已安装且已登录，窗口标题 "TraeWork CN"
  - ~/.trae-cn/argv.json 中加入（字符串值，数字会被忽略）:
        "remote-debugging-port": "9433"
    然后重启 TraeWork。CDP 仅监听 127.0.0.1。
  - pip 依赖 websockets（本机已随 uvicorn[standard] 具备）

限制:
  - 任务进入 TraeWork 窗口当前打开的会话（v1 不做会话切换）
  - Trae 会话正文在云端，本执行器不读其内部存储，只经 DOM/结果文件交互

用法:
  python executor/traework_executor.py --agent-id traework_pc1 --name "TraeWork@PC1"
  python executor/traework_executor.py --mock     # 不接 CDP，模拟执行（联调用）
  python executor/traework_executor.py --live     # 前台实时打印注入/轮询过程
"""
import argparse
import asyncio
import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import AgentBus, BusConfig  # noqa: E402
from agent_bus.files import download_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("traework_executor")

DEFAULT_CDP_PORT = 9433
POLL_INTERVAL = 3.0          # 回复轮询间隔（秒）
WINDOW_TITLE = "TraeWork CN"

# DOM 锚点（Trae SOLO-lite 聊天界面）
COMPOSER_SEL = ".chat-input-v2-input-box-editable"
SEND_BTN_SEL = "button.chat-input-v2-send-button"
USER_MSG_SEL = ".user-message"
AGENT_MSG_SEL = ".turn__agent-message"

# 提示词要带信封短码（回复校验锚点）+ 结果文件契约（完成信号）。
PROMPT_TEMPLATE = """【跨智能体任务】发起方:{sender_id} → 执行方:{receiver_id}(你) 编号:{task_tag}
{instruction}{context_block}{attachment_block}
请直接执行。完成后务必做两件事：1) 把最终结论完整写入文件 {result_file}；2) 在回复末尾写上"任务完成 {task_tag}"。"""

# DOM innerText 里的界面噪音行（agent 消息头部/工具摘要）
_CHROME_RE = re.compile(
    r"^(TraeWork|耗时.*|思考过程|正在.*|已执行\s*\d+.*|已创建.*|已修改.*|已发送.*|等待.*)$"
)


def _clean_reply(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip() and not _CHROME_RE.match(ln.strip())]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# CDP 客户端：后台 asyncio 线程 + 同步 facade（bus 主循环是同步的）
# ---------------------------------------------------------------------------

class CdpClient:
    """最小化 CDP 客户端：锁定 workbench page target，支持断线自动重连。"""

    def __init__(self, port: int = DEFAULT_CDP_PORT):
        self.port = port
        self.http_base = f"http://127.0.0.1:{port}"
        self._loop = None
        self._thread = None
        self._ws = None
        self._ws_url = None
        self._next_id = 0
        self._lock = threading.Lock()

    # ---- 生命周期 ----

    def start(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                        name="trae-cdp-loop")
        self._thread.start()

    def close(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._close_ws(), self._loop).result(timeout=5)
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _close_ws(self):
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ---- target 发现 ----

    def find_ws_url(self) -> str:
        """HTTP /json 找 workbench 主窗口 target，返回其 webSocketDebuggerUrl。"""
        with urllib.request.urlopen(self.http_base + "/json", timeout=5) as r:
            targets = json.load(r)
        pages = [t for t in targets if t.get("type") == "page"]
        pick = None
        for t in pages:
            url = t.get("url", "")
            title = t.get("title", "")
            if url.startswith("vscode-file://") or WINDOW_TITLE.lower() in title.lower():
                pick = t
                break
        if pick is None and pages:
            pick = pages[0]
        if pick is None:
            raise RuntimeError("CDP 无可用 page target（TraeWork 是否已启动？）")
        return pick["webSocketDebuggerUrl"]

    def check_alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.http_base + "/json/version", timeout=3) as r:
                json.load(r)
            return True
        except Exception:
            return False

    # ---- 调用 ----

    def call(self, method: str, params: dict = None, timeout: float = 30.0):
        """同步发一条 CDP 命令并等待响应（自动重连一次）。"""
        with self._lock:
            fut = asyncio.run_coroutine_threadsafe(self._call(method, params or {}), self._loop)
            try:
                return fut.result(timeout=timeout)
            except Exception:
                # 断线重连一次再试
                asyncio.run_coroutine_threadsafe(self._close_ws(), self._loop).result(timeout=5)
                fut = asyncio.run_coroutine_threadsafe(self._call(method, params or {}), self._loop)
                return fut.result(timeout=timeout)

    async def _call(self, method: str, params: dict):
        if self._ws is None:
            import websockets
            self._ws_url = self.find_ws_url()
            self._ws = await websockets.connect(self._ws_url, max_size=64 * 1024 * 1024)
        self._next_id += 1
        mid = self._next_id
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            raw = await self._ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 错误: {msg['error']}")
                return msg.get("result", {})

    def evaluate(self, expr: str, timeout: float = 30.0):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True}, timeout=timeout)
        return r.get("result", {}).get("value")


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------

class TraeWorkExecutor:
    def __init__(self, args):
        self.args = args
        self.live = getattr(args, "live", False)
        self.workdir = Path(args.workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        cfg = BusConfig.load(
            broker_host=args.broker_host, broker_port=args.broker_port,
            http_base=args.http_base, agent_id=args.agent_id,
        )
        self.bus = AgentBus(
            args.agent_id, name=args.name,
            capabilities=["desktop", "code", "files"] if not args.mock else ["mock"],
            executor="traework_gui" if not args.mock else "mock",
            config=cfg,
        )
        self.cdp = CdpClient(args.cdp_port)
        self._current_task_tag = ""

    # ---------- CDP 注入三步 ----------

    def _inject_prompt(self, prompt: str):
        """聚焦 composer → Input.insertText → 点发送（回退 ENTER）→ 校验提交。"""
        cdp = self.cdp
        # 1. 聚焦输入框（无需 OS 前台焦点，CDP 直达渲染进程）
        focused = cdp.evaluate(f"""
            (() => {{
                const el = document.querySelector({json.dumps(COMPOSER_SEL)});
                if (!el) return false;
                el.focus();
                return document.activeElement === el;
            }})()
        """)
        if not focused:
            raise RuntimeError(f"未找到/无法聚焦输入框 {COMPOSER_SEL}（TraeWork 窗口是否正常？）")

        # 2. 注入全文（Input.insertText 走真实输入管线，无 argv 长度/换行截断问题）
        cdp.call("Input.insertText", {"text": prompt})
        time.sleep(0.8)

        # 3. 提交：优先点发送按钮，回退 ENTER；带重试
        # 安全铁律：点击后按钮会变成"停止生成"——绝不在已提交/生成中时再次点击。
        for attempt in range(3):
            # 若输入框已空，说明上一次点击其实已提交成功（用户消息 DOM 可能延迟出现）
            if not self._composer_text():
                log.info("输入框已空，判定上次点击已提交，等待消息出现")
                if self._wait_tag_visible(timeout=15):
                    return
                raise RuntimeError("输入框已空但对话区始终未出现任务消息（渲染异常？）")

            btn = cdp.evaluate(f"""
                (() => {{
                    const b = document.querySelector({json.dumps(SEND_BTN_SEL)});
                    if (!b) return null;
                    const r = b.getBoundingClientRect();
                    return {{x: r.left + r.width / 2, y: r.top + r.height / 2,
                             cls: String(b.className), disabled: b.disabled === true,
                             aria: b.getAttribute('aria-label') || ''}};
                }})()
            """)
            clicked = False
            if btn and not btn.get("disabled") and "voice-call-mode" not in btn.get("cls", ""):
                x, y = btn["x"], btn["y"]
                cdp.call("Input.dispatchMouseEvent",
                         {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                cdp.call("Input.dispatchMouseEvent",
                         {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
                clicked = True
            else:
                # 回退：ENTER（多数聊天 composer Enter 即发送）
                for t in ("keyDown", "keyUp"):
                    cdp.call("Input.dispatchKeyEvent",
                             {"type": t, "key": "Enter", "code": "Enter",
                              "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})

            # 提交成功判定：轮询等待含任务短码的用户消息（虚拟列表渲染可能延迟）
            if self._wait_tag_visible(timeout=12):
                log.info("任务已提交（第 %d 次尝试，方式=%s）", attempt + 1,
                         "click" if clicked else "enter")
                return
            log.warning("第 %d 次提交未检出用户消息", attempt + 1)
            # 重试前重新聚焦（文本可能已清空或仍在）
            cdp.evaluate(f"(() => {{ const el = document.querySelector({json.dumps(COMPOSER_SEL)});"
                         f" if (el) el.focus(); return true; }})()")
        raise RuntimeError("注入失败：3 次尝试均未在对话区发现含短码的用户消息")

    def _composer_text(self) -> str:
        return self.cdp.evaluate(f"""
            (() => {{
                const el = document.querySelector({json.dumps(COMPOSER_SEL)});
                return el ? (el.innerText || '').trim() : '';
            }})()
        """) or ""

    def _wait_tag_visible(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._user_message_visible():
                return True
            time.sleep(1.0)
        return False

    def _user_message_visible(self) -> bool:
        tag = self._current_task_tag
        return bool(self.cdp.evaluate(f"""
            Array.from(document.querySelectorAll({json.dumps(USER_MSG_SEL)}))
                 .some(u => (u.innerText || '').includes({json.dumps(tag)}))
        """))

    def _poll_state(self) -> dict:
        """一次轮询：回复文本 + 发送按钮形态 + 运行指示器。"""
        tag = self._current_task_tag
        raw = self.cdp.evaluate(f"""
            (() => {{
                const TAG = {json.dumps(tag)};
                const nodes = Array.from(document.querySelectorAll(
                    {json.dumps(USER_MSG_SEL + "," + AGENT_MSG_SEL)}));
                let anchor = -1;
                for (let i = 0; i < nodes.length; i++) {{
                    if (nodes[i].classList.contains('user-message')
                        && (nodes[i].innerText || '').includes(TAG)) anchor = i;
                }}
                const replies = [];
                if (anchor >= 0) {{
                    for (let i = anchor + 1; i < nodes.length; i++) {{
                        if (nodes[i].classList.contains('turn__agent-message'))
                            replies.push(nodes[i].innerText || '');
                    }}
                }}
                const send = document.querySelector({json.dumps(SEND_BTN_SEL)});
                // 最后一个 agent turn 的状态条（运行/停止指示）
                let bar_text = '', icon_cls = '';
                const turns = Array.from(document.querySelectorAll({json.dumps(AGENT_MSG_SEL)}));
                if (turns.length) {{
                    const bar = turns[turns.length - 1].querySelector('[class*="latest-assistant-bar"]');
                    if (bar) bar_text = (bar.innerText || '').trim();
                    const icon = turns[turns.length - 1].querySelector('[class*="status-icon"]');
                    if (icon) icon_cls = String(icon.className);
                }}
                const indicators = Array.from(document.querySelectorAll(
                    '[class*="stop" i],[class*="generating" i],[class*="thinking" i],[class*="loading" i]'
                )).slice(0, 6).map(e => String(e.className).slice(0, 80));
                return JSON.stringify({{
                    anchor: anchor >= 0,
                    replies: replies,
                    send_cls: send ? String(send.className) : '',
                    bar_text: bar_text,
                    icon_cls: icon_cls,
                    indicators: indicators
                }});
            }})()
        """)
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    # 状态条文案 → 运行中（阻止"稳定即完成"判定）
    _RUNNING_RE = re.compile(r"思考|生成|执行|搜索|读取|写入|分析|恢复|规划|调用|浏览|整理|正在")
    # 状态条文案 → 已停止/失败（且无正文时提前报错）
    _STOPPED_RE = re.compile(r"手动停止|已停止|停止生成|失败|出错|中断|报错")

    def _wait_reply(self, result_file: Path, timeout: int):
        """轮询直到完成。完成信号（任一）:
        a) 结果文件出现且大小连续两轮稳定（指令契约，最可靠）
        b) DOM 回复含"任务完成 <短码>"标记
        c) DOM 回复有实质内容(>20字符)、连续两轮稳定、且状态条无运行指示
        异常: 状态条显示已停止/失败且无正文 → 提前 error
        返回 (reply_text, status, error)
        """
        tag = self._current_task_tag
        deadline = time.time() + timeout
        last_reply, stable = "", 0
        last_fsize, fstable = -1, 0

        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)

            # a) 结果文件契约
            try:
                if result_file.exists():
                    sz = result_file.stat().st_size
                    if sz == last_fsize and sz > 0:
                        fstable += 1
                    else:
                        fstable = 0
                    last_fsize = sz
                    if fstable >= 2:
                        text = result_file.read_text(encoding="utf-8", errors="replace").strip()
                        if text:
                            return text[:4000], "success", None
            except Exception as e:
                log.warning("结果文件读取异常: %s", e)

            # b/c/d) DOM 观察
            try:
                st = self._poll_state()
            except Exception as e:
                log.warning("DOM 轮询暂不可达: %s", e)
                continue
            reply = _clean_reply("\n\n".join(st.get("replies", [])))
            bar_text = st.get("bar_text", "")
            running = bool(self._RUNNING_RE.search(bar_text)) or \
                "stop" in st.get("send_cls", "") or bool(st.get("indicators"))
            stopped = bool(self._STOPPED_RE.search(bar_text)) and \
                "pause" in st.get("icon_cls", "")
            if self.live:
                print(f"  [轮询] 回复 {len(reply)} 字符, 稳定 {stable}/2, "
                      f"状态条={bar_text[:20]!r}, 运行中={'是' if running else '否'}, "
                      f"结果文件={'有' if result_file.exists() else '无'}", flush=True)

            # b) 完成标记
            if f"任务完成 {tag}" in reply or f"任务完成{tag}" in reply:
                return reply[:4000], "success", None

            # 排队状态（模型服务拥堵）：绝不把排队通知当回复，继续等待
            if "排队" in reply or "队列" in reply:
                if self.live and stable == 0:
                    print("  [轮询] 模型服务排队中，继续等待...", flush=True)
                last_reply, stable = "", 0
                continue

            # d) 已停止/失败且无实质内容
            if stopped and len(reply) <= 20:
                return reply[:4000], "error", f"TraeWork 生成被中止（状态条: {bar_text[:40]}）"

            # c) 实质内容稳定
            if reply and len(reply) > 20:
                if reply == last_reply:
                    stable += 1
                else:
                    stable = 0
                last_reply = reply
                if not running and stable >= 2:
                    return reply[:4000], "success", None

        return last_reply[:4000], "timeout", f"等待 TraeWork 回复超时（>{timeout}s），返回已观察到的部分"

    # ---------- 任务处理 ----------

    def handle_request(self, req: dict):
        task_id = req.get("task_id", "")
        payload = req.get("payload", {})
        timeout = min(req.get("timeout_seconds", 1800), self.args.timeout)
        task_tag = task_id[:8]
        self._current_task_tag = task_tag
        result_file = self.workdir / f"task_{task_tag}_result.md"
        if result_file.exists():
            try:
                result_file.unlink()
            except Exception:
                pass

        # 附件落盘（Claim-Check 下载），路径写进提示词让 Trae 自取
        local_files, att_lines = [], []
        for url in payload.get("attachment_urls") or []:
            try:
                name = Path(url.split("?")[0]).name
                dest = self.workdir / f"task_{task_tag}_att_{len(local_files)}_{name}"
                download_file(url, str(dest), self.bus.cfg.http_base)
                local_files.append(dest)
                att_lines.append(str(dest))
            except Exception as e:
                log.warning("附件下载失败 %s: %s", url, e)

        ctx = payload.get("context_data")
        prompt = PROMPT_TEMPLATE.format(
            sender_id=req.get("sender_id", "?"),
            receiver_id=req.get("target_id", "?"),
            task_tag=task_tag,
            instruction=payload.get("instruction", ""),
            context_block=f"\n【上下文】{ctx}" if ctx else "",
            attachment_block=("\n【附件】" + "; ".join(att_lines)) if att_lines else "",
            result_file=str(result_file),
        )

        started = time.time()
        if self.args.mock:
            output, status, error = self._run_mock(req, local_files)
        else:
            if self.live:
                print("\n" + "=" * 60, flush=True)
                print(f"  [LIVE] 收到任务 from {req.get('sender_id', '?')}", flush=True)
                print(f"  任务短码: {task_tag}", flush=True)
                print(f"  指令: {payload.get('instruction', '')[:120]}", flush=True)
                print("=" * 60, flush=True)
            try:
                self._inject_prompt(prompt)
                output, status, error = self._wait_reply(result_file, timeout)
            except Exception as e:
                output, status, error = "", "error", str(e)
            if self.live:
                print(f"\n  [LIVE] 执行完成 status={status} 耗时 {time.time() - started:.0f}s",
                      flush=True)
                if output:
                    print("─" * 60 + f"\n[TraeWork 回复]\n{output}\n" + "─" * 60, flush=True)

        log.info("任务 %s 完成 status=%s 耗时=%.0fs", task_tag, status, time.time() - started)
        self.bus.reply_task(req, output_text=output[:4000], status=status,
                            error=error, artifacts=[], session_id=None)

    def _run_mock(self, req, local_files):
        time.sleep(0.3)
        return (f"[mock] 已收到任务: {req.get('payload', {}).get('instruction', '')[:100]}\n"
                f"附件 {len(local_files)} 个: {[p.name for p in local_files]}", "success", None)

    # ---------- 主循环 ----------

    def run(self):
        self.cdp.start()
        if not self.args.mock:
            if self.cdp.check_alive():
                log.info("CDP 通道正常（127.0.0.1:%d）", self.cdp.port)
            else:
                log.warning("CDP 未就绪（127.0.0.1:%d）！请确认 TraeWork 已启动且 "
                            "argv.json 含 \"remote-debugging-port\": \"%d\"（字符串值）",
                            self.cdp.port, self.cdp.port)
        self.bus.connect(register=True)
        log.info("TraeWork 执行器就绪 mock=%s live=%s 等待任务...", self.args.mock, self.live)
        try:
            while True:
                for msg in self.bus.poll_inbox(timeout=2.0):
                    if msg.get("type") == "task_request":
                        try:
                            self.handle_request(msg)
                        except Exception:
                            log.exception("任务处理异常")
                            self.bus.reply_task(
                                msg, output_text="", status="error",
                                error="executor internal error",
                            )
        except KeyboardInterrupt:
            pass
        finally:
            self.bus.disconnect()
            self.cdp.close()


def main():
    parser = argparse.ArgumentParser(description="TraeWork CN 桌面端节点执行器（CDP 桥接）")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--mock", action="store_true", help="模拟执行，不接 CDP")
    parser.add_argument("--live", action="store_true", help="前台打印注入/轮询过程（演示用）")
    parser.add_argument("--workdir", default=str(ROOT_DIR / "data" / "executor_work"))
    parser.add_argument("--timeout", type=int, default=1800, help="单任务硬超时（秒）")
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT,
                        help="TraeWork CDP 端口（argv.json remote-debugging-port，默认 9433）")
    parser.add_argument("--broker-host", default=None)
    parser.add_argument("--broker-port", type=int, default=None)
    parser.add_argument("--http-base", default=None)
    TraeWorkExecutor(parser.parse_args()).run()


if __name__ == "__main__":
    main()
