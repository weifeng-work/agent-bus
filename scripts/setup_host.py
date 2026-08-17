"""主机侧一键引导：装 broker + 启动 bus_server + 打开面板（幂等可重入）。

一句提示词流程（主机）:
  python scripts/setup_host.py

做了什么:
  1. 确保依赖（paho-mqtt fastapi uvicorn requests，缺则 pip 装）
  2. broker 就绪:
     - Windows: 下载 mosquitto 便携 zip 解压到 data/runtime/mosquitto（零 UAC），
       以用户态进程启动（listener 1883 0.0.0.0 + allow_anonymous true，局域网可信边界）
     - Linux:   优先复用已装 mosquitto（apt/服务均可），无则提示一条 apt 命令
       （需一次 sudo；跑用户态实例时不占系统 1883、用 --broker-port 区分）
  3. 生成 broker 配置（匿名直连；历史版本曾发放节点凭据到 data/credentials.json，
     v2 匿名化后不再需要——add_node.py 仅作遗留管理）
  4. 启动 bus_server（data/server.pid），beacon 广播待队伍初始化后自动开始
  5. 打开浏览器面板 → 首次向导设定队伍名 → 全部完成

人工确认点: Windows 首次监听会弹防火墙确认（点"允许"）；Linux 装 mosquitto 需一次 sudo。
"""
import argparse
import json
import os
import platform as _platform
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_bus import provision  # noqa: E402

RUNTIME = ROOT / "data" / "runtime"
MOSQ_DIR = RUNTIME / "mosquitto"
# 便携 zip：官方 2024 年起不再发布（404），仅留作历史镜像命中时的快路径
MOSQUITTO_ZIPS = [
    "https://mosquitto.org/files/binary/win64/mosquitto-2.0.22-windows.zip",
    "https://mosquitto.org/files/binary/win64/mosquitto-2.0.21-windows.zip",
]
# 现行来源：NSIS 安装器 + 7z 解压（零 UAC 免安装提取）
MOSQUITTO_INSTALLERS = [
    "https://mosquitto.org/files/binary/win64/mosquitto-2.0.22-install-windows-x64.exe",
    "https://mosquitto.org/files/binary/win64/mosquitto-2.0.21-install-windows-x64.exe",
    "https://mosquitto.org/files/binary/win64/mosquitto-2.0.20-install-windows-x64.exe",
]
SEVEN_ZIP_URL = "https://www.7-zip.org/a/7zr.exe"          # 精简版，仅解 .7z
SEVEN_ZIP_FULL = "https://www.7-zip.org/a/7z2409-x64.exe"  # 完整版安装器（本身是 7z SFX）


def port_open(port: int, host="127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def step(n, msg):
    print(f"  [{n}] {msg}", flush=True)


def ensure_deps():
    import importlib
    deps = {  # import 名 -> pip 包名
        "paho.mqtt.client": "paho-mqtt", "fastapi": "fastapi",
        "uvicorn": "uvicorn", "requests": "requests",
    }
    missing = [pip for mod, pip in deps.items()
               if not _try_import(mod, importlib)]
    if missing:
        step(1, f"安装依赖: {', '.join(missing)}")
        r = subprocess.run([sys.executable, "-m", "pip", "install", *missing])
        if r.returncode != 0:
            # Debian 12+ PEP 668: 裸 pip install 报 externally-managed-environment
            # → --user --break-system-packages（装 ~/.local，与 apt 系统包共存，实测可行）
            step(1, "默认 pip 受 PEP 668 限制，改用 --user --break-system-packages")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user",
                 "--break-system-packages", *missing], check=True)


def _try_import(mod, importlib):
    try:
        importlib.import_module(mod)
        return True
    except ImportError:
        return False


def download_file(urls, dest: Path, quiet=False) -> Path:
    for u in urls:
        try:
            if not quiet:
                print(f"      下载 {u.rsplit('/', 1)[-1]} ...")
            urllib.request.urlretrieve(u, dest)
            return dest
        except Exception as e:
            if not quiet:
                print(f"      失败: {e}")
    raise RuntimeError(f"全部下载源失败，请手动下载解压到 {dest}")


