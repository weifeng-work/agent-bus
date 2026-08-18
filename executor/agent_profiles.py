"""Per-agent TUI 适配档案。

评审结论: REPL 类与 raw-TUI 类输入模型不同，完成锚点也不同，
必须按 agent 配 profile，不能一套正则打天下。
"""
import re
from dataclasses import dataclass, field


@dataclass
class AgentProfile:
    key: str
    launch_command: str                     # 在 pane shell 里敲的启动命令
    ready_pattern: str                      # 主界面就绪（可接收输入）
    ready_hint: str = None                  # 就绪附加条件: 屏幕须含此文本（状态栏特征）
    startup_dialogs: list = field(default_factory=list)  # [(正则, 键名)] 启动期对话框
    answer_bullet: str = r"^\s*●"           # 回复块行首标记（答案提取锚点）
    bullet_in_answer: bool = True           # bullet 行本身是否属于答案（codebuddy ● 是答案首行）
    input_line: str = r"^\s*>\s"            # 底部输入行特征
    separator_line: str = "─"               # 输入框分隔线字符集（区域锚定用）
    status_noise: tuple = ()                # 提取答案时剔除的状态行关键词
    stable_seconds: float = 10.0            # 静止判定窗口（quiescence）
    min_answer_wait: float = 3.0            # 注入后最少观察时间（防把回显当答案）

    def comp(self):
        """预编译正则。"""
        self._ready = re.compile(self.ready_pattern, re.MULTILINE)
        self._bullet = re.compile(self.answer_bullet, re.MULTILINE)
        self._input = re.compile(self.input_line, re.MULTILINE)
        self._dialogs = [(re.compile(p, re.MULTILINE), k) for p, k in self.startup_dialogs]
        return self

    def is_ready(self, screen_text: str) -> bool:
        if not self._ready.search(screen_text):
            return False
        if self.ready_hint and self.ready_hint not in screen_text:
            return False
        return True

    def input_box_region(self, screen_text: str):
        """返回 (start, end) 输入框区域行号区间。

        codebuddy/Ink 布局: 输入框夹在两条长分隔线之间（双线锚定）。
        opencode/BubbleTea 布局: 只有底部单条 `╹──` 下边界，输入框是其上方
        连续的 `┃` 行块（单线+连续行锚定）。transcript 里的用户消息回显
        同样带引导符，必须靠区域锚定区分"输入框"与"已提交回显"。
        """
        lines = screen_text.splitlines()
        sep_set = set(self.separator_line)
        sep_idxs = [
            i for i, ln in enumerate(lines)
            if len(ln.strip()) > 20 and set(ln.strip()) <= sep_set
        ]
        if len(sep_idxs) >= 2:
            return sep_idxs[-2] + 1, sep_idxs[-1]
        if len(sep_idxs) == 1:
            # 单下边界: 输入框 = 边界上方连续的 input_line 行块
            bottom = sep_idxs[0]
            top = bottom
            while top > 0 and self._input.match(lines[top - 1]):
                top -= 1
            if top < bottom:
                return top, bottom
        # 兜底: 最后一个输入行
        for i in range(len(lines) - 1, -1, -1):
            if self._input.match(lines[i]):
                return i, i + 1
        return len(lines), len(lines)

    def match_dialog(self, screen_text: str):
        for pat, key in self._dialogs:
            if pat.search(screen_text):
                return key
        return None

    def bullet_lines(self, screen_text: str):
        """答案区内的回复块偏移列表（锚定增量: 注入后新出现即视为有产出）。

        只在输入框区域之上计数——opencode 的输入框行与回显行同符号（┃），
        不限定区域会把输入框行误计为回复块。
        """
        input_idx, _ = self.input_box_region(screen_text)
        region = "\n".join(screen_text.splitlines()[:input_idx])
        return [m.start() for m in self._bullet.finditer(region)]


