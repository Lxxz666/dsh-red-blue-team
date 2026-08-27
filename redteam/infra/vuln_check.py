"""基础设施渗透 · 常见未授权/高危漏洞检测（深度渗透第三层）。

针对扫描发现的开放高危服务做**只读**未授权访问与已知风险检测：
Redis / MongoDB / Memcached 未授权、Spring Actuator 泄露、Docker API、
Elasticsearch、Nacos、Shiro 指纹等。全部被动/只读探测，不执行破坏性操作。
"""
from __future__ import annotations

import socket
from typing import Dict, List

import httpx

_COMMON_TIMEOUT = 4.0


def _tcp(host: str, port: int, data: bytes = b"", read: int = 512,
         timeout: float = _COMMON_TIMEOUT) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            if data:
                try:
                    s.sendall(data)
                except OSError:
                    pass
            buf = b""
            try:
                buf = s.recv(read)
            except (socket.timeout, OSError):
                pass
            return buf.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _http(host: str, port: int, path: str, scheme: str = "http",
          timeout: float = _COMMON_TIMEOUT) -> httpx.Response:
    with httpx.Client(timeout=timeout, verify=False,
                      follow_redirects=False) as c:
        return c.get(f"{scheme}://{host}:{port}{path}")


# ---- 单项检测（返回 dict: check/status/detail/port） ----


def check_redis(host: str, port: int) -> Dict[str, object]:
    out = _tcp(host, port, b"INFO\r\n", 400)
    if not out:
        return {"check": "Redis 未授权", "status": "unknown",
                "detail": "无响应", "port": port}
    low = out.lower()
    if "+pong" in low or "redis_version" in low or "role:" in low:
        return {"check": "Redis 未授权", "status": "vuln",
                "detail": f"未授权可执行命令（banner: {out[:70].strip()}）",
                "port": port}
    if "noauth" in low or "authentication" in low or "-err" in low:
        return {"check": "Redis 未授权", "status": "safe",
                "detail": "需要认证", "port": port}
    return {"check": "Redis 未授权", "status": "unknown",
            "detail": out[:70].strip(), "port": port}


def check_mongodb(host: str, port: int) -> Dict[str, object]:
    # isMaster 未授权探测
    import struct
    try:
        with socket.create_connection((host, port),
                                      timeout=_COMMON_TIMEOUT) as s:
            s.settimeout(_COMMON_TIMEOUT)
            body = b'{"isMaster":1,"client":{"application":{"name":"p"}}}'
            msg = struct.pack("<i", len(body) + 16) + b"\x00\x00\x00\x00" \
                + b"\xd4\x07\x00\x00" + b"\x00\x00\x00\x00" + body
            s.sendall(msg)
            data = s.recv(1024)
        if data:
            return {"check": "MongoDB 未授权", "status": "vuln",
                    "detail": "isMaster 未授权可查询", "port": port}
    except Exception:
        pass
    return {"check": "MongoDB 未授权", "status": "safe",
            "detail": "无未授权响应/未开放", "port": port}


def check_memcached(host: str, port: int) -> Dict[str, object]:
    out = _tcp(host, port, b"version\r\n", 128)
    if out.strip():
        return {"check": "Memcached 未授权", "status": "vuln",
                "detail": f"未授权可读（{out.strip()}）", "port": port}
    return {"check": "Memcached 未授权", "status": "safe",
            "detail": "无响应/需认证", "port": port}


