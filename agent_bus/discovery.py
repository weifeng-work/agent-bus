"""局域网队伍发现协议（UDP 广播 beacon）。

主机侧 BeaconBroadcaster：队伍初始化后每 3 秒向 255.255.255.255:41830 广播：
  {"proto": "agent-bus", "ver": 1, "team_id", "team_name", "host_name",
   "mqtt_port", "http_port"}

子设备侧 scan_teams()：绑定 41830 收集 timeout 秒内的 beacon，按 team_id 去重。

安全：beacon 不含任何凭据；入队即匿名登记于 HTTP /api/join（v2 匿名化后无口令）。
选广播而非组播：零配置（组播需 join group，跨 AP/网段行为更不可控），
代价是部分路由器 AP 隔离下不可达——保留手动输 IP 的回退路径。
"""
import json
import socket
import threading
import time

DISCOVERY_PORT = 41830
BEACON_INTERVAL = 3.0
PROTO = "agent-bus"
PROTO_VER = 1


class BeaconBroadcaster:
    """主机侧广播线程（team 未初始化时不广播）。"""

    def __init__(self, get_beacon):
        """get_beacon: () -> dict | None。返回 None 时不广播（队伍未初始化）。"""
        self.get_beacon = get_beacon
        self._stop = threading.Event()
        self._thread = None
        self._sock = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="beacon")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not self._stop.wait(BEACON_INTERVAL):
            try:
                b = self.get_beacon()
                if b:
                    payload = json.dumps(b, ensure_ascii=False).encode("utf-8")
                    # 255.255.255.255 走默认路由（可能被代理 TUN 劫持），
                    # 另按候选 IP 补发各网段定向广播（192.168.x.255 等），
                    # 确保物理网卡所在网段必达
                    targets = [("255.255.255.255", DISCOVERY_PORT)]
                    for ip in b.get("ips") or []:
                        if ip.count(".") == 3 and not ip.startswith("127."):
                            targets.append((ip.rsplit(".", 1)[0] + ".255",
                                            DISCOVERY_PORT))
                    for t in dict.fromkeys(targets):  # 去重保序
                        self._sock.sendto(payload, t)
            except OSError:
                pass  # 网卡切换等瞬时错误：下个周期重试


def scan_teams(timeout: float = 5.0) -> list:
    """扫描局域网内的队伍，返回按发现顺序去重后的列表。

    返回项: {team_id, team_name, host_name, host_ip, mqtt_port, http_port, ver}
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", DISCOVERY_PORT))
    sock.settimeout(0.5)
    teams, order = {}, []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, (ip, _port) = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            b = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if b.get("proto") != PROTO:
            continue
        tid = b.get("team_id") or ""
        if not tid:
            continue
        if tid not in teams:
            # host_ip = 收包来源地址（对该子设备而言天然可达）；
            # host_ips = 主机自报全部候选（join 侧连通性自检用，发现≠连通）
            ips = [x for x in ([ip] + (b.get("ips") or [])) if x]
            teams[tid] = {
                "team_id": tid,
                "team_name": b.get("team_name", ""),
                "host_name": b.get("host_name", ""),
                "host_ip": ip,
                "host_ips": list(dict.fromkeys(ips)),
                "mqtt_port": int(b.get("mqtt_port", 1883)),
                "http_port": int(b.get("http_port", 8000)),
                "ver": b.get("ver", 1),
            }
            order.append(tid)
        # 同队多网卡/重复 beacon：保留首个（host_ips 并集补充）
        elif b.get("ips"):
            cur = teams[tid]["host_ips"]
            cur.extend(x for x in b["ips"] if x and x not in cur)
    sock.close()
    return [teams[t] for t in order]
