"""WorkBuddy 节点执行器：通过官方 deeplink + UIA 把总线任务注入 WorkBuddy 桌面端并回收结果。

原理（GUI 桥接，适用于无 CLI 的桌面智能体）:
  收到 task_request
    → 构造协作提示词（含任务信封: 发起方/执行方/correlation_id 短码）
    → os.startfile('workbuddy://task?action=start&prompt=...') 官方 deeplink 预填输入框
      （注意: 必须走 ShellExecute，过 cmd/bash 会把 URL 里的 & 截断）
    → pywinauto(UIA) 定位输入框 Edit 控件 → click_input 聚焦 → ENTER 提交
    → 轮询 UIA 文本树: 定位含任务短码的用户消息 → 收集其后的助手回复文本
       完成信号: 新出现"已完成 xx"按钮 / 文本连续两轮稳定 / 硬超时
    → reply_task 回传给发起方

前提:
  - WorkBuddy 桌面端已安装且已登录（未运行时 deeplink 会冷启动，等待窗口即可）
  - WorkBuddy 以普通权限运行（管理员进程会触发 UIPI 输入拦截，err=5）
  - pip install pywinauto

用法:
  python executor/workbuddy_executor.py --agent-id workbuddy_pc1 --name "WorkBuddy@PC1"
  python executor/workbuddy_executor.py --mock    # 不动 GUI，模拟执行（联调用）
  python executor/workbuddy_executor.py --live    # 前台实时打印过程（演示用）
"""
import argparse
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import AgentBus, BusConfig  # noqa: E402
from agent_bus.files import download_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("workbuddy_executor")

WINDOW_TITLE = "WorkBuddy"
DEEPLINK_PREFIX = "workbuddy://task?action=start&prompt="
POLL_INTERVAL = 3.0        # UIA 轮询间隔（秒）
SUBMIT_WAIT = 4.0          # 每次 Enter 后等待对话出现的时间

# 提示词要短：它要过 URL。信封放头部（对话截断只砍尾部，短码可存活）。
PROMPT_TEMPLATE = """【跨智能体任务】发起方:{sender_id} → 执行方:{receiver_id}(你) 编号:{task_tag}
{instruction}{context_block}{attachment_block}
请直接执行，最终结论写在回复里（将按编号回传给发起方）。"""

# UIA 文本树里的界面噪音（非对话正文）
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_MODEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,11}$")
_NOISE_PREFIX = ("今天帮你做些什么", "内容由 AI 生成", "引用来源", "对话内搜索")


def _is_noise(text: str) -> bool:
    t = text.strip()
    if not t or t in ("\ufeff", "WorkBuddy", "默认权限", "暂无内容"):
        return True
    if _TIME_RE.match(t):
        return True
    return any(t.startswith(p) for p in _NOISE_PREFIX)


def _strip_tail_noise(parts: list) -> list:
    """去掉尾部的时间戳/模型名（如 'Hy3'、'19:14'），保留正文。"""
    while parts:
        t = parts[-1].strip()
        if _TIME_RE.match(t) or _MODEL_RE.match(t):
            parts.pop()
        else:
            break
    return parts


