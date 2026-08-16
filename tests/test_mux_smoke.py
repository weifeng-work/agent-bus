"""MuxTransport + ControlClient + ScreenTracker 组件冒烟测试（需本机 tmux/psmux）。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".deps"))
sys.path.insert(0, str(ROOT / "executor"))

from mux_transport import MuxTransport, ControlClient
from vt_screen import ScreenTracker, decode_tmux_escape

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


# 1. 转义解码单元测试
def test_escape():
    # "文" 的 UTF-8: e6 96 87
    out = decode_tmux_escape(r"\346\226\207")
    check("octal 转义解码", out == "文".encode("utf-8"), f"got={out!r}")
    # a + 反斜杠 + b + CR + LF
    out2 = decode_tmux_escape(r"a\\b\015\012")
    expect = b"a" + b"\x5c" + b"b" + b"\x0d" + b"\x0a"
    check("反斜杠+CRLF 解码", out2 == expect, f"got={out2!r}")


# 2. 滚动累积器（长回复防截断核心）: 纯进程内，不依赖 psmux
def test_scrollback():
    tr = ScreenTracker(cols=80, rows=10)

    # 2.1 前半段: L001..L030
    for i in range(1, 31):
        tr.feed(f"L{i:03d}\r\n".encode())
    visible = tr.text()
    check("屏幕只剩尾部(L030可见)", "L030" in visible, "")
    check("L001 已滚出屏幕", "L001" not in visible, "")

    # 2.2 锚点后继续: L031..L060（远超 10 行屏幕）
    mark = tr.mark()
    for i in range(31, 61):
        tr.feed(f"L{i:03d}\r\n".encode())

    full_since = tr.text_since(mark)
    check("锚点文本含新首行L031", "L031" in full_since, "")
    check("锚点文本含末行L060", "L060" in full_since, "")
    check("锚点文本不含锚点前L001", "L001" not in full_since, "")
    # 锚点语义: 只切"已滚出"的历史；锚点时仍在屏幕上的行（L021..L030）
    # 后续滚出时计入——所以 L020（锚点前最后滚出行）是排除边界
    check("锚点文本不含锚点前已滚出的L020", "L020" not in full_since, "")

    full_all = tr.text_since(0)
    check("全量文本头(L001)回捞成功", full_all.splitlines()[0].strip() == "L001",
          f"head={full_all.splitlines()[0]!r}")
    check("全量文本尾(L060)完整", full_all.rstrip().endswith("L060"), "")
    check("全量行数=滚出+屏幕", len([l for l in full_all.splitlines()]) >= 60, "")

    # 2.3 tail 上限（防超大回复撑爆 payload; 尾部空行不计入需求数）
    tailed = tr.text_since(0, tail=5)
    check("tail 截断只留尾部", len(tailed.splitlines()) <= 5
          and tailed.rstrip().endswith("L060") and "L001" not in tailed,
          f"lines={len(tailed.splitlines())}")

    # 2.4 内存上限: 超过 max_scrollback 丢弃最旧
    tr_small = ScreenTracker(cols=80, rows=5, max_scrollback=20)
    for i in range(1, 101):
        tr_small.feed(f"X{i:03d}\r\n".encode())
    sb = tr_small.text_since(0)
    check("滚动累积上限生效(丢最旧)", "X001" not in sb and "X100" in sb, "")

    # 2.5 宽字符 stub 不崩（Bubble Tea spinner 实测坑: pyte display 的 IndexError）
    tr_w = ScreenTracker(cols=40, rows=5)
    try:
        tr_w.feed("■⬝中文OK\r\n".encode())
        t = tr_w.text()
        check("宽字符 stub 渲染不崩", "中文OK" in t, f"text={t!r}")
    except IndexError as e:
        check("宽字符 stub 渲染不崩", False, f"IndexError: {e}")


# 3. 传输层全链路
def test_transport():
    mux = MuxTransport()
    sess = "agentbus_smoke"
    mux.kill_session(sess)
    ok = mux.start_session(sess)
    check("创建会话", ok)
    check("server 健康检查", mux.server_alive())

    ctrl = ControlClient(mux, sess, cols=mux.cols, rows=mux.rows)
    ctrl.bind_pane()
    # 等 shell 真正就绪（pwsh 首启要加载模块，固定 sleep 不可靠）
    shell_deadline = time.time() + 25
    while time.time() < shell_deadline:
        t = ctrl.tracker.text()
        if ">" in t and t.strip():
            break
        time.sleep(0.5)
    time.sleep(1.0)

    ctrl.text("Write-Output '重建测试-中文OK-12345'")
    ctrl.keys("Enter")
    time.sleep(4.0)

    text = ctrl.tracker.text()
    check("屏幕重建含注入命令", "重建测试-中文OK-12345" in text)
    check("屏幕重建含执行结果", any("重建测试-中文OK-12345" in ln for ln in text.splitlines()[1:]))

    h1 = ctrl.tracker.content_hash()
    time.sleep(1.5)
    h2 = ctrl.tracker.content_hash()
    check("静止屏幕 hash 稳定", h1 == h2, f"{h1} vs {h2}")

    # 对账: capture-pane 与重建一致
    cap = mux.capture_pane(sess)
    check("capture-pane 对账一致", "重建测试-中文OK-12345" in cap)

    # paste-buffer 路径
    ok2 = mux.inject_text(sess, "echo PASTE-OK-9988")
    ctrl.keys("Enter")
    time.sleep(3.0)
    check("paste-buffer 注入", ok2 and "PASTE-OK-9988" in ctrl.tracker.text())

    ctrl.close()
    pid = mux.pane_pid(sess)
    mux.kill_session(sess)
    if pid:
        mux.kill_process_tree(pid)
    check("会话清理", not mux.has_session(sess))


if __name__ == "__main__":
    test_escape()
    test_scrollback()
    test_transport()
    print(f"\n{'='*40}\n通过 {len(passed)} / 总 {len(passed)+len(failed)}")
    if failed:
        print("失败项:", failed)
        sys.exit(1)
