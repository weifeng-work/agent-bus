"""托盘前端 UI（Tray App）—— Layer 2+ 纯可视化前端。

纯粹的用户态可视化前端，不包含任何业务和控制逻辑。
- 在用户登录桌面时启动（计划任务 onlogon 或开始菜单快捷方式）
- 读取 state.json 和运行时信息来显示当前系统的受控状态（绿/黄/灰灯）
- 「彻底退出」按钮：向 state.json 写入 disabled → 退出自身
- 「启用」按钮：向 state.json 写入 active（D7：打开托盘默认不自动激活）
- 轮询 control.log 新条目弹通知气泡（D8）
- 受控中状态常驻可见：图标叠加锁标记（§6.3 强制要求）

依赖：pystray + pillow（pip install pystray pillow）
"""
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import state_machine  # noqa: E402

log = logging.getLogger("tray_app")

# 状态灯颜色
STATUS_COLORS = {"green": (46, 204, 113), "yellow": (241, 196, 15), "gray": (127, 140, 141)}

# 状态文件新鲜阈值（与 core_node 保持一致）
STATUS_FRESH_SECONDS = 60.0
# 心跳文件判活阈值
HB_FRESH_SECONDS = 150.0
# 轮询间隔
POLL_INTERVAL = 2.0
# control.log 轮询间隔
CONTROL_LOG_POLL_INTERVAL = 3.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 托盘 UI
# ---------------------------------------------------------------------------


