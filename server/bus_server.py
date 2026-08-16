"""中间架构服务端（单进程）：

  MQTT 桥   —— 订阅总线全部主题，落地注册/心跳/遗嘱/消息追溯到 SQLite
  HTTP API  —— 在线名单、消息时间线、文件上传下载（Claim-Check）
  Web 面板  —— server/static/index.html

运行:  python server/bus_server.py [--host 0.0.0.0] [--port 8000]
                              [--broker-host 127.0.0.1] [--broker-port 1883]
                              [--db data/bus.db] [--files-dir data/files]
"""
import argparse
import json
import logging
import os
import platform as _platform
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paho.mqtt.client as mqtt
import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_bus import provision
from agent_bus.discovery import BeaconBroadcaster, PROTO, PROTO_VER

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bus_server")

OFFLINE_AFTER_SECONDS = 90.0

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents(
                    agent_id TEXT PRIMARY KEY,
                    name TEXT, capabilities TEXT, platform TEXT, executor TEXT,
                    online INTEGER DEFAULT 1,
                    last_seen REAL, registered_at REAL
                );
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, topic TEXT, msg_type TEXT,
                    sender_id TEXT, target_id TEXT,
                    task_id TEXT, correlation_id TEXT,
                    status TEXT, payload TEXT
                );
                CREATE TABLE IF NOT EXISTS files(
                    file_id TEXT PRIMARY KEY, name TEXT, size INTEGER,
                    uploaded_by TEXT, ts REAL
                );
                CREATE TABLE IF NOT EXISTS http_tokens(
                    token TEXT PRIMARY KEY, agent_id TEXT,
                    role TEXT DEFAULT 'node', created_at REAL
                );
                CREATE TABLE IF NOT EXISTS team_info(
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    team_id TEXT, team_name TEXT,
                    pass_hash TEXT, created_at REAL, updated_at REAL
                );
                """
            )
            # 增量迁移：逐列补齐（列已存在时忽略；必须分开 try，否则前一列失败会跳过后一列）
            for col in ("hostname", "health"):
                try:
                    self.conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass

    def execute(self, sql, params=(), fetch=False):
        with _db_lock, self.conn:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall() if fetch else None
        return rows

    # ---- agents ----

    def upsert_agent(self, msg: dict):
        self.execute(
            """INSERT INTO agents(agent_id,name,capabilities,platform,executor,hostname,health,online,last_seen,registered_at)
               VALUES(?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 name=excluded.name, capabilities=excluded.capabilities,
                 platform=excluded.platform, executor=excluded.executor,
                 hostname=excluded.hostname, health=excluded.health,
                 online=1, last_seen=excluded.last_seen""",
            (msg["agent_id"], msg.get("name", ""), json.dumps(msg.get("capabilities", [])),
             msg.get("platform", ""), msg.get("executor", ""), msg.get("hostname", ""),
             msg.get("health", "unknown"),
             time.time(), msg.get("registered_at", time.time())),
        )

    def heartbeat(self, agent_id: str, health: str = None):
        # 心跳携带 health 时同步刷新（执行器登录态变化的推送通道）
        self.execute(
            "UPDATE agents SET online=1, last_seen=?, "
            "health=COALESCE(NULLIF(?,''), health) WHERE agent_id=?",
            (time.time(), health, agent_id),
        )

    def mark_offline(self, agent_id: str):
        self.execute("UPDATE agents SET online=0 WHERE agent_id=?", (agent_id,))

    def list_agents(self):
        rows = self.execute("SELECT * FROM agents ORDER BY online DESC, name", fetch=True)
        now = time.time()
        result = []
        for r in rows:
            stale = now - (r["last_seen"] or 0) > OFFLINE_AFTER_SECONDS
            result.append({
                "agent_id": r["agent_id"], "name": r["name"],
                "capabilities": json.loads(r["capabilities"] or "[]"),
                "platform": r["platform"], "executor": r["executor"],
                "hostname": r["hostname"] or "",
                "health": r["health"] or "unknown",
                "online": bool(r["online"]) and not stale,
                "last_seen": r["last_seen"], "registered_at": r["registered_at"],
            })
        return result

    # ---- messages ----

    def log_message(self, topic: str, msg: dict):
        # 心跳不进时间线：在线状态由 agents 表管理，入库只会刷屏
        if topic.startswith("bus/heartbeat/"):
            return
        target_id = msg.get("target_id", "")
        # task_result 兜底：旧版客户端不带 target_id，按 correlation_id 查原请求发起方
        if msg.get("type") == "task_result" and not target_id and msg.get("correlation_id"):
            rows = self.execute(
                "SELECT sender_id FROM messages WHERE correlation_id=? AND msg_type='task_request' LIMIT 1",
                (msg["correlation_id"],), fetch=True,
            )
            if rows:
                target_id = rows[0]["sender_id"]
        self.execute(
            """INSERT INTO messages(ts,topic,msg_type,sender_id,target_id,task_id,correlation_id,status,payload)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (time.time(), topic, msg.get("type", ""), msg.get("sender_id", ""),
             target_id, msg.get("task_id", ""),
             msg.get("correlation_id", ""), msg.get("status", ""),
             json.dumps(msg, ensure_ascii=False)),
        )

    def list_messages(self, limit=100, agent_id=None, keyword=None):
        sql, params = "SELECT * FROM messages", []
        conds = []
        if agent_id:
            conds.append("(sender_id=? OR target_id=?)")
            params += [agent_id, agent_id]
        if keyword:
            conds.append("payload LIKE ?")
            params.append(f"%{keyword}%")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.execute(sql, params, fetch=True)
        return [dict(r) for r in rows]

    # ---- files ----

    def add_file(self, file_id, name, size, uploaded_by):
        self.execute(
            "INSERT INTO files(file_id,name,size,uploaded_by,ts) VALUES(?,?,?,?,?)",
            (file_id, name, size, uploaded_by, time.time()),
        )

    def list_files(self):
        rows = self.execute("SELECT * FROM files ORDER BY ts DESC", fetch=True)
        return [dict(r) for r in rows]

    def get_file(self, file_id):
        rows = self.execute("SELECT * FROM files WHERE file_id=?", (file_id,), fetch=True)
        return dict(rows[0]) if rows else None

    # ---- http tokens ----

    def add_token(self, token: str, agent_id: str, role: str = "node"):
        self.execute(
            "INSERT OR REPLACE INTO http_tokens(token,agent_id,role,created_at) VALUES(?,?,?,?)",
            (token, agent_id, role, time.time()),
        )

    def check_token(self, token: str):
        rows = self.execute(
            "SELECT agent_id, role FROM http_tokens WHERE token=?", (token,), fetch=True
        )
        return dict(rows[0]) if rows else None

    # ---- team ----

    def get_team(self):
        rows = self.execute("SELECT * FROM team_info WHERE id=1", fetch=True)
        return dict(rows[0]) if rows else None

    def init_team(self, team_id: str, team_name: str, pass_hash: str):
        self.execute(
            "INSERT OR REPLACE INTO team_info(id,team_id,team_name,pass_hash,created_at,updated_at)"
            " VALUES(1,?,?,?,?,?)",
            (team_id, team_name, pass_hash, time.time(), time.time()),
        )

    def set_passphrase(self, pass_hash: str):
        self.execute("UPDATE team_info SET pass_hash=?, updated_at=? WHERE id=1",
                     (pass_hash, time.time()))

    def delete_agent(self, agent_id: str):
        self.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))


