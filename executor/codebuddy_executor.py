"""CodeBuddy 节点执行器：把总线任务注入 CodeBuddy CLI 并回传其输出。

原理（一次性拉起 + 会话延续）:
  收到 task_request
    → 下载附件到工作目录
    → 构造协作提示词
    → 拉起 `codebuddy -p [--resume <sid>] "<prompt>" --output-format json -y`
    → 解析 stdout 中的 JSON（result / session_id）
    → reply_task 回传给发起方

用法:
  python executor/codebuddy_executor.py --agent-id codebuddy_pc1 --name "CodeBuddy@PC1"
  python executor/codebuddy_executor.py --mock          # 不调 CodeBuddy，模拟执行（联调用）
"""
import argparse
import json
import logging
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
log = logging.getLogger("codebuddy_executor")

PROMPT_TEMPLATE = """你正在参与一个跨机器多智能体协作系统，有任务需要你完成。

【任务发起方】{sender_id}
【任务指令】{instruction}
{context_block}{attachment_block}
协作规则:
1. 直接执行任务，不要询问。
2. 如需与其他智能体协作，可用 Bash 调用通信 CLI（工作目录: {workdir}）:
   - 查看在线智能体: python "{cli_path}" agents
   - 给其他智能体发任务: python "{cli_path}" send --to <agent_id> --text "任务指令" --wait 300
   - 上传文件给对方: python "{cli_path}" upload <文件路径>
3. 完成后，把最终结论直接写在你的最终回复里（会被自动回传给发起方）。回复不要包含与任务无关的内容。"""


def find_codebuddy() -> str:
    for name in ("codebuddy", "codebuddy.cmd", "codebuddy.exe"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError("找不到 codebuddy 可执行文件，请确认已安装并在 PATH 中")


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


def _assistant_texts(events) -> str:
    """从事件数组提取全部 assistant 输出文本（兑底）。"""
    parts = []
    for item in events if isinstance(events, list) else []:
        if isinstance(item, dict) and item.get("role") == "assistant":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    return "\n".join(p for p in parts if p)


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
            output = result_elem.get("result") or _assistant_texts(data)
            return output.strip(), session_id
        texts = _assistant_texts(data)
        if texts:
            return texts, session_id
    return (text or "").strip()[:4000], None


def build_prompt(req: dict, workdir: Path, cli_path: Path) -> str:
    payload = req.get("payload", {})
    ctx = payload.get("context_data")
    context_block = f"【附加上下文】{ctx}\n" if ctx else ""
    urls = payload.get("attachment_urls") or []
    attachment_block = "【附件】已在工作目录: 见上方说明\n" if urls else ""
    return PROMPT_TEMPLATE.format(
        sender_id=req.get("sender_id", "?"),
        instruction=payload.get("instruction", ""),
        context_block=context_block,
        attachment_block=attachment_block,
        workdir=workdir,
        cli_path=cli_path,
    )


class CodeBuddyExecutor:
    def __init__(self, args):
        self.args = args
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
        else:
            output, session_id, status, error = self._run_codebuddy(prompt, payload.get("session_id"), timeout, self.workdir)

        elapsed = round(time.time() - started, 2)
        log.info("任务 %s 完成 status=%s 耗时=%ss", task_id[:8], status, elapsed)

        # 3. 回传
        self.bus.reply_task(
            req, output_text=output, status=status, error=error,
            artifacts=[], session_id=session_id,
        )

    def _run_codebuddy(self, prompt: str, session_id, timeout: int, cwd: Path):
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

        data = extract_json(proc.stdout or "")
        output, session = parse_codebuddy_output(proc.stdout or "")
        if data is not None and output:
            if proc.returncode != 0:
                return (proc.stderr or output)[:4000], session, "error", f"exit_code={proc.returncode}"
            return output, session, "success", None
        # stdout 非 JSON：退回原始文本
        raw = (proc.stdout or proc.stderr or "").strip()
        return raw[:4000], None, ("success" if proc.returncode == 0 else "error"), \
               None if proc.returncode == 0 else f"exit_code={proc.returncode}"

    def _run_mock(self, req: dict, local_files):
        time.sleep(0.3)
        payload = req.get("payload", {})
        output = (f"[mock] 已收到任务: {payload.get('instruction', '')[:100]}\n"
                  f"附件 {len(local_files)} 个: {[p.name for p in local_files]}")
        return output, f"mock-session-{req.get('task_id', '')[:8]}", "success", None

    # ---------- 主循环 ----------

    def run(self):
        self.bus.connect(register=True)
        log.info("执行器就绪 mock=%s workdir=%s 等待任务...", self.args.mock, self.workdir)
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
    parser.add_argument("--workdir", default=str(ROOT_DIR / "data" / "executor_work"))
    parser.add_argument("--timeout", type=int, default=1800, help="单任务硬超时（秒）")
    parser.add_argument("--broker-host", default=None)
    parser.add_argument("--broker-port", type=int, default=None)
    parser.add_argument("--http-base", default=None)
    CodeBuddyExecutor(parser.parse_args()).run()


if __name__ == "__main__":
    main()
