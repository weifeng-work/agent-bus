"""HTTP 文件服务客户端（Claim-Check 上传/下载）与在线名单查询。"""
import os
import urllib.parse

import requests


def upload_file(path: str, http_base: str, uploaded_by: str = "") -> dict:
    """上传文件，返回 {file_id, name, size, url}。"""
    url = f"{http_base}/api/files/upload"
    with open(path, "rb") as f:
        resp = requests.post(
            url,
            files={"file": (os.path.basename(path), f)},
            params={"uploaded_by": uploaded_by},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()


def download_file(url_or_id: str, dest: str, http_base: str = "") -> str:
    """下载文件到 dest。url_or_id 可以是完整 URL 或 file_id。"""
    if url_or_id.startswith("http://") or url_or_id.startswith("https://"):
        url = url_or_id
    else:
        base = http_base or os.environ.get("BUS_HTTP_BASE", "http://127.0.0.1:8000")
        url = f"{base.rstrip('/')}/api/files/{urllib.parse.quote(url_or_id)}"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                f.write(chunk)
    return dest


def list_agents_http(http_base: str) -> list:
    """查询已注册 Agent 名单（含在线状态）。"""
    resp = requests.get(f"{http_base}/api/agents", timeout=10)
    resp.raise_for_status()
    return resp.json()