def check_actuator(host: str, port: int, scheme: str = "http") -> Dict[str, object]:
    try:
        r = _http(host, port, "/actuator", scheme)
        if r.status_code == 200 and "spring" in (r.text or "").lower():
            return {"check": "Spring Actuator 未授权", "status": "vuln",
                    "detail": "/actuator 未授权可达，可进一步读取 env/heapdump",
                    "port": port}
        r2 = _http(host, port, "/actuator/env", scheme)
        if r2.status_code == 200 and any(k in r2.text.lower()
                                         for k in ("spring", "password", "jdbc")):
            return {"check": "Spring Actuator 配置泄露", "status": "vuln",
                    "detail": "/actuator/env 泄露配置（可能含密钥/数据源）",
                    "port": port}
        if r.status_code in (200, 401, 403):
            return {"check": "Spring Actuator", "status": "safe",
                    "detail": f"端点存在但 HTTP {r.status_code}", "port": port}
    except Exception:
        pass
    return {"check": "Spring Actuator", "status": "safe",
            "detail": "未检测到", "port": port}


def check_docker_api(host: str, port: int, scheme: str = "http") -> Dict[str, object]:
    try:
        r = _http(host, port, "/version", scheme)
        if r.status_code == 200 and ("api_version" in r.text.lower()
                                     or "version" in r.text.lower()):
            return {"check": "Docker API 未授权", "status": "vuln",
                    "detail": "Docker Remote API 未授权可达（可接管容器）",
                    "port": port}
    except Exception:
        pass
    return {"check": "Docker API", "status": "safe",
            "detail": "未检测到", "port": port}


def check_elasticsearch(host: str, port: int, scheme: str = "http") -> Dict[str, object]:
    try:
        r = _http(host, port, "/", scheme)
        if r.status_code == 200 and "elasticsearch" in r.text.lower():
            return {"check": "Elasticsearch 未授权", "status": "vuln",
                    "detail": "ES 未授权可读（集群信息泄露）", "port": port}
    except Exception:
        pass
    return {"check": "Elasticsearch", "status": "safe",
            "detail": "未检测到", "port": port}


def check_nacos(host: str, port: int, scheme: str = "http") -> Dict[str, object]:
    try:
        r = _http(host, port, "/nacos/v1/auth/users", scheme)
        if r.status_code == 200 and "username" in r.text.lower():
            return {"check": "Nacos 未授权", "status": "vuln",
                    "detail": "Nacos 用户列表未授权泄露", "port": port}
    except Exception:
        pass
    return {"check": "Nacos", "status": "safe", "detail": "未检测到", "port": port}


def check_shiro(host: str, port: int, scheme: str = "http") -> Dict[str, object]:
    try:
        r = _http(host, port, "/", scheme)
        sc = r.headers.get("Set-Cookie", "")
        if "rememberme=deleteme" in sc.lower():
            return {"check": "Apache Shiro 指纹", "status": "info",
                    "detail": "检测到 Shiro rememberMe（存在反序列化利用面）",
                    "port": port}
    except Exception:
        pass
    return {"check": "Apache Shiro", "status": "safe",
            "detail": "未检测到", "port": port}


# ---- 端口 → 适用检测 映射 ----


def _dispatch(port: int, host: str, scheme: str) -> List[Dict[str, object]]:
    checks = []
    if port == 6379:
        checks.append(check_redis(host, port))
    elif port == 27017:
        checks.append(check_mongodb(host, port))
    elif port == 11211:
        checks.append(check_memcached(host, port))
    elif port == 2375:
        checks.append(check_docker_api(host, port, scheme))
    elif port == 9200:
        checks.append(check_elasticsearch(host, port, scheme))
    elif port == 8848:
        checks.append(check_nacos(host, port, scheme))
    if port in (80, 443, 8080, 8081, 8088, 8443, 8888, 9000, 8000, 8001,
                7001, 8161, 10080):
        checks.append(check_actuator(host, port, scheme))
        checks.append(check_shiro(host, port, scheme))
    return checks


def run_vuln_checks(host: str, open_ports: List[int],
                    scheme: str = "http") -> List[Dict[str, object]]:
    """对开放端口跑适用检测，返回全部结果。"""
    out: List[Dict[str, object]] = []
    for port in open_ports:
        for c in _dispatch(int(port), host, scheme):
            out.append(c)
    return out
