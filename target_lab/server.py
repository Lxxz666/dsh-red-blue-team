"""target_lab.server —— 靶场 HTTP 服务器（纯标准库，零依赖）。

用法::

    lab = start_lab(guards_file="lab_guards.yml", port=8765)
    print(lab.port)      # 实际监听端口（port=0 时为随机端口）
    ...
    lab.stop()

所有路由逻辑在 target_lab.app.LabApp 中，HTTP 层只做协议转换。
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .app import LabApp
from .guards import DEFAULT_GUARDS, dump_guards


def build_default_guards_file(path: str) -> str:
    """生成默认"弱防护"（全部漏洞开启）的防护配置文件。"""
    dump_guards(DEFAULT_GUARDS, path)
    return path


class _Handler(BaseHTTPRequestHandler):
    """把 HTTP 请求转换为 LabApp.handle(kind, **payload) 调用。"""

    app: LabApp = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        pass

    # ---- 路由分发 ----

    def _dispatch(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[-1] for k, v in
                  urllib.parse.parse_qs(parsed.query).items()}
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = parsed.path
        # 无 body 方法若带 Content-Length：排空残留字节，防止污染 keep-alive 连接
        if self.command in ("GET", "HEAD", "DELETE"):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                self.rfile.read(length)

        if self.command == "POST" and path == "/api/chat":
            body = self._read_json()
            result = self.app.handle(
                "chat", messages=body.get("messages") or [],
                role=body.get("role", "customer"))
        elif self.command == "GET" and path == "/api/state":
            result = self.app.handle("state", token=headers.get("x-scanner-token", ""))
        elif self.command == "POST" and path == "/api/_admin/reload":
            result = self.app.handle("reload", token=headers.get("x-admin-token", ""))
        elif self.command == "POST" and path == "/api/_admin/reset":
            result = self.app.handle("reset", token=headers.get("x-admin-token", ""))
        elif self.command == "GET" and path == "/api/health":
            result = self.app.handle("health")
        else:
            body: Dict[str, Any] = {}
            payload = ""
            content_type = self.headers.get("Content-Type", "")
            if self.command in ("POST", "PUT", "PATCH"):
                if "json" in content_type:
                    body = self._read_json()
                else:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = self.rfile.read(length).decode("utf-8",
                                                             errors="replace")
            result = self.app.handle(
                "api", method=self.command, path=path, params=params,
                body=body, payload=payload, headers=headers)
        self._write(result)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _write(self, result: Dict[str, Any]) -> None:
        status = int(result.get("status", 200))
        text = str(result.get("text", ""))
        headers = {str(k): str(v) for k, v in
                   (result.get("headers") or {}).items()}
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",
                         "application/json; charset=utf-8" if status != 302
                         else "text/plain; charset=utf-8")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch


class LabServer:
    """靶场服务器句柄。"""

    def __init__(self, guards_file: Optional[str] = None,
                 port: int = 0, host: str = "127.0.0.1") -> None:
        self.app = LabApp(guards_file)
        self.guards_file = guards_file
        handler = type("_BoundHandler", (_Handler,), {"app": self.app})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self.port: int = self._httpd.server_address[1]
        self.host = host
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "LabServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="target-lab")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "LabServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def start_lab(guards_file: Optional[str] = None, port: int = 0,
              host: str = "127.0.0.1") -> LabServer:
    """一键起靶场（线程内 HTTP 服务，返回已启动的句柄）。"""
    return LabServer(guards_file=guards_file, port=port, host=host).start()
