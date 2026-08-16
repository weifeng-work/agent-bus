"""OpenCode 节点执行器：把总线任务注入 OpenCode CLI 并回传其输出。

原理（一次性拉起 + 会话延续）:
  收到 task_request
    → 下载附件到任务子目录
    → 构造协作提示词（含任务信封: 发起方/执行方/correlation_id）
    → 拉起 `opencode run [--session <sid>] "<prompt>" --format json --auto [-f <file>...]`
    → 解析 stdout 中的 NDJSON 事件流（拼接 type=="text" 的 part.text）
    → reply_task 回传给发起方

与 codebuddy CLI 的参数映射:
    codebuddy -p                     → opencode run
    --resume <sid>                   → --session <sid>
    --output-format json             → --format json
    -y                               → --auto
    （附件）下载后写入提示词         → -f <file> 原生附加

跨平台说明（Windows / Linux / macOS 同一份代码）:
    - 二进制查找: OPENCODE_PATH 环境变量 > PATH(which) > npm 全局 bin > nvm bin 兜底
    - 认证/会话存在 ~/.local/share/opencode/（SQLite），必须与 opencode 登录时同一 OS 用户运行
    - Windows 注意: npm 安装需 --allow-scripts=opencode-ai 才能装上平台二进制
    - Linux 注意: cron/systemd 干净环境下 PATH 常不含 nvm bin，靠上述兜底查找解决

用法:
  python executor/opencode_executor.py --agent-id opencode_pc1 --name "OpenCode@PC1"
  python executor/opencode_executor.py --mock          # 不调 OpenCode，模拟执行（联调用）
  python executor/opencode_executor.py --live          # 前台实时输出到终端（演示用）
"""
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import AgentBus, BusConfig  # noqa: E402
from agent_bus.files import download_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("opencode_executor")

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


# ---------- 二进制查找（跨平台 + PATH 兜底） ----------

def _nvm_bin_dirs() -> list:
    """nvm 安装的 node 全局 bin 目录（Linux/macOS 常见两种布局）。"""
    home = Path.home()
    pats = [".nvm/versions/node/*/bin", ".config/nvm/versions/node/*/bin"]
    dirs = []
    for pat in pats:
        dirs.extend(sorted(home.glob(pat), reverse=True))
    return dirs