def find_mosquitto_existing() -> str:
    """复用本机已安装的 mosquitto（PATH / Program Files / C:\\mosquitto）。"""
    import shutil
    exe = shutil.which("mosquitto")
    if exe:
        return exe
    for base in (r"C:\Program Files\mosquitto", r"C:\Program Files (x86)\mosquitto",
                 r"C:\mosquitto"):
        p = Path(base) / "mosquitto.exe"
        if p.exists():
            return str(p)
    return ""


def _ensure_7z_full(dl_dir: Path) -> str:
    """返回可解 NSIS 的完整 7z.exe：7zr 解 7-Zip 自身 SFX 安装器引导出完整版。"""
    full = dl_dir / "7zip" / "7z.exe"
    if full.exists():
        return str(full)
    sevenzr = dl_dir / "7zr.exe"
    if not sevenzr.exists():
        download_file([SEVEN_ZIP_URL], sevenzr)
    inst = dl_dir / "7z-setup.exe"
    if not inst.exists():
        download_file([SEVEN_ZIP_FULL], inst)
    r = subprocess.run([str(sevenzr), "x", str(inst), f"-o{dl_dir / '7zip'}", "-y"],
                       capture_output=True)
    if not full.exists():
        raise RuntimeError(f"7-Zip 引导解压失败: {r.stderr.decode(errors='replace')[:200]}")
    return str(full)


