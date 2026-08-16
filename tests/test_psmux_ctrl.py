# psmux control mode (-C) 验收脚本：验证 %output 增量事件流
import os
import subprocess
import sys
import time

TMUX = os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
    r"\marlocarlo.psmux_Microsoft.Winget.Source_8wekyb3d8bbwe\tmux.exe"
)
OUT_FILE = r"C:\tmp\ctrl_out.txt"


def main():
    out = open(OUT_FILE, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [TMUX, "-C", "attach-session", "-t", "t2"],
        stdin=subprocess.PIPE,
        stdout=out,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    time.sleep(1.5)
    # 通过 control mode 下发命令，触发 pane 输出
    proc.stdin.write('send-keys -t t2 "Write-Output ctrl-mode-ping-12345" Enter\n')
    proc.stdin.write('send-keys -t t2 "Write-Output 控制模式中文输出" Enter\n')
    proc.stdin.flush()
    time.sleep(4)
    proc.stdin.write("detach-client\n")
    proc.stdin.flush()
    time.sleep(1)
    proc.kill()
    out.close()

    content = open(OUT_FILE, encoding="utf-8", errors="replace").read()
    lines = content.splitlines()
    print(f"总行数: {len(lines)}")
    # 统计关键通知类型
    kinds = {}
    for ln in lines:
        if ln.startswith("%"):
            head = ln.split(" ", 1)[0]
            kinds[head] = kinds.get(head, 0) + 1
    print(f"通知类型统计: {kinds}")
    # 找 %output 事件
    outputs = [ln for ln in lines if ln.startswith("%output")]
    print(f"%output 事件数: {len(outputs)}")
    joined = "".join(outputs)
    for probe in ("ctrl-mode-ping-12345", "控制模式中文输出"):
        print(f"包含 {probe!r}: {probe in joined}")
    # 展示前几条 %output 原始样本
    for ln in outputs[:8]:
        print("SAMPLE:", ln[:160])


if __name__ == "__main__":
    sys.exit(main())
