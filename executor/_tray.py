"""托盘 UI（pystray + pillow）—— 通信节点的交互面（架构 §4.3）。

懒加载：仅 GUI 模式由 comm_node.py 导入（--headless 不依赖 pystray/pillow）。
安装：pip install pystray pillow

菜单：
  受控开关（勾选，= 熔断）        → 关闭即 kill 执行器 + 灯灰
  shell 受控能力（M3 启用）
  自修复 / 查看错误日志 / 查看受控记录(M3) / 一键收集诊断包
  退出（watchdog 分钟级拉回）
"""
import os

import pystray
from PIL import Image, ImageDraw

from executor.comm_node import STATUS_COLORS


def _lamp_image(rgb, size=32):
    """生成 RGBA 圆点图标。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, size - 2, size - 2], fill=rgb + (255,))
    return img


class TrayGui:
    def __init__(self, node):
        self.node = node
        self.icon = None
        self._images = {name: _lamp_image(rgb) for name, rgb in STATUS_COLORS.items()}

    # ---------------- 菜单 ----------------

    def _menu(self):
        n = self.node
        if n.role == "hub":
            # 主控 hub 托盘：连接/受控状态人眼前可见（架构 §4.3）
            return pystray.Menu(
                pystray.MenuItem(f"Agent Bus [hub] {n.agent_id}", None, enabled=False),
                pystray.MenuItem("查看控制面板", self._open_panel),
                pystray.MenuItem("远程 Shell 用法：--shell-exec --target <node> --cmd <命令>",
                                 None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit),
            )
        return pystray.Menu(
            pystray.MenuItem(f"Agent Bus [{n.role}] {n.agent_id}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "受控开关（关闭=紧急停止）", self._toggle_controlled,
                checked=lambda item: n.controlled),
            pystray.MenuItem(
                "shell 受控能力（开启后免二次确认）", self._toggle_shell_control,
                checked=lambda item: n.shell_control),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("自修复", self._self_heal),
            pystray.MenuItem("查看错误日志", self._open_logs),
            pystray.MenuItem("查看受控记录", self._open_control_log),
            pystray.MenuItem("一键收集诊断包", self._diagnostics),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出（watchdog ≤1min 拉回）", self._quit),
        )

    def _toggle_controlled(self, icon, item):
        self.node.set_controlled(not self.node.controlled)
        self.update_menu()

    def _open_panel(self, icon, item):
        """打开主控面板（hub 用）。"""
        import webbrowser
        webbrowser.open(f"{self.node.http_base}/")

    def _quit(self, icon, item):
        self.node._stop.set()
        icon.stop()

    def _toggle_shell_control(self, icon, item):
        self.node.shell_control = not self.node.shell_control
        self.node._save_control_cfg()
        self.node.log.info("shell_control -> %s", self.node.shell_control)
        self.update_menu()

    def _self_heal(self, icon, item):
        threading = __import__("threading")
        threading.Thread(target=self.node.self_heal, daemon=True).start()

    def _open_logs(self, icon, item):
        logs = [
            self.node.data_dir / f"{self.node.agent_id}.log.err",
            self.node.data_dir / f"{self.node.agent_id}.log",
            self.node.data_dir / "tray_shell.log.err",
        ]
        target = next((p for p in logs if p and p.exists()), self.node.data_dir)
        try:
            os.startfile(str(target))  # noqa: S606  Windows 打开文件/目录
        except Exception as e:
            self.node.log.warning("打开日志失败: %s", e)

    def _open_control_log(self, icon, item):
        p = self.node.control_log
        if p and p.exists():
            os.startfile(str(p))

    def _diagnostics(self, icon, item):
        threading = __import__("threading")
        threading.Thread(target=self.node.collect_diagnostics, daemon=True).start()

    def _quit(self, icon, item):
        self.node.set_controlled(False)
        self.node._stop.set()
        icon.stop()

    # ---------------- 运行时 ----------------

    def run(self):
        """主线程消息循环（阻塞）；监督/心跳线程已在 comm_node.run 启动。"""
        self.icon = pystray.Icon(
            "agent-bus-comm-node",
            self._images.get(self.node.lamp, self._images["gray"]),
            f"Agent Bus {self.node.role}",
            self._menu(),
        )
        self.update_lamp(self.node.lamp)
        self.icon.run()

    def update_lamp(self, lamp):
        if self.icon:
            try:
                self.icon.icon = self._images.get(lamp, self._images["gray"])
            except Exception:
                pass

    def update_menu(self):
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def notify(self, title: str, message: str):
        if self.icon:
            try:
                self.icon.notify(message, title)
            except Exception:
                pass

    def open_folder(self, path):
        try:
            os.startfile(str(path))  # noqa: S606
        except Exception:
            pass
