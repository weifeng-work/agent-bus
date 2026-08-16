"""CodeBuddy 节点执行器：把总线任务注入 CodeBuddy CLI 并回传其输出。

原理（一次性拉起 + 会话延续）:
  收到 task_request
    → 下载附件到工作目录
    → 构造协作提示词（含任务信封: 发起方/执行方/correlation_id）
    → 拉起 `codebuddy -p [--resume <sid>] "<prompt>" --output-format json -y`
    → 解析 stdout 中的 JSON（result / session_id）
    → reply_task 回传给发起方

用法:
  python executor/codebuddy_executor.py --agent-id codebuddy_pc1 --name "CodeBuddy@PC1"
  python executor/codebuddy_executor.py --mock          # 不调 CodeBuddy，模拟执行（联调用）
  python executor/codebuddy_executor.py --live          # 前台实时输出到终端（演示用）
"""
import argparse
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import AgentBus, BusConfig  # noqa: E402
from agent_bus.files import download_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("codebuddy_executor")

OUTPUT_LIMIT = 20000  # 回传正文上限（审核报告类长文 4000 不够；MQTT 单消息可承载）

PROMPT_TEMPLATE = """你正在参与一个跨机器多智能体协作系统，有任务需要你完成。

【任务信封】(多智能体防串扰，请核对后再执行)
- 发起方 agent_id: {sender_id}
- 执行方 agent_id: {receiver_id} (即你)
- 任务编号 correlation_id: {task_id}

【任务指令】
{instruction}
{context_block}{attachment_block}
协作规则:
1. 核对执行方 ID 确实是你；直接执行任务，不要询问。
2. 如需与其他智能体协作（含向发起方追问），用 Bash 调用通信 CLI（工作目录: {workdir}），务必指定准确的 --to <agent_id>:
   - 查看在线智能体: python "{cli_path}" agents
   - 给指定智能体发任务: python "{cli_path}" send --to <agent_id> --text "任务指令" --wait 300
   - 上传文件给对方: python "{cli_path}" upload <文件路径>
3. 完成后，把最终结论直接写在你的最终回复里（系统会按任务编号自动回传给发起方 {sender_id}）。回复不要包含与任务无关的内容。"""


