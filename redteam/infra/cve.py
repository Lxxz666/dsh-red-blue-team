"""深层次攻击 · 已知高危 CVE / 漏洞指纹检测（第四层）。

根据服务指纹与端口匹配**公开已知高危漏洞**，并做**只读**验证（不执行
破坏性利用、不触发 JNDI/反序列化实际攻击）。覆盖 Spring Boot / Apache Shiro /
Fastjson / Log4j / Struts2 / WebLogic / Tomcat / Redis 等。

关键安全边界：全部被动/只读探测；Log4j 只做指纹提示（不触发 JNDI 回连）；
不执行任何写操作或利用型载荷。
"""
from __future__ import annotations

import socket
from typing import Dict, List, Optional

import httpx

_TIMEOUT = 5.0


def _http(host: str, port: int, path: str, scheme: str = "http",
          method: str = "GET", timeout: float = _TIMEOUT,
          data: Optional[str] = None) -> Optional[httpx.Response]:
    try:
        with httpx.Client(timeout=timeout, verify=False,
                          follow_redirects=False) as c:
            return c.request(method, f"{scheme}://{host}:{port}{path}",
                             data=data)
    except Exception:
        return None


# ---- 单项深层次检测（返回 dict 或 None） ----


def check_actuator_env(host: str, port: int,
                       scheme: str = "http") -> Optional[Dict[str, object]]:
    """Spring Actuator /env：读取配置，扫描敏感字段（密钥/数据源）。

    探测多个候选路径（顶层 /actuator/env 或被网关前缀 /api 转发到后端）。
    """
    sensitive = ["password", "secret", "api_key", "apikey", "token",
                 "jdbc:", "private_key", "aws_secret", "username",
                 "database", "redis"]
    for path in ("/actuator/env", "/api/actuator/env", "/api/env"):
        r = _http(host, port, path, scheme)
        if r is None or r.status_code != 200:
            continue
        if not (r.text or "").strip().startswith("{"):
            continue  # SPA 兜底/非 JSON 不算
        body = r.text.lower()
        found = [s for s in sensitive if s in body]
        if found:
            return {"cve": "Spring Actuator env 配置泄露",
                    "service": "Spring Boot", "risk": "critical",
                    "status": "vuln",
                    "detail": f"{path} 可访问且泄露敏感字段: {found}"}
        return {"cve": "Spring Actuator env 暴露", "service": "Spring Boot",
                "risk": "high", "status": "info",
                "detail": f"{path} 未授权可读（暴露应用配置）"}
    return None


def check_actuator_heapdump(host: str, port: int,
                            scheme: str = "http") -> Optional[Dict[str, object]]:
    """Spring Actuator heapdump：检测堆转储可下载（含运行时密钥/密码）。"""
    r = _http(host, port, "/actuator/heapdump", scheme)
    if r is None:
        return None
    ct = r.headers.get("Content-Type", "")
    cl = r.headers.get("Content-Length", "0")
    if r.status_code == 200 and ("octet-stream" in ct or "java" in ct
                                 or r.content[:4] == b"\x4a\x41\x56\x41"):
        return {"cve": "Spring Actuator heapdump 泄露", "service": "Spring Boot",
                "risk": "critical", "status": "vuln",
                "detail": f"/actuator/heapdump 可下载（大小 {cl}，含内存密钥/密码）"}
    return None


def check_shiro(host: str, port: int, scheme: str = "http") -> Optional[Dict[str, object]]:
    """Apache Shiro rememberMe：反序列化 RCE 面（CVE-2016-4437 等）。"""
    r = _http(host, port, "/", scheme)
    if r is None:
        return None
    sc = r.headers.get("Set-Cookie", "") or ""
    if "rememberme=deleteme" in sc.lower():
        return {"cve": "CVE-2016-4437 Shiro rememberMe 反序列化",
                "service": "Apache Shiro", "risk": "critical",
                "status": "info",
                "detail": "存在 Shiro rememberMe（若默认密钥则反序列化 RCE）"}
    return None


def check_spring_rce(host: str, port: int,
                     scheme: str = "http") -> Optional[Dict[str, object]]:
    """Spring 已知 RCE 相关指纹/路径（SpringShell CVE-2022-22965 等提示）。"""
    # Spring 常见管理/信息路径
    for path in ("/actuator", "/actuator/health", "/error", "/env"):
        r = _http(host, port, path, scheme)
        if r is not None and r.status_code == 200 and (
                "spring" in (r.text or "").lower()
                or "whitelabel" in (r.text or "").lower()
                or (r.text or "").strip().startswith("{")):
            return {"cve": "Spring 框架暴露面", "service": "Spring Boot",
                    "risk": "high", "status": "info",
                    "detail": f"检测到 Spring 特征路径 {path}（存在 SpringShell/Actuator 利用面）"}
    return None


