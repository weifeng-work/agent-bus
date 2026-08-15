"""集中配置：全部可由环境变量覆盖，方便脚本化与跨机部署。

环境变量:
  BUS_BROKER_HOST  MQTT Broker 地址（默认 127.0.0.1）
  BUS_BROKER_PORT  MQTT 端口（默认 1883）
  BUS_HTTP_BASE    中间架构 HTTP 基址（默认 http://127.0.0.1:8000）
  BUS_AGENT_ID     默认本节点 agent_id
"""
import os


class BusConfig:
    def __init__(self, broker_host=None, broker_port=None, http_base=None, agent_id=None):
        self.broker_host = broker_host or os.environ.get("BUS_BROKER_HOST", "127.0.0.1")
        self.broker_port = int(broker_port or os.environ.get("BUS_BROKER_PORT", "1883"))
        self.http_base = (http_base or os.environ.get("BUS_HTTP_BASE", "http://127.0.0.1:8000")).rstrip("/")
        self.agent_id = agent_id or os.environ.get("BUS_AGENT_ID", "")

    @classmethod
    def load(cls, **overrides) -> "BusConfig":
        clean = {k: v for k, v in overrides.items() if v is not None}
        return cls(**clean)