class TrayApp:
    def __init__(self, install_dir: str, role: str = "worker", agent_id: str = ""):
        self.install_dir = Path(install_dir).resolve()
        self.role = role
        self.agent_id = agent_id
        self.runtime_dir = self.install_dir / "data" / "runtime"
        self.data_dir = self.install_dir / "data"

        # 运行时文件路径
        self.status_file = self.runtime_dir / "executor_status.json"
        self.hb_file = self.runtime_dir / "tray_heartbeat.ts"
        self.control_log = self.data_dir / "control.log"

        # 状态
        self.lamp = "gray"
        self._stop = threading.Event()

        # control.log 轮询
        self._last_log_pos = 0
        if self.control_log.exists():
            self._last_log_pos = self.control_log.stat().st_size

        # 图标
        self._icon = None
        self._images = {}

        # 初始化 GUI
        self._init_gui()

    def _init_gui(self):
        try:
            import pystray  # noqa: F401
            from PIL import Image, ImageDraw
        except ImportError:
            log.error("托盘 UI 需要 pystray + pillow：pip install pystray pillow")
            raise SystemExit(2)
        # 生成图标（含受控角标）：每个灯色有两种版本——受控中（带锁标记）和普通
        for name, rgb in STATUS_COLORS.items():
            # 普通版
            img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse([2, 2, 30, 30], fill=rgb + (255,))
            self._images[name] = img
            # 受控版：右下角叠加小锁标记（§6.3 受控状态常驻可见）
            ctrl_img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            dc = ImageDraw.Draw(ctrl_img)
            dc.ellipse([2, 2, 30, 30], fill=rgb + (255,))
            # 锁标记：小矩形 + 弧（左下角白色）
            dc.rectangle([18, 24, 28, 30], fill=(255, 255, 255, 220))
            dc.arc([20, 18, 26, 26], 180, 360, fill=(255, 255, 255, 220), width=2)
            self._images[f"{name}_ctrl"] = ctrl_img

    # ---------------- 受控状态判定 ----------------

    def _is_controlled(self) -> bool:
        """检查是否处于受控中状态（受控开关开 + shell_control 开）。"""
        ctrl = load_json(self.runtime_dir / "controlled.json", {"on": True})
        cfg = load_json(self.runtime_dir / "control_config.json", {"shell_control": False})
        return bool(ctrl.get("on", True)) and bool(cfg.get("shell_control", False))

    # ---------------- 状态计算 ----------------

    def _compute_lamp(self) -> str:
        """根据运行时文件计算灯色（绿/黄/灰）。

        绿 = 状态文件新鲜 + status=connected + 心跳文件新鲜
        黄 = 状态文件或心跳文件过期（进程在但失联）
        灰 = 状态文件缺失或进程已停
        返回的字符串可能带 _ctrl 后缀（受控中时使用受控版图标）
        """
        # 读状态文件
        st = load_json(self.status_file, {"status": "unknown", "ts": 0.0})
        status = st.get("status", "unknown")
        status_ts = float(st.get("ts", 0))
        status_fresh = (time.time() - status_ts) < STATUS_FRESH_SECONDS

        # 读心跳文件
        try:
            hb_ts = float(self.hb_file.read_text(encoding="utf-8").strip())
            hb_fresh = (time.time() - hb_ts) < HB_FRESH_SECONDS
        except Exception:
            hb_fresh = False

        # 读 state.json
        sv = state_machine.read_state(str(self.install_dir))
        if sv == state_machine.STATE_DISABLED:
            return "gray"

        # 基于灯色
        if status == "connected" and status_fresh and hb_fresh:
            base = "green"
        elif hb_fresh:
            base = "yellow"
        else:
            base = "gray"

        # 受控中时使用带锁标记的图标（§6.3：受控状态常驻可见）
        if self._is_controlled() and base in ("green", "yellow"):
            return f"{base}_ctrl"
        return base

    # ---------------- 菜单 ----------------

    def _menu(self):
        import pystray
        sv = state_machine.read_state(str(self.install_dir))
        is_active = sv == state_machine.STATE_ACTIVE
        is_controlled = self._is_controlled()

        # 受控角标文案
        ctrl_badge = "🔒 受控中" if is_controlled else " 未受控"

        items = [
            pystray.MenuItem(
                f"Agent Bus [{self.role}] {self.agent_id}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                f"状态: {'运行中' if is_active else '已停用'} | {ctrl_badge}",
                None, enabled=False),
            pystray.MenuItem(
                "启用" if not is_active else "✓ 已启用",
                self._enable_service if not is_active else None,
                enabled=not is_active),
            pystray.MenuItem(
                "彻底退出并停用服务" if is_active else "已停用",
                self._disable_service if is_active else None,
                enabled=is_active),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("查看受控记录", self._open_control_log),
            pystray.MenuItem("查看错误日志", self._open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出（仅关闭托盘）", self._quit),
        ]
        return pystray.Menu(*items)

    def _enable_service(self, icon, item):
        """启用：写 active 到 state.json。"""
        ok = state_machine.write_state(str(self.install_dir), state_machine.STATE_ACTIVE)
        if ok:
            log.info("已启用服务（state.json -> active）")
            self._update()
        else:
            log.error("启用失败：无法写入 state.json")

    def _disable_service(self, icon, item):
        """彻底退出：写 disabled 到 state.json → 退出自身。

        服务层的 Watchdog 发现状态改变后，会接管并关闭底层的 core_node 和业务进程。
        """
        ok = state_machine.write_state(str(self.install_dir), state_machine.STATE_DISABLED)
        if ok:
            log.info("已停用服务（state.json -> disabled），托盘退出")
            self._quit(icon, item)
        else:
            log.error("停用失败：无法写入 state.json")

    def _open_control_log(self, icon, item):
        p = self.control_log
        if p and p.exists():
            try:
                os.startfile(str(p))
            except Exception as e:
                log.warning("打开受控记录失败: %s", e)

    def _open_logs(self, icon, item):
        logs = [
            self.data_dir / f"{self.agent_id}.log.err",
            self.data_dir / f"{self.agent_id}.log",
            self.data_dir / "tray_shell.log.err",
        ]
        target = next((p for p in logs if p and p.exists()), self.data_dir)
        try:
            os.startfile(str(target))
        except Exception as e:
            log.warning("打开日志失败: %s", e)

    def _quit(self, icon, item):
        self._stop.set()
        if icon:
            icon.stop()

    # ---------------- control.log 轮询（D8） ----------------

    def _poll_control_log(self):
        """轮询 control.log 新条目，弹通知气泡（D8）。

        尝试读取默认路径和 fallback 路径（core_node 回退用）。
        """
        import tempfile
        fallback_log = Path(tempfile.gettempdir()) / "agent-bus-control.log"

        def _poll_one(path: Path, last_pos: int) -> int:
            """轮询单个文件，返回最新位置。"""
            if not path.exists():
                return last_pos
            current_size = path.stat().st_size
            if current_size > last_pos:
                with open(path, "r", encoding="utf-8") as f:
                    f.seek(last_pos)
                    new_lines = f.readlines()
                last_pos = current_size
                for line in new_lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        cmd = entry.get("cmd", "")[:80]
                        sender = entry.get("sender", "?")
                        self._notify("受控操作",
                                     f"主控 {sender} 执行: {cmd}")
                    except Exception:
                        pass
            return last_pos

        while not self._stop.is_set():
            try:
                self._last_log_pos = _poll_one(self.control_log, self._last_log_pos)
                # 也轮询 fallback 路径（SYSTEM 服务可能写临时目录）
                _poll_one(fallback_log, 0)
            except Exception:
                pass
            self._stop.wait(CONTROL_LOG_POLL_INTERVAL)

    # ---------------- UI 更新 ----------------

    def _update(self):
        """刷新灯色和菜单。"""
        lamp = self._compute_lamp()
        if lamp != self.lamp:
            self.lamp = lamp
            log.info("[灯] %s（受控: %s）", lamp.upper(), self._is_controlled())
        if self._icon:
            try:
                self._icon.icon = self._images.get(lamp, self._images["gray"])
                self._icon.update_menu()
            except Exception:
                pass

    def _notify(self, title: str, message: str):
        if self._icon:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass

    # ---------------- 主循环 ----------------

    def run(self):
        import pystray

        # 创建图标
        lamp = self._compute_lamp()
        self._icon = pystray.Icon(
            "agent-bus-tray",
            self._images.get(lamp, self._images["gray"]),
            f"Agent Bus {self.role}",
            self._menu(),
        )

        # 启动 control.log 轮询线程
        poll_thread = threading.Thread(target=self._poll_control_log, daemon=True)
        poll_thread.start()

        # 启动 UI 更新循环
        def update_loop():
            while not self._stop.is_set():
                self._update()
                self._stop.wait(POLL_INTERVAL)

        update_thread = threading.Thread(target=update_loop, daemon=True)
        update_thread.start()

        # 主线程消息循环
        self._icon.run()

        log.info("托盘 UI 已退出")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Agent Bus 托盘前端 UI（纯可视化前端）")
    ap.add_argument("--install-dir", default=str(ROOT_DIR),
                    help="agent-bus 安装目录（默认项目根目录）")
    ap.add_argument("--role", choices=("worker", "hub"), default="worker",
                    help="节点角色（用于显示）")
    ap.add_argument("--agent-id", default="",
                    help="节点身份（用于显示，默认为空）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    tray = TrayApp(install_dir=args.install_dir, role=args.role, agent_id=args.agent_id)
    try:
        tray.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()