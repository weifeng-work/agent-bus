"""VT 屏幕重建：把 tmux/psmux control mode 的 %output 原始 VT 流重建为逻辑屏幕。

设计要点（来自评审共识）:
- 原始 VT 流不能直接当 transcript：PSReadLine 光标重绘、spinner 局部刷新会污染文本
- 用 pyte 做真实终端仿真，重绘自动归并为最终状态
- 完成检测看"重建后内容 hash 是否稳定"，而非"有无新字节"
- 长回复防截断: pyte 屏幕固定 rows 行，滚出即丢 → HistoryScreen 的滚动事件
  + 每次.feed 后立即 drain 成纯文本累积（防 deque 溢出丢行/Char 对象驻留），
  任务起点 mark() 打锚点，提取答案用 text_since(mark) 取完整内容
- 依赖: pip install pyte
"""
import hashlib
import threading


class ScreenTracker:
    """线程安全的逻辑屏幕跟踪器 + 滚动累积器。

    用法:
        tracker = ScreenTracker(cols=160, rows=50)
        tracker.feed(b"\\x1b[2Jhello")     # 喂入 %output 解码后的原始 VT 字节
        text = tracker.text()              # 当前屏幕文本（每行去尾部空格）
        m = tracker.mark()                 # 任务起点水位锚点
        full = tracker.text_since(m)       # 锚点后的完整文本（含滚出屏幕的部分）
        h = tracker.content_hash()         # 内容指纹（quiescence 检测用）
    """

    # pyte HistoryScreen 内部 history deque 的 maxlen: 必须 > 单次 feed 最大滚动行数
    # （%output 单块典型 ≤ 数 KB → ≤ 百行级），否则溢出静默丢行
    _PYTE_HISTORY = 2000

    def __init__(self, cols: int = 160, rows: int = 50, max_scrollback: int = 5000):
        import pyte  # 延迟导入，非交互执行器无需此依赖

        self.cols = cols
        self.rows = rows
        self.max_scrollback = max_scrollback
        self.screen = pyte.HistoryScreen(cols, rows, history=self._PYTE_HISTORY)
        self.stream = pyte.ByteStream(self.screen)
        self._lock = threading.Lock()
        self.bytes_fed = 0
        self._scrollback = []  # 已滚出屏幕顶的行（纯文本，上限 max_scrollback）

    def feed(self, data: bytes):
        """喂入一段原始 VT 字节流（来自 %output 事件解码结果）。"""
        if not data:
            return
        with self._lock:
            self.stream.feed(data)
            self._drain_locked()
            self.bytes_fed += len(data)

    def _drain_locked(self):
        """把 history.top 中滚出的行转为纯文本（调用方须持锁）。

        及时 drain 的两个理由:
        - history deque 有 maxlen，积压超过即静默丢最旧行
        - Char 对象（~100B/字符）转 str（~160B/行）后释放，长会话内存可控
        """
        top = self.screen.history.top
        while top:
            line = top.popleft()
            self._scrollback.append(self._line_from_buffer(line))
        overflow = len(self._scrollback) - self.max_scrollback
        if overflow > 0:
            del self._scrollback[:overflow]

    def _line_from_buffer(self, line_dict) -> str:
        """{列号: Char} 稀疏行 → 纯文本（缺列补空格，去尾空格）。

        绕过 pyte 0.8.2 `display` 属性: 其 render() 对宽字符 stub 槽
        （data=""）取 char[0] 会 IndexError（Bubble Tea spinner 实测崩）。
        stub 渲染为空串（宽字符自身已占两列），缺列补空格。
        """
        cells = []
        for i in range(self.cols):
            ch = line_dict.get(i)
            if ch is None:
                cells.append(" ")
            else:
                cells.append(ch.data)  # stub(data="") → 空，保持宽字符粘连
        return "".join(cells).rstrip()

    def _screen_lines_locked(self):
        """当前屏幕各行文本（调用方须持锁；自渲染防 pyte display 崩溃）。"""
        return [self._line_from_buffer(self.screen.buffer[y])
                for y in range(self.rows)]

    def reset(self):
        """清屏重建（会话重开 / %begin 全量 dump 前调用）。滚动累积一并作废。"""
        with self._lock:
            self.screen.reset()
            self._scrollback.clear()
            self.bytes_fed = 0

    def mark(self) -> int:
        """当前滚动累积水位（任务起点锚点，配合 text_since 用）。"""
        with self._lock:
            self._drain_locked()
            return len(self._scrollback)

    def text_since(self, mark: int, tail: int = None) -> str:
        """锚点之后的完整文本 = 滚动累积[mark:] + 当前屏幕。

        长回复防截断的主通道: 即使回复远超 rows 行滚出屏幕，仍完整可取。
        tail 限制返回行数上限（防超大回复撑爆总线 payload）。
        """
        with self._lock:
            self._drain_locked()
            lines = self._scrollback[mark:] + self._screen_lines_locked()
        if tail is not None and len(lines) > tail:
            lines = lines[-tail:]
        return "\n".join(lines)

    def text(self) -> str:
        """当前屏幕文本：每行 rstrip 尾部空格，保留行结构。"""
        with self._lock:
            return "\n".join(self._screen_lines_locked())

    def tail(self, n: int = 25) -> str:
        """屏幕最后 n 个非空行（进度直播用，控制 payload 体积）。"""
        lines = [ln for ln in self.text().splitlines() if ln.strip()]
        return "\n".join(lines[-n:])

    def content_hash(self) -> str:
        """重建内容的指纹；采样点间 hash 不变 = 屏幕静止。"""
        return hashlib.sha256(self.text().encode("utf-8")).hexdigest()[:16]

    @property
    def cursor(self):
        """光标位置 (x, y)，供 prompt 锚定检测用。"""
        with self._lock:
            return self.screen.cursor.x, self.screen.cursor.y


def decode_tmux_escape(data: str) -> bytes:
    """解码 tmux control mode 的行内转义。

    协议: '\\\\' -> 反斜杠字节；'\\ooo'（定宽3位八进制）-> 原始字节。
    其余字符按字面 UTF-8 编码。数据会跨包拆分，调用方须按字节流连续喂。
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c == "\\" and i + 1 < n:
            nxt = data[i + 1]
            if nxt == "\\":
                out.append(0x5C)
                i += 2
                continue
            oct_s = data[i + 1:i + 4]
            if len(oct_s) == 3 and all(ch in "01234567" for ch in oct_s):
                out.append(int(oct_s, 8))
                i += 4
                continue
        out += c.encode("utf-8")
        i += 1
    return bytes(out)