def find_codebuddy() -> str:
    for name in ("codebuddy", "codebuddy.cmd", "codebuddy.exe"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError("找不到 codebuddy 可执行文件，请确认已安装并在 PATH 中")


# ---- CLI 登录态检测（auth_required 识别与恢复） ----

# 未登录时 CLI 报错的典型特征（小写匹配；避免裸 401/403 误报任务内容）
_AUTH_MARKERS = (
    "未登录", "请先登录", "请登录", "登录后重试", "登录已过期",
    "not logged in", "please log in", "please sign in", "sign in required",
    "login required", "unauthorized", "forbidden",
    "authentication required", "not authenticated",
    "token expired", "invalid token", "no valid token",
    'status":401', 'status":403', "status: 401", "status: 403",
)

AUTH_REQUIRED_HINT = (
    "CodeBuddy CLI 未登录，无法执行任务。修复方法：在该机器的终端运行 codebuddy "
    "（交互模式，不带 -p），按提示完成登录后退出即可；执行器会自动检测并恢复，无需重启。"
)


def looks_like_auth_failure(*texts) -> bool:
    """判断 CLI 输出/报错文本是否为登录认证类失败（区别于普通任务错误）。"""
    blob = "\n".join(t for t in texts if t).lower()
    return any(m in blob for m in _AUTH_MARKERS)


def extract_json(text: str):
    """从 stdout 容错提取 JSON。

    CodeBuddy 2.x 的 --output-format json 实际输出为"事件数组"：
    [ {type:message,role:user,...}, {type:message,role:assistant,...}, {type:result, result, session_id,...} ]
    最终结论在末尾 type=="result" 元素；个别版本/场景可能输出单对象或前后带杂音。
    """
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = text.find(opener), text.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except ValueError:
                continue
    return None


def _extract_assistant_text(evt: dict) -> list:
    """从一条 assistant 事件中提取所有可读文本。

    兼容 CodeBuddy 不同版本/格式的 stream-json 事件结构：
      - 标准结构: {type:"message", role:"assistant", content:[{type:"output_text", text:"..."}]}
      - 简化版:  {type:"assistant", text:"..."}
      - 增量版:  {type:"message", role:"assistant", delta:{text:"..."}}
      - content 字段名差异: output_text | text | output
    """
    texts = []
    if not isinstance(evt, dict):
        return texts
    # 顶层 text 字段（简化版事件）
    if isinstance(evt.get("text"), str) and evt["text"]:
        texts.append(evt["text"])
    # content 数组（标准结构）
    content = evt.get("content")
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type", "")
            if ctype in ("output_text", "text", "output"):
                t = c.get("text", "")
                if t:
                    texts.append(t)
            elif ctype == "delta" and isinstance(c.get("text"), str) and c["text"]:
                texts.append(c["text"])
    # delta 字段（流式增量）
    delta = evt.get("delta")
    if isinstance(delta, str) and delta:
        texts.append(delta)
    elif isinstance(delta, dict):
        t = delta.get("text") or delta.get("content")
        if isinstance(t, str) and t:
            texts.append(t)
    return texts


def _is_assistant_event(evt: dict) -> bool:
    """判断一条事件是否为 assistant 输出（兼容多种事件类型/role 命名）。"""
    if not isinstance(evt, dict):
        return False
    etype = evt.get("type", "")
    role = evt.get("role", "")
    if etype in ("assistant", "assistant_message", "output_text", "text"):
        return True
    if etype == "message" and role == "assistant":
        return True
    # 兜底：含 content/delta/text 字段且非 user 的事件，按 assistant 处理
    if role != "user" and (evt.get("content") or evt.get("delta") or evt.get("text")):
        return etype in ("message", "message_delta", "response")
    return False


def _assistant_texts(events) -> str:
    """从事件数组提取全部 assistant 输出文本（兜底）。"""
    parts = []
    for item in events if isinstance(events, list) else []:
        if _is_assistant_event(item):
            parts.extend(_extract_assistant_text(item))
    return "\n".join(p for p in parts if p)


def _result_to_text(val) -> str:
    """result 事件正文字段 → 纯文本。

    部分版本 CodeBuddy 把整段会话数组（含 user 消息里的 system-reminder 噪音）
    序列化成 JSON 字符串塞进 result.result——必须解析后只取 assistant 文本，
    否则截断后全是噪音、真正的回答永远露不出来。
    """
    if val is None:
        return ""
    if isinstance(val, str):
        s = val.strip()
        if s[:1] in "[{":
            try:
                return _result_to_text(json.loads(s))
            except ValueError:
                return s
        return s
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict) and _is_assistant_event(item):
                parts.extend(_extract_assistant_text(item))
        return "\n".join(p for p in parts if p)
    if isinstance(val, dict):
        texts = _extract_assistant_text(val)
        if texts:
            return "\n".join(texts)
        return _result_to_text(val.get("text") or val.get("output") or val.get("content"))
    return str(val)


def parse_codebuddy_output(text: str):
    """解析 CodeBuddy stdout，返回 (output_text, session_id)。"""
    data = extract_json(text)
    if isinstance(data, dict):
        data = [data]
    if isinstance(data, list):
        session_id = None
        for item in data:
            if isinstance(item, dict):
                sid = item.get("session_id") or item.get("sessionId")
                if sid:
                    session_id = sid
        result_elem = next(
            (x for x in data if isinstance(x, dict) and x.get("type") == "result"), None
        )
        if result_elem:
            output = _result_to_text(result_elem.get("result")) or _assistant_texts(data)
            return output.strip()[:OUTPUT_LIMIT], session_id
        texts = _assistant_texts(data)
        if texts:
            return texts[:OUTPUT_LIMIT], session_id
    return (text or "").strip()[:OUTPUT_LIMIT], None


def build_prompt(req: dict, workdir: Path, cli_path: Path) -> str:
    payload = req.get("payload", {})
    ctx = payload.get("context_data")
    context_block = f"【附加上下文】{ctx}\n" if ctx else ""
    urls = payload.get("attachment_urls") or []
    attachment_block = "【附件】已在工作目录: 见上方说明\n" if urls else ""
    return PROMPT_TEMPLATE.format(
        sender_id=req.get("sender_id", "?"),
        receiver_id=req.get("target_id", "?"),
        task_id=req.get("correlation_id", "?"),
        instruction=payload.get("instruction", ""),
        context_block=context_block,
        attachment_block=attachment_block,
        workdir=workdir,
        cli_path=cli_path,
    )


