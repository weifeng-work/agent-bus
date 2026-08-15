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
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt
import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
                """
            )

    def execute(self, sql, params=(), fetch=False):
        with _db_lock, self.conn:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall() if fetch else None
        return rows

    # ---- agents ----

    def upsert_agent(self, msg: dict):
        self.execute(
            """INSERT INTO agents(agent_id,name,capabilities,platform,executor,online,last_seen,registered_at)
               VALUES(?,?,?,?,?,1,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 name=excluded.name, capabilities=excluded.capabilities,
                 platform=excluded.platform, executor=excluded.executor,
                 online=1, last_seen=excluded.last_seen""",
            (msg["agent_id"], msg.get("name", ""), json.dumps(msg.get("capabilities", [])),
             msg.get("platform", ""), msg.get("executor", ""),
             time.time(), msg.get("registered_at", time.time())),
        )

    def heartbeat(self, agent_id: str):
        self.execute(
            "UPDATE agents SET online=1, last_seen=? WHERE agent_id=?",
            (time.time(), agent_id),
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


# ---------------------------------------------------------------------------
# MQTT 桥
# ---------------------------------------------------------------------------

class MqttBridge:
    def __init__(self, store: Store, broker_host: str, broker_port: int):
        self.store = store
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bus-server-bridge")
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
            self.store.heartbeat(data.get("agent_id", ""))
        elif topic.startswith("bus/offline/"):
            self.store.mark_offline(data.get("agent_id", ""))
            log.info("离线(遗嘱): %s", data.get("agent_id"))


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

def create_app(store: Store, files_dir: Path, bridge: MqttBridge) -> FastAPI:
    app = FastAPI(title="Agent Bus Server")
    files_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/api/health")
    def health():
        return {"ok": True, "agents": len(store.list_agents())}

    @app.get("/api/agents")
    def agents():
        return store.list_agents()

    @app.get("/api/messages")
    def messages(limit: int = Query(100, ge=1, le=1000),
                 agent_id: str = None, keyword: str = None):
        return store.list_messages(limit, agent_id, keyword)

    @app.get("/api/files")
    def files_list():
        base = os.environ.get("BUS_HTTP_BASE", "").rstrip("/")
        out = []
        for f in store.list_files():
            f["url"] = f"{base}/api/files/{f['file_id']}" if base else f"/api/files/{f['file_id']}"
            out.append(f)
        return out

    @app.post("/api/files/upload")
    async def upload(file: UploadFile = File(...), uploaded_by: str = ""):
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
    def download(file_id: str):
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
    args = parser.parse_args()

    # 面板/消息里展示的绝对 URL 基址（默认用本机回环，局域网部署时设置环境变量）
    if not os.environ.get("BUS_HTTP_BASE"):
        os.environ["BUS_HTTP_BASE"] = f"http://127.0.0.1:{args.port}"

    store = Store(Path(args.db))
    bridge = MqttBridge(store, args.broker_host, args.broker_port)
    bridge.start()
    app = create_app(store, Path(args.files_dir), bridge)
    log.info("Agent Bus Server 启动: http://%s:%d (面板在 /)", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