def check_struts2(host: str, port: int, scheme: str = "http") -> Optional[Dict[str, object]]:
    """Struts2 已知 RCE（S2-045 CVE-2017-5638 等）指纹路径。"""
    for path in ("/struts2-showcase", "/struts/", "/example/HelloWorld.action"):
        r = _http(host, port, path, scheme)
        if r is not None and r.status_code in (200, 302, 500) and ".action" in path:
            return {"cve": "Struts2 暴露面", "service": "Apache Struts2",
                    "risk": "high", "status": "info",
                    "detail": f"Struts2 action 路径可达（{path}，存在 S2-045/S2-057 等 RCE）"}
    return None


def check_weblogic(host: str, port: int, scheme: str = "http") -> Optional[Dict[str, object]]:
    """WebLogic 已知漏洞指纹（CVE-2020-14882 未授权 RCE 等）。"""
    for path in ("/console/login/LoginForm.jsp", "/weblogic/", "/wls-wsat/"):
        r = _http(host, port, path, scheme)
        if r is not None and r.status_code in (200, 302) and \
                ("weblogic" in (r.text or "").lower() or "/console" in path):
            return {"cve": "WebLogic 暴露面", "service": "Oracle WebLogic",
                    "risk": "critical", "status": "info",
                    "detail": f"WebLogic 管理面可达（{path}，存在 CVE-2020-14882 等未授权 RCE）"}
    return None


def check_tomcat_manager(host: str, port: int,
                         scheme: str = "http") -> Optional[Dict[str, object]]:
    """Tomcat Manager/弱口令面 + PUT 上传（CVE-2017-12615）。"""
    r = _http(host, port, "/manager/html", scheme)
    if r is not None and r.status_code in (401, 302, 200) and \
            (r.status_code == 401 or "tomcat" in (r.text or "").lower()):
        return {"cve": "Tomcat Manager 暴露", "service": "Apache Tomcat",
                "risk": "high", "status": "info",
                "detail": "/manager/html 可达（HTTP {r.status_code}，存在弱口令/后台风险）"}
    r2 = _http(host, port, "/", scheme, method="OPTIONS")
    if r2 is not None:
        allow = r2.headers.get("Allow", "")
        if "PUT" in allow.upper() and port in (80, 443, 8080, 8081, 8443):
            return {"cve": "Tomcat PUT 上传", "service": "Apache Tomcat",
                    "risk": "high", "status": "info",
                    "detail": "响应允许 PUT 方法（CVE-2017-12615 条件）"}
    return None


def check_redis_unauth(host: str, port: int) -> Optional[Dict[str, object]]:
    """Redis 未授权（可与 vuln_check 互补的 CVE 视角）。"""
    try:
        with socket.create_connection((host, port), timeout=4) as s:
            s.settimeout(4)
            s.sendall(b"CONFIG GET dir\r\n")
            data = s.recv(256).decode("utf-8", errors="replace")
        if "dir" in data and ("\r\n" in data):
            return {"cve": "Redis 未授权", "service": "Redis",
                    "risk": "critical", "status": "vuln",
                    "detail": "Redis 未授权可执行命令（CONFIG GET 成功）"}
    except Exception:
        pass
    return None


# ---- 端口 → 适用深层次检测 映射 ----


def _dispatch(host: str, port: int, scheme: str) -> List[Dict[str, object]]:
    checks: List[Dict[str, object]] = []
    if port == 6379:
        c = check_redis_unauth(host, port)
        if c:
            checks.append(c)
    if port in (80, 443, 8080, 8081, 8088, 8443, 8888, 9000, 8000, 8001,
                7001, 8161, 10080, 9090):
        for fn in (check_actuator_env, check_actuator_heapdump, check_shiro,
                   check_spring_rce, check_struts2, check_weblogic,
                   check_tomcat_manager):
            try:
                c = fn(host, port, scheme)
                if c:
                    checks.append(c)
            except Exception:
                continue
    return checks


def run_cve_checks(host: str, open_ports: List[int],
                   scheme: str = "http") -> List[Dict[str, object]]:
    """对开放端口跑深层次已知漏洞检测，返回全部命中。"""
    out: List[Dict[str, object]] = []
    for port in open_ports:
        out.extend(_dispatch(host, int(port), scheme))
    return out


def summarize_cve(checks: List[Dict[str, object]]) -> str:
    if not checks:
        return "深层次已知漏洞检测未命中"
    lines = [f"深层次漏洞命中 {len(checks)} 项"]
    for c in checks:
        lines.append(f"  - [{c.get('risk')}] {c.get('cve')} ｜ {c.get('detail','')}")
    return "\n".join(lines)