PROFILES = {
    # CodeBuddy Code TUI (Ink)：
    # - 主界面底部有独立输入行 ">"，状态栏 "⏵⏵ accept edits on ... ← for agents"
    # - 首次进新目录弹 trust 对话框，默认选项即 Trust folder only，回车确认
    # - 回复以 "●" 开头成块出现在输入行上方
    "codebuddy": AgentProfile(
        key="codebuddy",
        # `--permission-mode acceptEdits`：TUI 进目录/编辑文件免二次授权确认，
        # 避免交互式执行器在授权请求处卡住；acceptEdits 仍保留模型有权限范围。
        launch_command="codebuddy --permission-mode acceptEdits",
        ready_pattern=r"^\s*>",           # 输入行（ghost 占位提示也匹配）
        ready_hint="⏵⏵",                 # 状态栏出现才算主界面就绪
        startup_dialogs=[
            (r"Do you trust the files in this folder\?", "Enter"),
        ],
        answer_bullet=r"^\s*●",
        input_line=r"^\s*>\s",
        status_noise=("⏵⏵",),
        stable_seconds=10.0,
    ),
    # OpenCode TUI (Bubble Tea, 全屏布局)——已实测校准 (v1.18.18):
    # - 主界面就绪: 输入框 ghost "Ask anything..." + 底部 "tab agents" 快捷键行
    # - 布局: 消息流在上，底部 `┃` 行输入框 + `╹──` 下边界（无 `>` 前缀）
    # - 用户回显行: `  ┃  文本`（答案块起点锚，与输入框行同符号，靠区域锚定区分）
    # - 助手答案无 ● bullet，正文直接跟在 `Thought:` 折叠块之后
    # - 生成心跳: `▣ Build · model · Ns` 耗时行每秒刷新（静止判定的活性信号）
    # - 右侧 "Getting started" 弹窗混在答案区，靠 status_noise 剔除
    "opencode": AgentProfile(
        key="opencode",
        # `--auto`：自动批准未被显式拒绝的权限（与 codebuddy acceptEdits 等价），
        # 避免交互式执行器卡在权限确认请求。
        launch_command="opencode --auto",
        ready_pattern=r"Ask anything",
        ready_hint="tab agents",
        startup_dialogs=[
            (r"Connect provider", "Enter"),  # 首次需配置 provider 的连接提示
        ],
        answer_bullet=r"^\s{0,4}┃[ \t]+\S",  # 用户消息回显行（新答案块的起点锚；[ \t] 禁跨行匹配）
        bullet_in_answer=False,             # 回显是用户的话，不属于答案内容
        input_line=r"^\s{0,4}┃",            # 任何 ┃ 行（输入框块锚定 + clean 截停用）
        separator_line="╹─▀",               # 字符集语义: ╹▀▀▀ 下边界线
        status_noise=("┃", "▣", "Thought", "Context", "tokens", "spent",
                      "Getting started", "Connect provider", "OpenCode includes",
                      "LSP", "providers to", "start immediately", "including",
                      "Claude, GPT", "so you can"),
        stable_seconds=12.0,
    ),
}


def get_profile(key: str) -> AgentProfile:
    prof = PROFILES.get(key)
    if not prof:
        raise KeyError(f"未知 agent profile: {key!r}，可选: {list(PROFILES)}")
    return prof.comp()


def extract_answer(screen_text: str, profile: AgentProfile,
                   sent_echo: str = None) -> str:
    """从重建屏幕提取最终答案。

    策略: 取最后一个回复块（bullet 起始）到输入框区域上界之间的连续内容；
    bullet_in_answer=False 时跳过 bullet 行本身（opencode 的 bullet 是用户
    回显行，不是答案）。找不到 bullet 则退化为屏幕尾部非空行。剔除状态噪音行。
    """
    lines = screen_text.splitlines()
    # 定位输入框区域上界（答案在其上方，且不受输入框 ghost 文本干扰）
    input_idx, _ = profile.input_box_region(screen_text)

    bullet_idx = None
    for m in profile._bullet.finditer(screen_text):
        pos = m.start()
        line_no = screen_text.count("\n", 0, pos)
        if line_no < input_idx:
            bullet_idx = line_no

    def clean(chunk_lines):
        # 容忍段内空行（长答案多段落，评审坑: 首空行截断）；遇输入行停
        sep_set = set(profile.separator_line)
        out = []
        for ln in chunk_lines:
            s = ln.strip()
            if not s:
                if out and all(not x.strip() for x in out[-1:]):
                    continue
                out.append(s)
                continue
            # 纯分隔线行（字符集 ⊆ separator_line）不是答案内容
            if len(s) > 10 and set(s) <= sep_set:
                continue
            if any(noise in ln for noise in profile.status_noise):
                continue
            if profile._input.match(ln):
                break
            out.append(s)
        return "\n".join(out).strip()

    if bullet_idx is not None:
        start = bullet_idx if profile.bullet_in_answer else bullet_idx + 1
        ans = clean(lines[start:input_idx])
        if ans:
            return ans

    # 兜底: 输入框之上的尾部非空行
    tail = [ln for ln in lines[:input_idx] if ln.strip()][-8:]
    return "\n".join(
        ln.strip() for ln in tail
        if not any(noise in ln for noise in profile.status_noise)
    )