def _npm_global_bin() -> list:
    """npm 全局 node_modules 下的平台包 bin 目录（opencode-linux-x64 等）。"""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return []
    try:
        root = subprocess.run(
            [npm, "root", "-g"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        ).stdout.strip()
        out = []
        p = Path(root)
        if p.is_dir():
            # shim 形式: <npm root -g>/../bin 或 .bin
            for cand in (p.parent / "bin", p / ".bin", p):
                if cand.is_dir():
                    out.append(cand)
            # 平台二进制包: opencode-linux-x64 / opencode-windows-x64 / ...
            for sub in p.glob("opencode-*-x64*/bin"):
                out.append(sub)
            for sub in p.glob("opencode-ai/node_modules/opencode-*/bin"):
                out.append(sub)
        return out
    except Exception:
        return []


def find_opencode() -> str:
    """按优先级查找 opencode 可执行文件。

    1. 环境变量 OPENCODE_PATH（显式指定，最可靠）
    2. PATH 中的 opencode / opencode.cmd / opencode.exe
    3. npm 全局目录与 nvm bin 兜底扫描（干净环境下 PATH 常缺失）
    """
    env = os.environ.get("OPENCODE_PATH")
    if env and Path(env).exists():
        return env

    names = ("opencode", "opencode.cmd", "opencode.exe")
    for name in names:
        path = shutil.which(name)
        if path:
            return path

    for d in _npm_global_bin() + _nvm_bin_dirs():
        for name in names:
            cand = d / name
            if cand.is_file():
                return str(cand)
    raise FileNotFoundError(
        "找不到 opencode 可执行文件。请确认已安装（npm i -g opencode-ai），"
        "或设置 OPENCODE_PATH 环境变量指向其二进制"
    )


# ---------- NDJSON 事件流解析 ----------

def parse_opencode_stream(text: str):
    """解析 opencode run --format json 的 NDJSON 事件流。

    每行一个 JSON 事件，核心结构:
      {"type":"step_start","sessionID":"ses_...","part":{...}}
      {"type":"text","sessionID":"ses_...","part":{"type":"text","text":"回复正文",...}}
      {"type":"step_finish","sessionID":"ses_...","part":{...,"tokens":{...}}}

    返回 (output_text, session_id, error_msg):
      - output_text: 全部 text 事件 part.text 的拼接（同一 messageID 的流式分片直接相连，不同消息换行）
      - session_id: 顶层 sessionID（首条事件即可取到）
      - error_msg:  error 事件的错误信息（无则为 None）
    """
    msg_texts, msg_order, session_id, error_msg = {}, [], None, None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue  # 忽略非 JSON 行（日志/进度输出）
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if not isinstance(evt, dict):
            continue
        sid = evt.get("sessionID") or evt.get("session_id")
        if sid:
            session_id = sid
        etype = evt.get("type", "")
        if etype == "text":
            part = evt.get("part") or {}
            t = part.get("text") if isinstance(part, dict) else None
            if t:
                mid = (part.get("messageID") if isinstance(part, dict) else None) \
                    or evt.get("messageID") or "_"
                if mid not in msg_texts:
                    msg_texts[mid] = []
                    msg_order.append(mid)
                msg_texts[mid].append(t)
        elif etype == "error":
            part = evt.get("part") or {}
            error_msg = (part.get("data") or part.get("message")
                         or json.dumps(evt, ensure_ascii=False)[:500]) \
                if isinstance(part, dict) else json.dumps(evt, ensure_ascii=False)[:500]
    output = "\n".join("".join(msg_texts[mid]) for mid in msg_order)
    return output, session_id, error_msg


def build_prompt(req: dict, workdir: Path, cli_path: Path) -> str:
    payload = req.get("payload", {})
    ctx = payload.get("context_data")
    context_block = f"【附加上下文】{ctx}\n" if ctx else ""
    urls = payload.get("attachment_urls") or []
    attachment_block = "【附件】已通过 -f 附加到本条消息\n" if urls else ""
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


class OpenCodeExecutor:
    def __init__(self, args):
        self.args = args
        self.live = getattr(args, "live", False)
        self.oc_path = None if args.mock else find_opencode()
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
            executor="opencode_cli" if not args.mock else "mock",
            config=cfg,
        )
        # opencode 无需登录即可使用，健康态恒为 ok
        self.bus.health = "ok" if not args.mock else "unknown"

    # ---------- 任务执行 ----------

    def handle_request(self, req: dict):
        task_id = req.get("task_id", "")
        payload = req.get("payload", {})
        timeout = min(req.get("timeout_seconds", 600), self.args.timeout)
        task_dir = self.workdir / f"task_{task_id[:8]}"
        task_dir.mkdir(parents=True, exist_ok=True)

        # 1. 附件落盘（Claim-Check 下载），opencode 用 -f 原生附加
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

        # 2. 执行（mock 或 opencode）
        #    注意: opencode 会话存于 ~/.local/share/opencode/（按项目目录归档），
        #    统一在固定 workdir 下执行以保证 --session 能找到（附件放 task 子目录，用绝对路径 -f）。
        started = time.time()
        if self.args.mock:
            output, session_id, status, error = self._run_mock(req, local_files)
        elif self.live:
            output, session_id, status, error = self._run_opencode_live(
                prompt, payload.get("session_id"), timeout, self.workdir, req, local_files)
        else:
            output, session_id, status, error = self._run_opencode(
                prompt, payload.get("session_id"), timeout, self.workdir, local_files)

        elapsed = round(time.time() - started, 2)
        log.info("任务 %s 完成 status=%s 耗时=%ss", task_id[:8], status, elapsed)

        # 3. 回传
        self.bus.reply_task(
            req, output_text=output, status=status, error=error,
            artifacts=[], session_id=session_id,
        )

    def _make_cmd(self, session_id, local_files) -> list:
        """构造 opencode 命令行。prompt 不走 argv：opencode run 会把 argv prompt
        截断在第一个换行符（实测 argv 只送达首行，stdin 完整送达），统一走 stdin。"""
        cmd = [self.oc_path, "run"]
        if session_id:
            cmd += ["--session", str(session_id)]
        cmd += ["--format", "json", "--auto"]
        for f in local_files:
            cmd += ["-f", str(f)]
        return cmd

    def _spawn_env(self) -> dict:
        """子进程环境：把 opencode 所在目录并入 PATH（覆盖 nvm 未加载等场景）。"""
        env = os.environ.copy()
        bindir = str(Path(self.oc_path).parent)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        return env

    def _run_opencode(self, prompt: str, session_id, timeout: int, cwd: Path,
                      local_files: list):
        cmd = self._make_cmd(session_id, local_files)
        if session_id:
            log.info("延续会话 %s", session_id)
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd), input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                env=self._spawn_env(),
            )
        except subprocess.TimeoutExpired:
            return "", None, "timeout", f"OpenCode 执行超时（>{timeout}s），进程已终止"
        except Exception as e:
            return "", None, "error", f"拉起 OpenCode 失败: {e}"

        output, sid, err_evt = parse_opencode_stream(proc.stdout or "")
        if err_evt:
            return output[:4000], sid, "error", f"opencode error 事件: {err_evt}"
        if output:
            if proc.returncode != 0:
                return (proc.stderr or output)[:4000], sid, "error", \
                    f"exit_code={proc.returncode}"
            return output, sid, "success", None
        # 一个 text 事件都没收到 → 视为失败，带回 stderr 供诊断（而非空回复）
        raw = (proc.stderr or proc.stdout or "").strip()
        msg = raw[:4000] if raw else f"opencode 无输出（exit_code={proc.returncode}）"
        return msg, sid, ("success" if proc.returncode == 0 and sid else "error"), \
            None if (proc.returncode == 0 and sid) else f"exit_code={proc.returncode}"

    def _run_opencode_live(self, prompt: str, session_id, timeout: int,
                           cwd: Path, req: dict, local_files: list):
        """前台 live 模式：text 事件实时打印到终端（给旁观者看），
        同时收集全部事件用于解析正文/sessionID。
        """
        cmd = self._make_cmd(session_id, local_files)

        # ── 任务信封：在终端打印任务来源，让旁观者看懂发生了什么 ──
        print("\n" + "=" * 60, flush=True)
        print(f"  [LIVE] 收到任务 from {req.get('sender_id', '?')}", flush=True)
        print(f"  任务编号: {req.get('correlation_id', '?')[:16]}...", flush=True)
        print(f"  指令: {req.get('payload', {}).get('instruction', '')[:120]}", flush=True)
        if session_id:
            print(f"  延续会话: {session_id[:16]}...", flush=True)
        print("=" * 60, flush=True)
        print("  OpenCode 开始执行...\n", flush=True)

        collected = []
        final_output, final_session, err_evt = "", session_id, None

        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=self._spawn_env(),
            )
            # prompt 走 stdin（argv 会在首个换行处被 opencode 截断，见 _make_cmd 注释）
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                collected.append(line)
                s = line.strip()
                if not s.startswith("{"):
                    continue
                try:
                    evt = json.loads(s)
                except ValueError:
                    continue
                if not isinstance(evt, dict):
                    continue
                sid = evt.get("sessionID") or evt.get("session_id")
                if sid:
                    final_session = sid
                etype = evt.get("type", "")
                if etype == "text":
                    t = (evt.get("part") or {}).get("text", "")
                    if t:
                        print(t, end="", flush=True)
                elif etype == "error":
                    err_evt = json.dumps(evt, ensure_ascii=False)[:500]
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return "", final_session, "timeout", f"OpenCode 执行超时（>{timeout}s）"
        except Exception as e:
            return "", final_session, "error", f"拉起 OpenCode 失败: {e}"

        print(f"\n\n{'=' * 60}", flush=True)
        print(f"  [LIVE] OpenCode 执行完成 exit_code={proc.returncode}", flush=True)
        print(f"  回传结果给 {req.get('sender_id', '?')}", flush=True)
        print("=" * 60 + "\n", flush=True)

        # 兜底：实时流没拼出正文，整体重新解析一次
        final_output, sid, err_evt2 = parse_opencode_stream("".join(collected))
        if sid:
            final_session = sid
        if err_evt2:
            err_evt = err_evt2
        if err_evt:
            return final_output[:4000], final_session, "error", \
                f"opencode error 事件: {err_evt}"
        if not final_output:
            stderr = (proc.stderr.read() if proc.stderr else "") or ""
            raw = stderr.strip()[:4000] if stderr.strip() else \
                f"opencode 无输出（exit_code={proc.returncode}）"
            ok = proc.returncode == 0 and final_session
            return raw, final_session, "success" if ok else "error", \
                None if ok else f"exit_code={proc.returncode}"
        if proc.returncode != 0:
            return final_output[:4000], final_session, "error", \
                f"exit_code={proc.returncode}"
        return final_output[:4000], final_session, "success", None

    def _run_mock(self, req: dict, local_files):
        time.sleep(0.3)
        payload = req.get("payload", {})
        output = (f"[mock] 已收到任务: {payload.get('instruction', '')[:100]}\n"
                  f"附件 {len(local_files)} 个: {[p.name for p in local_files]}")
        return output, f"mock-session-{req.get('task_id', '')[:8]}", "success", None

    # ---------- 主循环 ----------

    def run(self):
        self.bus.connect(register=True)
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
    parser = argparse.ArgumentParser(description="OpenCode 节点执行器（跨平台）")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--mock", action="store_true", help="模拟执行，不调用 OpenCode")
    parser.add_argument("--live", action="store_true",
                        help="前台 live 模式：OpenCode 输出实时打印到终端（演示用）")
    parser.add_argument("--workdir", default=str(ROOT_DIR / "data" / "executor_work"))
    parser.add_argument("--timeout", type=int, default=1800,
                        help="单任务硬超时（秒），默认 1800（模型推理可达数分钟）")
    parser.add_argument("--broker-host", default=None)
    parser.add_argument("--broker-port", type=int, default=None)
    parser.add_argument("--http-base", default=None)
    OpenCodeExecutor(parser.parse_args()).run()


if __name__ == "__main__":
    main()