def extract_mosquitto_nsis(installer: Path, dest: Path):
    """从 NSIS 安装器提取便携 mosquitto（mosquitto.exe 同目录文件即运行所需）。"""
    dest.mkdir(parents=True, exist_ok=True)
    out = installer.parent / "mosq_extract"
    z7 = _ensure_7z_full(installer.parent)
    r = subprocess.run([z7, "x", str(installer), f"-o{out}", "-y"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"NSIS 解压失败: {r.stderr.decode(errors='replace')[:200]}")
    hits = list(out.rglob("mosquitto.exe"))
    if not hits:
        raise RuntimeError("解压产物中未找到 mosquitto.exe")
    src_dir = hits[0].parent
    for f in src_dir.iterdir():
        if f.is_file():
            f.replace(dest / f.name)
    shutil_rmtree(out)


def shutil_rmtree(p: Path):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


def ensure_broker_win(port: int) -> str:
    """Windows broker 就绪，返回 mosquitto.exe 路径。优先级：
    1. 本机已安装（PATH / Program Files）—— e2e/复装场景零下载
    2. data/runtime/mosquitto 便携目录已就位 —— 幂等复用
    3. 官方便携 zip（已停发，镜像命中时仍最快）
    4. NSIS 安装器 + 7z 免安装提取 —— 零 UAC 现行兜底
    """
    exe = find_mosquitto_existing()
    if exe:
        step(2, f"复用已安装 mosquitto: {exe}")
        return exe
    exe = MOSQ_DIR / "mosquitto.exe"
    if exe.exists():
        step(2, f"复用便携 mosquitto: {exe}")
        return str(exe)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    dl = RUNTIME / "_dl"
    dl.mkdir(exist_ok=True)

    step(2, "获取 mosquitto（便携 zip → NSIS 免安装提取，零 UAC）...")
    zip_path = dl / "mosquitto.zip"
    try:
        download_file(MOSQUITTO_ZIPS, zip_path, quiet=True)
    except RuntimeError:
        zip_path = None
    if zip_path is not None:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(RUNTIME)
        zip_path.unlink(missing_ok=True)
        if not exe.exists():  # zip 带版本子目录则归位
            for p in RUNTIME.rglob("mosquitto.exe"):
                if p.parent != MOSQ_DIR:
                    MOSQ_DIR.mkdir(parents=True, exist_ok=True)
                    for f in p.parent.iterdir():
                        f.replace(MOSQ_DIR / f.name)
                break
    if not exe.exists():
        inst = dl / "mosquitto-setup.exe"
        download_file(MOSQUITTO_INSTALLERS, inst)
        extract_mosquitto_nsis(inst, MOSQ_DIR)
    if not exe.exists():
        raise RuntimeError("mosquitto.exe 不存在（zip 与 NSIS 提取均失败）")
    return str(exe)


def ensure_broker_linux(port: int) -> str:
    """Linux: 复用系统 mosquitto（apt 装的即可，用户态跑第二个实例不冲突）。"""
    exe = shutil_which("mosquitto")
    if not exe:
        print("  [2] 未检测到 mosquitto，安装（需要一次 sudo）:")
        print("      sudo apt-get update && sudo apt-get install -y mosquitto")
        print("      安装后重跑本脚本（系统服务若已监听 1883，用 --broker-port 换端口跑用户态实例）")
        sys.exit(1)
    return exe


def shutil_which(name):
    import shutil
    exe = shutil.which(name)
    if exe:
        return exe
    # Debian: apt 装的 mosquitto 二进制在 /usr/sbin，普通用户 PATH 不含 sbin
    for cand in (f"/usr/sbin/{name}", f"/usr/local/sbin/{name}"):
        if Path(cand).exists():
            return cand
    return None


def write_broker_conf(port: int) -> Path:
    conf = RUNTIME / "mosquitto.conf"
    # 匿名直连：不启用 password_file/acl（凭据逻辑已移除，先跑通功能）
    conf.write_text(
        f"listener {port} 0.0.0.0\n"
        f"allow_anonymous true\n",
        encoding="utf-8",
    )
    return conf


def start_broker(exe: str, port: int):
    if port_open(port):
        step(3, f"端口 {port} 已有 broker 在监听（复用，可能为服务模式）")
        return
    conf = write_broker_conf(port)
    args = [exe, "-c", str(conf)]
    proc = subprocess.Popen(
        args, cwd=str(RUNTIME),
        stdout=open(RUNTIME / "broker.log", "ab"),
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=(0x08000000 if _platform.system() == "Windows" else 0),
    )
    (ROOT / "data" / "broker.pid").write_text(str(proc.pid))
    (ROOT / "data" / "broker.cmd.json").write_text(json.dumps({
        "args": args, "cwd": str(RUNTIME),
        "probe_host": "127.0.0.1", "probe_port": port,
    }), encoding="utf-8")
    for _ in range(20):
        if port_open(port):
            step(3, f"broker 已启动 pid={proc.pid} :{port}")
            return
        time.sleep(0.25)
    raise RuntimeError("broker 启动后端口未就绪，查看 data/runtime/broker.log")


def start_server(http_port: int, broker_port: int):
    if port_open(http_port):
        step(5, f"端口 {http_port} 已有 bus_server 在监听（复用）")
        return
    env = os.environ.copy()
    env.update({
        "BUS_BROKER_HOST": "127.0.0.1", "BUS_BROKER_PORT": str(broker_port),
        "BUS_HTTP_BASE": f"http://{provision.get_local_ip()}:{http_port}",
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "bus_server.py"),
         "--host", "0.0.0.0", "--port", str(http_port),
         "--broker-port", str(broker_port)],
        cwd=str(ROOT), env=env,
        stdout=open(ROOT / "data" / "server.log", "ab"),
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=(0x08000000 if _platform.system() == "Windows" else 0),
    )
    (ROOT / "data" / "server.pid").write_text(str(proc.pid))
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/api/health", timeout=1):
                step(5, f"bus_server 已启动 pid={proc.pid} http://0.0.0.0:{http_port}")
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("bus_server 启动失败，查看 data/server.log")


def main():
    ap = argparse.ArgumentParser(description="agent-bus 主机一键引导")
    ap.add_argument("--broker-port", type=int, default=1883)
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    print("== agent-bus 主机引导 ==")
    ensure_deps()

    is_win = _platform.system() == "Windows"
    exe = ensure_broker_win(args.broker_port) if is_win else ensure_broker_linux(args.broker_port)

    # 4. 启动 broker + bus_server（匿名直连，无凭据）
    start_broker(exe, args.broker_port)
    start_server(args.http_port, args.broker_port)

    panel = f"http://127.0.0.1:{args.http_port}/"
    print("\n== 完成 ==")
    print(f"  面板: {panel} （首次进入设定队伍名称即开始广播）")
    print(f"  子设备加入: 在目标机运行 python scripts/join_team.py（匿名，无需口令）")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(panel)
        # 部分环境 webbrowser 无效，附文本提示（上面已打印）


if __name__ == "__main__":
    main()
