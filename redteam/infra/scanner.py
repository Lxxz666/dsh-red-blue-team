"""基础设施渗透 · 端口扫描与服务识别（纯标准库 socket，零依赖）。

对目标主机做 TCP 端口扫描 + banner 抓取 + 服务猜测，作为深度渗透的第一层
（信息收集）。后续 fingerprint / vuln_check 基于这里的结果深入。
"""
from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

# 常用高危/业务端口（参考攻防演练与 SRC 常见暴露面）
COMMON_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 135, 139, 143, 443, 445, 873, 1080,
    1433, 1521, 2181, 2375, 3306, 3389, 5432, 5900, 6379, 7001,
    8000, 8001, 8080, 8081, 8088, 8161, 8443, 8880, 8888, 9000,
    9090, 9092, 9200, 9300, 10000, 11211, 15672, 27017, 5000,
    50070, 8848, 10080,
]

# 端口 → 是否发 HTTP 探测 / 发什么探测
_HTTP_PORTS = {80, 443, 8080, 8081, 8088, 8443, 8880, 8888, 9000, 9090,
               8000, 8001, 8161, 9200, 2375, 8848, 7001, 10080, 50070}
_BANNER_PAYLOAD = {
    6379: b"PING\r\n",        # Redis
    9200: b"GET / HTTP/1.0\r\n\r\n",   # Elasticsearch
    8848: b"GET /nacos/v1/console/health/readiness HTTP/1.0\r\n\r\n",
    2375: b"GET /version HTTP/1.0\r\n\r\n",   # Docker API
}
_HTTP_PORTS_ALL = {
    80: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    443: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    8080: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    8081: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    8443: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    8888: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    9000: b"GET / HTTP/1.0\r\nHost: x\r\n\r\n",
    9200: b"GET / HTTP/1.0\r\n\r\n",
    2375: b"GET /version HTTP/1.0\r\n\r\n",
}


def _tcp_banner(host: str, port: int, timeout: float,
                payload: bytes = b"") -> Tuple[bool, str]:
    """TCP 连接并抓 banner（返回: 是否开放, banner 文本）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            if payload:
                try:
                    s.sendall(payload)
                except OSError:
                    pass
            chunks: List[bytes] = []
            total = 0
            while total < 512:
                try:
                    data = s.recv(512)
                except socket.timeout:
                    break
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                total += len(data)
                if b"\r\n\r\n" in b"".join(chunks) or total >= 512:
                    break
            banner = b"".join(chunks).decode("utf-8", errors="replace")
            return True, banner[:512]
    except (socket.timeout, OSError):
        return False, ""


def _guess_service(port: int, banner: str) -> str:
    b = banner.lower()
    if b.startswith("ssh-2.0"):
        return "SSH"
    if "coyote" in b or ("apache" in b and "tomcat" in b):
        return "Apache Tomcat"
    if "nginx" in b:
        return "Nginx"
    if "apache/" in b:
        return "Apache HTTP"
    if "iis" in b:
        return "Microsoft IIS"
    if "spring" in b:
        return "Spring Boot"
    if "mysql" in b or "maria" in b:
        return "MySQL/MariaDB"
    if "redis" in b or b.startswith("+pong"):
        return "Redis"
    if "postgresql" in b or "postgres" in b:
        return "PostgreSQL"
    if "elasticsearch" in b:
        return "Elasticsearch"
    if "mongodb" in b or "iswritableprimary" in b:
        return "MongoDB"
    if "docker" in b or "\"api_version\"" in b:
        return "Docker API"
    if "nacos" in b:
        return "Nacos"
    if "memcached" in b or "error" in b and "memcache" in b:
        return "Memcached"
    if "weblogic" in b:
        return "WebLogic"
    if "jboss" in b:
        return "JBoss"
    if "jetty" in b:
        return "Jetty"
    if "shiro" in b or "rememberme" in b:
        return "Apache Shiro"
    if "fastjson" in b:
        return "Fastjson"
    guess = {
        21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS",
        110: "POP3", 135: "MSRPC", 139: "NetBIOS", 143: "IMAP",
        443: "HTTPS", 445: "SMB", 873: "rsync", 1080: "SOCKS",
        1433: "MSSQL", 1521: "Oracle", 2181: "ZooKeeper", 2375: "Docker API",
        3306: "MySQL", 3389: "RDP", 5432: "PostgreSQL", 5900: "VNC",
        6379: "Redis", 7001: "WebLogic", 8000: "HTTP", 8080: "HTTP",
        8161: "ActiveMQ", 8443: "HTTPS", 8888: "HTTP", 9090: "HTTP",
        9200: "Elasticsearch", 9300: "ES transport", 10000: "HTTP",
        11211: "Memcached", 15672: "RabbitMQ mgmt", 27017: "MongoDB",
        5000: "HTTP", 50070: "HDFS", 8848: "Nacos",
    }.get(port)
    return guess or "unknown"


def scan_ports(host: str, ports: Optional[List[int]] = None,
               timeout: float = 1.5, workers: int = 60) -> List[Dict[str, object]]:
    """并发 TCP 扫描 + banner。返回开放端口列表 [{port, service, banner}]。"""
    if not ports:
        ports = list(COMMON_PORTS)
    ports = list(dict.fromkeys(int(p) for p in ports))

    def _one(port: int):
        payload = _BANNER_PAYLOAD.get(port, _HTTP_PORTS_ALL.get(port, b""))
        open_, banner = _tcp_banner(host, port, timeout, payload)
        if open_:
            return {"port": port, "service": _guess_service(port, banner),
                    "banner": banner}
        return None

    results: List[Dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(_one, ports):
            if r is not None:
                results.append(r)
    results.sort(key=lambda d: int(d["port"]))
    return results
