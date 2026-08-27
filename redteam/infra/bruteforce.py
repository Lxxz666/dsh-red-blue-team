"""深度渗透 · 弱口令/认证绕过检测（opt-in，仅授权目标）。

Redis / HTTP 管理后台常见弱口令检测（零额外依赖，纯 socket + httpx）。

⚠️ 合规边界：弱口令爆破对公网目标有封禁与合规风险。本模块**默认关闭**
（`--brute` 显式开启 / `brute_enabled=true`），且仅建议在**已授权目标**上使用。
"""
from __future__ import annotations

import socket
from typing import Dict, List, Optional

import httpx

_TIMEOUT = 4.0

# 常见 Redis 弱口令（含空密码）
REDIS_PASSWORDS = ["", "redis", "123456", "12345678", "admin", "root",
                   "password", "redis123", "root123", "test", "123123",
                   "123456789", "admin123", "666666", "888888"]

# 常见后台弱口令（user, pass）
HTTP_ADMIN_CREDS = [
    ("admin", "admin"), ("admin", "123456"), ("admin", "admin123"),
    ("admin", "password"), ("admin", "admin888"), ("admin", "12345678"),
    ("root", "root"), ("root", "123456"), ("root", "admin"),
    ("test", "test"), ("test", "123456"), ("admin", "111111"),
]


def check_redis_password(host: str, port: int, password: str,
                         timeout: float = _TIMEOUT) -> bool:
    """Redis AUTH 弱口令：AUTH <pass> 返回 +OK 则命中。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            if password:
                s.sendall(f"AUTH {password}\r\n".encode())
            else:
                s.sendall(b"PING\r\n")  # 空密码 = 未授权
            data = s.recv(128).decode("utf-8", errors="replace")
        return "+ok" in data.lower() or "pong" in data.lower()
    except Exception:
        return False


def brute_redis(host: str, port: int,
                passwords: Optional[List[str]] = None) -> Dict[str, object]:
    """对 Redis 尝试常见弱口令，返回命中结果。"""
    passwords = passwords or REDIS_PASSWORDS
    for pw in passwords:
        if check_redis_password(host, port, pw):
            return {"service": "Redis", "port": port, "credential": pw,
                    "status": "vuln",
                    "detail": f"Redis 弱口令/未授权：password={pw!r}"}
    return {"service": "Redis", "port": port, "status": "safe",
            "detail": "常见弱口令未命中"}


def _login_http(host: str, port: int, path: str, user: str, password: str,
                scheme: str = "http", timeout: float = _TIMEOUT) -> bool:
    """POST 表单登录，检测是否成功（302 到后台 / Set-Cookie 会话 / 200 且非错误）。"""
    try:
        with httpx.Client(timeout=timeout, verify=False,
                          follow_redirects=False) as c:
            r = c.post(f"{scheme}://{host}:{port}{path}",
                       data={"username": user, "password": password,
                             "user": user, "pass": password},
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
            loc = r.headers.get("Location", "") or ""
            sc = r.headers.get("Set-Cookie", "") or ""
            if r.status_code == 302 and ("admin" in loc.lower()
                                         or "index" in loc.lower()
                                         or "home" in loc.lower()
                                         or "main" in loc.lower()):
                return True
            if sc and ("session" in sc.lower() or "token" in sc.lower()
                       or "auth" in sc.lower()) and r.status_code in (200, 302):
                return True
            if r.status_code == 200:
                low = r.text.lower()
                if ("logout" in low or "welcome" in low or "dashboard" in low) \
                        and ("error" not in low[:500] and "wrong" not in low[:500]):
                    return True
    except Exception:
        pass
    return False


def brute_http_admin(host: str, port: int, scheme: str = "http",
                     login_paths: Optional[List[str]] = None,
                     creds: Optional[List[tuple]] = None) -> List[Dict[str, object]]:
    """对 HTTP 管理后台尝试常见弱口令。"""
    creds = creds or HTTP_ADMIN_CREDS
    login_paths = login_paths or ["/admin/login", "/admin", "/api/login",
                                  "/login", "/admin/login.html"]
    out: List[Dict[str, object]] = []
    for path in login_paths:
        for user, pw in creds:
            if _login_http(host, port, path, user, pw, scheme):
                out.append({"service": f"HTTP 后台({path})", "port": port,
                            "credential": f"{user}:{pw}", "status": "vuln",
                            "detail": f"后台弱口令命中：{user}/{pw} @ {path}"})
                return out  # 命中一个即可，避免多余请求
    return out


def run_brute(host: str, open_ports: List[int],
              scheme: str = "http") -> List[Dict[str, object]]:
    """对开放端口跑弱口令检测（仅 Redis + HTTP 后台，零依赖）。"""
    out: List[Dict[str, object]] = []
    for port in open_ports:
        port = int(port)
        if port == 6379:
            out.append(brute_redis(host, port))
        if port in (80, 443, 8080, 8081, 8088, 8443, 8888, 9000, 8000, 8001):
            for res in brute_http_admin(host, port, scheme):
                out.append(res)
    return out


def summarize_brute(results: List[Dict[str, object]]) -> str:
    vulns = [r for r in results if r.get("status") == "vuln"]
    if not vulns:
        return "弱口令检测未命中（常见弱口令）"
    return "\n".join(f"[弱口令] {r.get('service')} 命中 {r.get('credential')} "
                     f"｜ {r.get('detail','')}" for r in vulns)
