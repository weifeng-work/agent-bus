"""通信节点（Comm Node）—— 兼容包装（Phase 1 重构版）。

本文件为兼容入口，保持旧 CLI 接口不变。
新部署由 core_node.py（Layer 1 底层控制通道）+ tray_app.py（Layer 2+ 纯 UI）替代。

行为：
  - 检测到 --headless 或 --role hub 时 → 直接委托 core_node.py
  - 其他情况（默认 worker GUI 模式）→ 提示用户迁移到 core_node + tray_app
  - 所有参数与 core_node.py 完全兼容

旧版 comm_node.py 中的 GUI 代码已剥离到 tray_app.py。
"""
import sys
import warnings
from pathlib import Path

warnings.warn(
    "comm_node.py 已重构为兼容包装。新部署请使用 core_node.py（Layer 1 无头服务）"
    " + tray_app.py（Layer 2+ 托盘 UI）。",
    DeprecationWarning,
    stacklevel=2,
)

# 直接委托给 core_node.py
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from executor.core_node import main  # noqa: E402

if __name__ == "__main__":
    main()