class WorkBuddyExecutor:
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
            capabilities=["desktop", "office", "files"] if not args.mock else ["mock"],
            executor="workbuddy_gui" if not args.mock else "mock",
            config=cfg,
        )

    # ---------- GUI 桥接三步 ----------

    def _connect_window(self, wait: float = 20.0):
        """连接 WorkBuddy 主窗口（冷启动时等它出现）。"""
        from pywinauto import Application
        deadline = time.time() + wait
        last_err = None
        while time.time() < deadline:
            try:
                app = Application(backend="uia").connect(title=WINDOW_TITLE, timeout=3)
                dlg = app.window(title=WINDOW_TITLE)
                if dlg.exists(timeout=1):
                    return app, dlg
            except Exception as e:
                last_err = e
            time.sleep(1.5)
        raise RuntimeError(f"找不到 WorkBuddy 窗口（{last_err}）；请确认已启动且为普通权限运行")

    def _walk(self, dlg, kinds: tuple, max_depth=12):
        """按 UIA 树先序遍历收集 (顺序, control_type, name) —— 顺序即视觉顺序。"""
        out = []

        def rec(ctrl, depth):
            if depth > max_depth:
                return
            try:
                ct = ctrl.element_info.control_type
                if ct in kinds:
                    name = ctrl.element_info.name or ""
                    out.append((ct, name))
            except Exception:
                return
            try:
                for ch in ctrl.children():
                    rec(ch, depth + 1)
            except Exception:
                pass

        rec(dlg, 0)
        return out

    def _find_input_edit(self, dlg):
        """定位聊天输入框 Edit：取宽度最大者（输入框横贯窗口底部）。"""
        best, best_w = None, -1
        edits = []

        def rec(ctrl, depth):
            if depth > 12:
                return
            try:
                if ctrl.element_info.control_type == "Edit":
                    edits.append(ctrl)
            except Exception:
                return
            try:
                for ch in ctrl.children():
                    rec(ch, depth + 1)
            except Exception:
                pass

        rec(dlg, 0)
        for e in edits:
            try:
                w = e.rectangle().width()
                if w > best_w:
                    best, best_w = e, w
            except Exception:
                continue
        if best is None:
            raise RuntimeError("UIA 树中未找到输入框 Edit 控件")
        return best

    def _inject_prompt(self, prompt: str):
        """deeplink 预填 + 点击聚焦 + ENTER 提交。带"覆盖草稿"弹窗的自愈重试。"""
        import os
        from pywinauto.keyboard import send_keys

        app, dlg = self._connect_window()
        # 注入前快照（用于之后识别"新出现的"对话与按钮）
        texts_before = [n for _, n in self._walk(dlg, ("Text", "ListItem"))]
        btns_before = {n for _, n in self._walk(dlg, ("Button",))}

        url = DEEPLINK_PREFIX + quote(prompt)
        if len(url) > 4000:
            log.warning("prompt 过长（URL %d 字符），WorkBuddy 可能截断", len(url))
        os.startfile(url)  # ShellExecute 直达；绝不能过 cmd（& 会被截断）
        time.sleep(2.5)   # 等窗口弹前 + 预填

        for attempt in range(3):
            dlg = self._connect_window(wait=10)[1]
            try:
                dlg.set_focus()
            except Exception:
                pass
            edit = self._find_input_edit(dlg)
            edit.click_input()          # 真实点击聚焦（负坐标副屏同样有效）
            time.sleep(0.5)
            send_keys("{ENTER}")
            time.sleep(SUBMIT_WAIT)

            # 提交成功判定：文本树出现含任务短码的用户消息
            texts_now = [n for _, n in self._walk(dlg, ("Text", "ListItem", "Button"))]
            task_tag = self._current_task_tag
            if any(task_tag in n for n in texts_now):
                log.info("任务已提交（第 %d 次尝试命中）", attempt + 1)
                return texts_before, btns_before
            # 可能弹了"覆盖当前草稿"确认框：点掉后重试
            for _, n in self._walk(dlg, ("Button",)):
                if n and ("覆盖" in n or n == "确定"):
                    log.info("检测到草稿覆盖弹窗，点击 %r 后重试", n)
                    try:
                        dlg.child_window(title=n, control_type="Button").click_input()
                        time.sleep(1.0)
                    except Exception:
                        pass
                    break
            log.warning("第 %d 次提交未检出用户消息，重试", attempt + 1)
        raise RuntimeError("deeplink 预填/ENTER 提交失败（3 次尝试均未在对话区发现任务消息）")

    def _wait_reply(self, texts_before: list, btns_before: set, timeout: int):
        """轮询 UIA 文本树直到助手回复完成。

        完成信号（任一）:
          a) 新出现"已完成 xx"按钮或"复制"按钮（WorkBuddy 完成态专属）
          b) 回复文本非空且连续两轮稳定
        返回 (reply_text, status, error)
        """
        task_tag = self._current_task_tag
        deadline = time.time() + timeout
        last_reply, stable_rounds = "", 0

        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                app, dlg = self._connect_window(wait=5)
            except Exception as e:
                log.warning("窗口暂不可达: %s", e)
                continue

            texts = [n for _, n in self._walk(dlg, ("Text", "ListItem"))]
            btns = {n for _, n in self._walk(dlg, ("Button",))}

            # a) 完成态按钮
            done_btn = any(
                n.startswith("已完成") or n == "复制"
                for n in btns - btns_before - {""}
            )

            # b) 定位回复区: 用户信封(首个含短码文本)之后找助手发送者标头 'WorkBuddy'，
            #    标头之后到噪音/尾部即回复正文。
            #    注意不能按"含短码"断开——WorkBuddy 会遵守信封协议在回复里回显 #短码。
            reply_parts = []
            env_idx = next((i for i, n in enumerate(texts) if task_tag in n), None)
            if env_idx is not None:
                header_idx = next(
                    (i for i in range(env_idx + 1, len(texts))
                     if texts[i].strip() == WINDOW_TITLE),
                    None,
                )
                if header_idx is not None:
                    seg = texts[header_idx + 1:]  # 发送者标头之后（不能含标头自身）
                else:  # 兜底: 最后一次出现短码的文本（回复回执头）即回复起点
                    last_tag = max(
                        (i for i, n in enumerate(texts) if task_tag in n),
                        default=env_idx,
                    )
                    seg = texts[last_tag:]
                # 跳过正文前紧贴的时间戳/BOM
                while seg and (_TIME_RE.match(seg[0].strip()) or seg[0].strip() == "\ufeff"):
                    seg = seg[1:]
                for n in seg:
                    if _is_noise(n):
                        break
                    reply_parts.append(n)
            reply = "\n".join(_strip_tail_noise(reply_parts)).strip()

            if reply:
                if reply == last_reply:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                last_reply = reply
                if self.live:
                    print(f"  [轮询] 回复 {len(reply)} 字符, 稳定 {stable_rounds}/2, "
                          f"完成按钮={'有' if done_btn else '无'}", flush=True)
                if done_btn or stable_rounds >= 2:
                    return reply, "success", None
            elif self.live:
                print("  [轮询] 暂未检出回复...", flush=True)

        return last_reply, "timeout", f"等待 WorkBuddy 回复超时（>{timeout}s），返回已观察到的部分"

    # ---------- 任务处理 ----------

    def handle_request(self, req: dict):
        task_id = req.get("task_id", "")
        payload = req.get("payload", {})
        timeout = min(req.get("timeout_seconds", 600), self.args.timeout)
        task_tag = task_id[:8]
        self._current_task_tag = task_tag

        # 1. 附件落盘（Claim-Check 下载），路径写进提示词让 WorkBuddy 自取
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
                texts_before, btns_before = self._inject_prompt(prompt)
                output, status, error = self._wait_reply(texts_before, btns_before, timeout)
            except Exception as e:
                output, status, error = "", "error", str(e)
            if self.live:
                print(f"\n  [LIVE] 执行完成 status={status} 耗时 {time.time() - started:.0f}s", flush=True)
                if output:
                    print("─" * 60 + f"\n[WorkBuddy 回复]\n{output}\n" + "─" * 60, flush=True)

        log.info("任务 %s 完成 status=%s 耗时=%.0fs", task_tag, status, time.time() - started)
        self.bus.reply_task(req, output_text=output[:4000], status=status,
                            error=error, artifacts=[], session_id=None)

    def _run_mock(self, req, local_files):
        time.sleep(0.3)
        return (f"[mock] 已收到任务: {req.get('payload', {}).get('instruction', '')[:100]}\n"
                f"附件 {len(local_files)} 个: {[p.name for p in local_files]}", "success", None)

    # ---------- 主循环 ----------

    def run(self):
        self.bus.connect(register=True)
        log.info("WorkBuddy 执行器就绪 mock=%s live=%s 等待任务...", self.args.mock, self.live)
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


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy 桌面端节点执行器")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--mock", action="store_true", help="模拟执行，不动 GUI")
    parser.add_argument("--live", action="store_true", help="前台打印注入/轮询过程（演示用）")
    parser.add_argument("--workdir", default=str(ROOT_DIR / "data" / "executor_work"))
    parser.add_argument("--timeout", type=int, default=1200, help="单任务硬超时（秒）")
    parser.add_argument("--broker-host", default=None)
    parser.add_argument("--broker-port", type=int, default=None)
    parser.add_argument("--http-base", default=None)
    WorkBuddyExecutor(parser.parse_args()).run()


if __name__ == "__main__":
    main()