# ---------------------------------------------------------------------------
# MQTT 桥
# ---------------------------------------------------------------------------

class MqttBridge:
    def __init__(self, store: Store, broker_host: str, broker_port: int,
                 username: str = "", password: str = ""):
        self.store = store
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bus-server-bridge")
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.broker_host, self.broker_port = broker_host, broker_port

    def start(self):
        self.client.connect(self.broker_host, self.broker_port, keepalive=60)
        self.client.loop_start()

    def stop(self):
        try:
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            client.subscribe([("bus/#", 1), ("agent/+/inbox", 1)])
            log.info("MQTT 桥已连接 %s:%s，订阅 bus/# 与 agent/+/inbox",
                     self.broker_host, self.broker_port)
        else:
            log.error("MQTT 连接失败 reason_code=%s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        topic = msg.topic
        self.store.log_message(topic, data)  # 全量追溯
        t = data.get("type")
        if t == "register":
            self.store.upsert_agent(data)
            log.info("注册: %s (%s)", data.get("agent_id"), data.get("name", ""))
        elif topic.startswith("bus/heartbeat/"):
            self.store.heartbeat(data.get("agent_id", ""), health=data.get("health"))
        elif topic.startswith("bus/offline/"):
            self.store.mark_offline(data.get("agent_id", ""))
            log.info("离线(遗嘱): %s", data.get("agent_id"))


# ---------------------------------------------------------------------------
# join 限速（内存态：每 IP 5 次失败锁 5 分钟——防短码爆破）
# ---------------------------------------------------------------------------

class JoinRateLimiter:
    def __init__(self, max_fails=5, lock_seconds=300):
        self.max_fails, self.lock_seconds = max_fails, lock_seconds
        self._state = {}  # ip -> {"fails": int, "lock_until": float}
        self._lock = threading.Lock()

    def check(self, ip: str):
        with self._lock:
            st = self._state.get(ip)
            if st and st["lock_until"] > time.time():
                wait = int(st["lock_until"] - time.time())
                raise HTTPException(429, f"失败次数过多，已锁定，请 {wait}s 后重试")

    def fail(self, ip: str):
        with self._lock:
            st = self._state.setdefault(ip, {"fails": 0, "lock_until": 0})
            st["fails"] += 1
            if st["fails"] >= self.max_fails:
                st["lock_until"] = time.time() + self.lock_seconds
                st["fails"] = 0

    def reset(self, ip: str):
        with self._lock:
            self._state.pop(ip, None)


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

def create_app(store: Store, files_dir: Path, bridge: MqttBridge,
               auth_enabled: bool = True, broker_port: int = 1883) -> FastAPI:
    app = FastAPI(title="Agent Bus Server")
    files_dir.mkdir(parents=True, exist_ok=True)

    from fastapi import Depends, Header
    from fastapi.security.utils import get_authorization_scheme_param

    join_limiter = JoinRateLimiter()

    def require_token(authorization: str = Header(None), token: str = Query(None)):
        """API 令牌认证：Authorization: Bearer <token> 或 ?token=（供浏览器下载链接）。
        未启用认证（auth_enabled=False，如初始化前）时直接放行。"""
        if not auth_enabled:
            return {"agent_id": "", "role": "anonymous"}
        tok = None
        if authorization:
            scheme, param = get_authorization_scheme_param(authorization)
            if scheme.lower() == "bearer":
                tok = param
        tok = tok or token
        if not tok:
            raise HTTPException(401, "missing token")
        ident = store.check_token(tok)
        if not ident:
            raise HTTPException(401, "invalid token")
        return ident

    def require_admin(ident: dict = Depends(require_token)):
        """管理操作（口令管理/节点移除）：仅 admin/bridge 角色（role 分离，审核 E.1）。"""
        if auth_enabled and ident.get("role") not in ("admin", "bridge"):
            raise HTTPException(403, "admin role required")
        return ident

    @app.get("/api/health")
    def health():
        # 开放端点（发现/连通性探测用），不泄露任何业务信息
        return {"ok": True}

    # ---- 队伍：首次向导 + 加入（唯一的匿名业务端点） ----

    class SetupBody(BaseModel):
        team_name: str
        passphrase: str

    class JoinBody(BaseModel):
        passphrase: str
        agent_id: str = ""
        device_name: str = ""
        platform: str = ""

    @app.get("/api/team/status")
    def team_status():
        """匿名：面板据此决定显示首次向导还是登录。不泄露口令相关信息。"""
        t = store.get_team()
        return {"initialized": bool(t), "team_name": t["team_name"] if t else ""}

    @app.post("/api/team/setup")
    def team_setup(body: SetupBody):
        """匿名但仅可用一次：队伍未初始化时允许设定队名+口令（首启向导）。
        已初始化后永久 403——重置口令需 admin 登录面板操作。"""
        if store.get_team():
            raise HTTPException(403, "team already initialized")
        team_name = body.team_name.strip()
        if not (1 <= len(team_name) <= 32):
            raise HTTPException(400, "队伍名称长度需 1-32")
        if not (4 <= len(body.passphrase) <= 64):
            raise HTTPException(400, "口令长度需 4-64")
        store.init_team(uuid.uuid4().hex[:12], team_name,
                        provision.hash_passphrase(body.passphrase))
        log.info("队伍已初始化: %s", team_name)
        return {"ok": True, "team_name": team_name}

    @app.post("/api/join")
    def join(body: JoinBody, request: Request):
        """子设备加入队伍：核对口令 → 自动发凭据（MQTT 用户 + role=node HTTP 令牌）。

        安全：匿名端点仅此一个；口令错误计入 IP 限速（5 次锁 5 分钟）；
        重置节点先撤销旧令牌（provision 幽灵令牌修复）。
        """
        team = store.get_team()
        if not team:
            raise HTTPException(403, "team not initialized")
        ip = request.client.host if request.client else "?"
        join_limiter.check(ip)
        if not provision.verify_passphrase(body.passphrase, team["pass_hash"]):
            join_limiter.fail(ip)
            raise HTTPException(401, "口令错误")
        join_limiter.reset(ip)

        agent_id = (body.agent_id or "").strip() or f"node-{uuid.uuid4().hex[:6]}"
        if not provision.valid_agent_id(agent_id):
            raise HTTPException(400, "agent_id 非法（限 [A-Za-z0-9_-]，1-64 位）")
        try:
            creds = provision.CredStore(db_path=store.db_path).provision(agent_id, role="node")
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            raise HTTPException(500, f"凭据开通失败: {e}")

        restarted, restart_msg = provision.restart_user_broker()
        if not restarted:
            log.warning("join: %s", restart_msg)
        log.info("节点加入: %s (%s) from %s", agent_id, body.device_name, ip)
        return {
            "ok": True, "team_name": team["team_name"], "team_id": team["team_id"],
            "agent_id": agent_id,
            "mqtt_user": creds["mqtt_user"], "mqtt_pass": creds["mqtt_pass"],
            "http_token": creds["http_token"],
            "broker_host": provision.get_local_ip(), "broker_port": broker_port,
            "broker_restarted": restarted, "broker_message": restart_msg,
        }

    # ---- 管理（admin/bridge 角色） ----

    class PassphraseBody(BaseModel):
        passphrase: str = ""   # 留空自动生成 6 位数字短码

    @app.post("/api/admin/passphrase")
    def regenerate_passphrase(body: PassphraseBody, ident: dict = Depends(require_admin)):
        new_pass = body.passphrase.strip() or f"{secrets.randbelow(1000000):06d}"
        if not (4 <= len(new_pass) <= 64):
            raise HTTPException(400, "口令长度需 4-64")
        store.set_passphrase(provision.hash_passphrase(new_pass))
        log.info("加入口令已重新生成（不影响在册设备）by %s", ident.get("agent_id"))
        return {"ok": True, "passphrase": new_pass}

    @app.delete("/api/admin/nodes/{agent_id}")
    def remove_node(agent_id: str, ident: dict = Depends(require_admin)):
        """移除节点：吊销 HTTP 令牌 + 删 MQTT 用户 + 从名单摘除。passwd 需重启生效。"""
        if agent_id == provision.BRIDGE_USER:
            raise HTTPException(400, "不能移除桥接账号")
        cs = provision.CredStore(db_path=store.db_path)
        cs.revoke_tokens(agent_id)
        try:
            provision.remove_mqtt_user(provision.auth_dir() / "passwd", agent_id)
        except (RuntimeError, FileNotFoundError) as e:
            log.warning("移除 MQTT 用户失败（继续摘除名单）: %s", e)
        store.delete_agent(agent_id)
        restarted, restart_msg = provision.restart_user_broker()
        log.info("节点已移除: %s by %s（%s）", agent_id, ident.get("agent_id"), restart_msg)
        return {"ok": True, "agent_id": agent_id, "broker_restarted": restarted,
                "broker_message": restart_msg}

    @app.get("/api/agents")
    def agents(ident: dict = Depends(require_token)):
        return store.list_agents()

    @app.get("/api/messages")
    def messages(limit: int = Query(100, ge=1, le=1000),
                 agent_id: str = None, keyword: str = None,
                 ident: dict = Depends(require_token)):
        return store.list_messages(limit, agent_id, keyword)

    @app.get("/api/files")
    def files_list(ident: dict = Depends(require_token)):
        base = os.environ.get("BUS_HTTP_BASE", "").rstrip("/")
        out = []
        for f in store.list_files():
            f["url"] = f"{base}/api/files/{f['file_id']}" if base else f"/api/files/{f['file_id']}"
            out.append(f)
        return out

    @app.post("/api/files/upload")
    async def upload(file: UploadFile = File(...), uploaded_by: str = "",
                     ident: dict = Depends(require_token)):
        file_id = uuid.uuid4().hex[:12]
        dest = files_dir / f"{file_id}_{Path(file.filename or 'unnamed').name}"
        size = 0
        with open(dest, "wb") as out:
            while chunk := await file.read(64 * 1024):
                size += len(chunk)
                out.write(chunk)
        store.add_file(file_id, file.filename, size, uploaded_by)
        base = os.environ.get("BUS_HTTP_BASE", "").rstrip("/")
        url = f"{base}/api/files/{file_id}" if base else f"/api/files/{file_id}"
        log.info("文件上传: %s (%d bytes) by %s", file.filename, size, uploaded_by)
        return {"file_id": file_id, "name": file.filename, "size": size, "url": url}

    @app.get("/api/files/{file_id}")
    def download(file_id: str, ident: dict = Depends(require_token)):
        meta = store.get_file(file_id)
        if not meta:
            raise HTTPException(404, "file not found")
        path = files_dir / f"{file_id}_{meta['name']}"
        if not path.exists():
            raise HTTPException(410, "file data missing")
        return FileResponse(path, filename=meta["name"])

    @app.on_event("shutdown")
    def shutdown():
        bridge.stop()

    app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
    return app


def main():
    parser = argparse.ArgumentParser(description="Agent Bus 中间架构服务端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--broker-host", default=os.environ.get("BUS_BROKER_HOST", "127.0.0.1"))
    parser.add_argument("--broker-port", type=int, default=int(os.environ.get("BUS_BROKER_PORT", "1883")))
    parser.add_argument("--db", default=str(ROOT_DIR / "data" / "bus.db"))
    parser.add_argument("--files-dir", default=str(ROOT_DIR / "data" / "files"))
    parser.add_argument("--mqtt-user", default=os.environ.get("BUS_MQTT_USER", ""))
    parser.add_argument("--mqtt-pass", default=os.environ.get("BUS_MQTT_PASS", ""))
    parser.add_argument("--no-api-auth", action="store_true",
                        help="禁用 HTTP API 令牌认证（仅初始化/排障用）")
    args = parser.parse_args()

    # 面板/消息里展示的绝对 URL 基址（默认用本机回环，局域网部署时设置环境变量）
    if not os.environ.get("BUS_HTTP_BASE"):
        os.environ["BUS_HTTP_BASE"] = f"http://127.0.0.1:{args.port}"

    store = Store(Path(args.db))
    bridge = MqttBridge(store, args.broker_host, args.broker_port,
                        username=args.mqtt_user, password=args.mqtt_pass)
    bridge.start()
    app = create_app(store, Path(args.files_dir), bridge,
                     auth_enabled=not args.no_api_auth, broker_port=args.broker_port)

    # 队伍发现广播：未初始化时 get_beacon 返回 None，不广播
    def _beacon():
        t = store.get_team()
        if not t:
            return None
        return {"proto": PROTO, "ver": PROTO_VER, "team_id": t["team_id"],
                "team_name": t["team_name"], "host_name": _platform.node(),
                "ips": provision.get_local_ips(),  # 多网卡/代理 TUN 场景全量自报
                "mqtt_port": args.broker_port, "http_port": args.port}

    BeaconBroadcaster(_beacon).start()
    log.info("Agent Bus Server 启动: http://%s:%d (面板在 /)", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