class CodeBuddyExecutor:
    def __init__(self, args):
        self.args = args
        self.live = getattr(args, "live", False)
        self.cb_path = None if args.mock else find_codebuddy()
        self.workdir = Path(args.workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.cli_path = (ROOT_DIR / "skill" / "cli.py").resolve()
        cfg = BusConfig.load(
            broker_host=args.broker_host, broker_port=args.broker_port,
            http_base=args.http_base, agent_id=args.agent_id,
        )
        self.bus = AgentBus(
            args.agent_id, name=args.name,
            capabilities=["code", "files", "shell"] if not args.mock else ["mock"],
            executor="codebuddy_cli" if not args.mock else "mock",
            config=cfg,
        )

    # ---------- 任务执行 ----------

    def handle_request(self, req: dict):
        task_id = req.get("task_id", "")
        payload = req.get("payload", {})
        timeout = min(req.get("timeout_seconds", 600), self.args.timeout)
        task_dir = self.workdir / f"task_{task_id[:8]}"
        task_dir.mkdir(parents=True, exist_ok=True)

        # 1. 附件落盘（Claim-Check 下载）
        local_files = []
        for url in payload.get("attachment_urls") or []:
            try:
                name = Path(url.split("?")[0]).name
                dest = task_dir / f"att_{len(local_files)}_{name}"
                download_file(url, str(dest), self.bus.cfg.http_base)
                local_files.append(dest)
                log.info("附件已下载: %s", dest.name)
            except Exception as e:  # 单个附件失败不阻断任务
                log.warning("附件下载失败 %s: %s", url, e)

        prompt = build_prompt(req, task_dir, self.cli_path)
        if local_files:
            names = ", ".join(str(p) for p in local_files)
            prompt = prompt.replace("【附件】已在工作目录: 见上方说明",
                                    f"【附件】已下载到: {names}")

        # 2. 执行（mock 或 codebuddy）
        #    注意: CodeBuddy 的会话记录按工作目录归档，--resume 只能在同一 cwd 下找到。
        #    因此 codebuddy 统一在固定 workdir 下执行（附件放 task 子目录，路径用绝对路径）。
        started = time.time()
        if self.args.mock:
            output, session_id, status, error = self._run_mock(req, local_files)
        elif self.live:
            output, session_id, status, error = self._run_codebuddy_live(
                prompt, payload.get("session_id"), timeout, self.workdir, req)
        else:
            output, session_id, status, error = self._run_codebuddy(
                prompt, payload.get("session_id"), timeout, self.workdir, task_dir)

        elapsed = round(time.time() - started, 2)
        log.info("任务 %s 完成 status=%s 耗时=%ss", task_id[:8], status, elapsed)

        # 2.5 登录态失败识别：错误文本命中认证特征 → 标记 health 并改写为可读指引
        if not self.args.mock:
            if status == "error" and looks_like_auth_failure(output, error):
                self.bus.set_health("auth_required")
                raw_detail = ((error or "") + "\n" + (output or ""))[:400]
                output = AUTH_REQUIRED_HINT
                error = f"auth_required: CodeBuddy CLI 未登录（原始错误: {raw_detail}）"
                log.warning("任务 %s 因 CLI 未登录失败，已标记 health=auth_required", task_id[:8])
            elif status == "success" and self.bus.health == "auth_required":
                self.bus.set_health("ok")  # 真实任务成功 → 自动恢复

        # 3. 回传
        self.bus.reply_task(
            req, output_text=output, status=status, error=error,
            artifacts=[], session_id=session_id,
        )

    def _run_codebuddy(self, prompt: str, session_id, timeout: int, cwd: Path,
                       task_dir: Path = None):
        cmd = [self.cb_path, "-p"]
        if session_id:
            cmd += ["--resume", str(session_id)]
            log.info("延续会话 %s", session_id)
        cmd += ["--output-format", "json", "-y", prompt]
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return "", None, "timeout", f"CodeBuddy 执行超时（>{timeout}s），进程已终止"
        except Exception as e:
            return "", None, "error", f"拉起 CodeBuddy 失败: {e}"

        # 原始输出落盘（排障证据：解析器对不上版本格式时，可事后分析真实结构）
        if task_dir is not None:
            try:
                (task_dir / "stdout_raw.json").write_text(proc.stdout or "", encoding="utf-8")
                (task_dir / "stderr_raw.txt").write_text(proc.stderr or "", encoding="utf-8")
            except OSError as e:
                log.warning("原始输出落盘失败: %s", e)

        data = extract_json(proc.stdout or "")
        output, session = parse_codebuddy_output(proc.stdout or "")
        if data is not None and output:
            if proc.returncode != 0:
                return (proc.stderr or output)[:OUTPUT_LIMIT], session, "error", f"exit_code={proc.returncode}"
            return output, session, "success", None
        # stdout 非 JSON：退回原始文本
        raw = (proc.stdout or proc.stderr or "").strip()
        if proc.stdout and proc.stdout.lstrip().startswith("[") and len(raw) > OUTPUT_LIMIT:
            # 疑似未闭合会话数组：结论在尾部（assistant/result），头部是 user 上下文噪音
            raw = "...(前略)...\n" + raw[-(OUTPUT_LIMIT - 15):]
        return raw[:OUTPUT_LIMIT], None, ("success" if proc.returncode == 0 else "error"), \
               None if proc.returncode == 0 else f"exit_code={proc.returncode}"

    def _run_codebuddy_live(self, prompt: str, session_id, timeout: int,
                            cwd: Path, req: dict):
        """前台 live 模式：CodeBuddy stdout 逐行实时打印到终端（给旁观者看），
        同时收集 stream-json 事件用于解析 result/session_id。

        兼容多种事件结构（详见 _is_assistant_event / _extract_assistant_text）。
        若实时未捕获到 assistant 文本但 result 事件含正文，会兜底补打一次，
        保证旁观者总能看到 CodeBuddy 的实际回复。
        """
        cmd = [self.cb_path, "-p"]
        if session_id:
            cmd += ["--resume", str(session_id)]
        cmd += ["--output-format", "stream-json", "-y", prompt]

        # ── 任务信封：在终端打印任务来源，让旁观者看懂发生了什么 ──
        print("\n" + "=" * 60, flush=True)
        print(f"  [LIVE] 收到任务 from {req.get('sender_id', '?')}", flush=True)
        print(f"  任务编号: {req.get('correlation_id', '?')[:16]}...", flush=True)
        print(f"  指令: {req.get('payload', {}).get('instruction', '')[:120]}", flush=True)
        if session_id:
            print(f"  延续会话: {session_id[:16]}...", flush=True)
        print("=" * 60, flush=True)
        print("  CodeBuddy 开始执行...\n", flush=True)

        collected_lines = []
        final_output = ""
        final_session = session_id
        assistant_printed = False   # 实时是否已打印过 assistant 正文
        seen_event_types = set()     # 调试用：记录所有事件类型

        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,  # 行缓冲，配合 text=True 尽早吐出
            )
            # 逐行读取 stdout，实时打印 + 累积
            for line in proc.stdout:
                collected_lines.append(line)
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    evt = json.loads(line_stripped)
                except ValueError:
                    # 非 JSON 行：原样打印（可能是工具调用日志、进度条等）
                    print(line, end="", flush=True)
                    continue
                if not isinstance(evt, dict):
                    continue

                etype = evt.get("type", "")
                if etype:
                    seen_event_types.add(etype)
                log.debug("stream-json 事件 type=%s role=%s", etype, evt.get("role", ""))

                # assistant 文本输出 → 实时打印（朋友能看懂）
                if _is_assistant_event(evt):
                    for txt in _extract_assistant_text(evt):
                        if txt:
                            print(txt, end="", flush=True)
                            assistant_printed = True
                # result 事件 → 提取最终结论和 session_id
                elif etype == "result":
                    final_output = _result_to_text(
                        evt.get("result") or evt.get("output_text") or "")
                    sid = evt.get("session_id") or evt.get("sessionId")
                    if sid:
                        final_session = sid

            proc.wait(timeout=timeout)

        except subprocess.TimeoutExpired:
            proc.kill()
            return "", final_session, "timeout", f"CodeBuddy 执行超时（>{timeout}s）"
        except Exception as e:
            return "", final_session, "error", f"拉起 CodeBuddy 失败: {e}"

        print(f"\n\n{'=' * 60}", flush=True)
        print(f"  [LIVE] CodeBuddy 执行完成 exit_code={proc.returncode}", flush=True)
        print(f"  回传结果 ({len(final_output)} 字符) 给 {req.get('sender_id', '?')}", flush=True)
        if seen_event_types:
            log.info("本次 stream-json 事件类型: %s", sorted(seen_event_types))
        print("=" * 60 + "\n", flush=True)

        # 兜底1：实时没打印过 assistant 文本，但 result 事件里有正文 → 补打一次
        # （覆盖 CodeBuddy 把整段回复塞进 result.result 而未发 message 事件的情况）
        if not assistant_printed and final_output:
            print("─" * 60, flush=True)
            print("[CodeBuddy 回复正文]", flush=True)
            print(final_output, flush=True)
            print("[/CodeBuddy 回复正文]", flush=True)
            print("─" * 60 + "\n", flush=True)

        # 兜底2：stream-json 没解析到 result，尝试整体解析累积的输出
        if not final_output:
            full = "".join(collected_lines)
            output, sid = parse_codebuddy_output(full)
            final_output = output
            if sid:
                final_session = sid
            # 兜底3：整体解析后仍没拿到，但实时已经打印过 assistant 文本，用打印过的内容
            if not final_output and assistant_printed:
                events = []
                for l in collected_lines:
                    s = l.strip()
                    if not s or not (s.startswith("{") or s.startswith("[")):
                        continue
                    try:
                        events.append(json.loads(s))
                    except ValueError:
                        continue
                final_output = _assistant_texts(events)

        if proc.returncode != 0 and not final_output:
            return (proc.stderr or "")[:OUTPUT_LIMIT], final_session, "error", \
                   f"exit_code={proc.returncode}"
        return final_output[:OUTPUT_LIMIT], final_session, "success", None

    def _run_mock(self, req: dict, local_files):
        time.sleep(0.3)
        payload = req.get("payload", {})
        output = (f"[mock] 已收到任务: {payload.get('instruction', '')[:100]}\n"
                  f"附件 {len(local_files)} 个: {[p.name for p in local_files]}")
        return output, f"mock-session-{req.get('task_id', '')[:8]}", "success", None

    # ---------- 登录态自动恢复 ----------

    def _auth_watch_loop(self):
        """health=auth_required 期间每 60s 做一次最小探测任务。

        未登录时请求被认证层拒绝（不消耗 token）；登录成功后探测通过即自动恢复，
        之后停止探测（health=ok 不再发探测任务）。
        """
        while True:
            time.sleep(60)
            if self.bus.health != "auth_required":
                continue
            try:
                proc = subprocess.run(
                    [self.cb_path, "-p", "回复OK", "--output-format", "json", "-y"],
                    cwd=str(self.workdir), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=120,
                )
                out, _, _ = parse_codebuddy_output(proc.stdout or "")
                if (proc.returncode == 0 and out
                        and not looks_like_auth_failure(out, proc.stderr)):
                    self.bus.set_health("ok")
                    log.info("探测成功: CodeBuddy 登录态已恢复")
                else:
                    log.info("探测仍未登录，60s 后重试")
            except Exception as e:
                log.warning("登录探测异常: %s", e)

    # ---------- 主循环 ----------

    def run(self):
        self.bus.connect(register=True)
        if not self.args.mock:
            threading.Thread(target=self._auth_watch_loop, daemon=True).start()
        log.info("执行器就绪 mock=%s live=%s workdir=%s 等待任务...",
                 self.args.mock, self.live, self.workdir)
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
                                error="executor internal error (see server logs)",
                            )
        except KeyboardInterrupt:
            pass
        finally:
            self.bus.disconnect()


def main():
    parser = argparse.ArgumentParser(description="CodeBuddy 节点执行器")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--mock", action="store_true", help="模拟执行，不调用 CodeBuddy")
    parser.add_argument("--live", action="store_true",
                        help="前台 live 模式：CodeBuddy 输出实时打印到终端（演示用）")
    parser.add_argument("--workdir", default=str(ROOT_DIR / "data" / "executor_work"))
    parser.add_argument("--timeout", type=int, default=1800, help="单任务硬超时（秒）")
    parser.add_argument("--broker-host", default=None)
    parser.add_argument("--broker-port", type=int, default=None)
    parser.add_argument("--http-base", default=None)
    CodeBuddyExecutor(parser.parse_args()).run()


if __name__ == "__main__":
    main()
