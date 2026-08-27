"""基础设施渗透 · HTTP 服务指纹 + 敏感路径探测（深度渗透第二层）。

基于端口扫描结果，对开放的 HTTP/HTTPS 端口识别中间件/框架指纹，并探测
常见敏感路径（Actuator、备份文件、源码泄露、管理后台、文档接口等）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import httpx

# 常见敏感/泄露路径（参考 SRC 与攻防演练高频暴露面）
SENSITIVE_PATHS = [
    "/actuator", "/actuator/env", "/actuator/health", "/actuator/heapdump",
    "/actuator/mappings", "/admin", "/admin/login", "/.env", "/.git/config",
    "/.git/HEAD", "/backup.zip", "/backup.sql", "/db.sql", "/config.php",
    "/swagger-ui.html", "/swagger-ui/index.html", "/swagger/index.html",
    "/v2/api-docs", "/v3/api-docs", "/console", "/manager/html",
    "/status", "/server-status", "/.svn/entries", "/robots.txt",
    "/sitemap.xml", "/login", "/wp-login.php", "/druid/index.html",
    "/api-docs", "/actuator/configprops",
]

FRAMEWORK_MARKERS = [
    ("Spring Boot", ["spring", "actuator", "x-application-context", "whitelabel"]),
    ("Nginx", ["nginx"]),
    ("Apache HTTP", ["apache"]),
    ("Tomcat", ["tomcat", "coyote"]),
    ("IIS", ["microsoft-iis"]),
    ("Jetty", ["jetty"]),
    ("WebLogic", ["weblogic"]),
    ("JBoss", ["jboss", "wildfly"]),
    ("Shiro", ["shiro", "rememberme=deleteme"]),
    ("Django", ["django", "csrftoken"]),
    ("Flask", ["flask", "werkzeug"]),
    ("Node/Express", ["express", "x-powered-by: express"]),
    ("WordPress", ["wordpress", "wp-content"]),
]


def _detect_framework(headers: Dict[str, str], body: str) -> List[str]:
    blob = " ".join(f"{k}:{v}".lower() for k, v in headers.items())
    blob += " " + body[:4000].lower()
    found = []
    for name, markers in FRAMEWORK_MARKERS:
        if any(m in blob for m in markers):
            found.append(name)
    return found


def http_fingerprint(host: str, port: int, scheme: str = "http",
                     timeout: float = 5.0) -> Dict[str, object]:
    """对 http(s) 端口 GET / 做指纹识别。"""
    base = f"{scheme}://{host}:{port}"
    try:
        with httpx.Client(timeout=timeout, verify=False,
                          follow_redirects=False) as c:
            r = c.get(base + "/")
        headers = {k: v for k, v in r.headers.items()}
        body = (r.text or "")[:4000]
        title = ""
        if "<title" in body.lower():
            s = body.lower().find("<title")
            e = body.find("</title>")
            if e > s:
                title = body[s + 7:e].strip()[:80]
        server = headers.get("Server", "")
        xpb = headers.get("X-Powered-By", "")
        fw = _detect_framework(headers, body)
        # 后端框架确认：探测多个候选后端路径（SPA 兜底的 200/非 JSON 不报），
        # 命中 Spring Boot 默认 JSON 404（{timestamp,status,error,path}）
        for probe in ("/api/__probe_nonexistent_404",
                      "/__probe_nonexistent_404", "/api-docs"):
            try:
                rp = c.get(base + probe)
                if rp.status_code == 404 and (rp.text or "").strip().startswith("{"):
                    lowp = rp.text.lower()
                    if "timestamp" in lowp and "error" in lowp and "path" in lowp:
                        fw.append("Spring Boot (backend)")
                        break
            except Exception:
                continue
        return {
            "port": port, "scheme": scheme, "status": r.status_code,
            "server": server, "x_powered_by": xpb, "title": title,
            "frameworks": fw,
        }
    except Exception as exc:
        return {"port": port, "scheme": scheme, "error": str(exc)}


def probe_sensitive(host: str, port: int, scheme: str = "http",
                    paths: Optional[List[str]] = None,
                    timeout: float = 5.0) -> List[Dict[str, object]]:
    """探测敏感路径。返回 [(path, status, note)]，按风险排序。"""
    base = f"{scheme}://{host}:{port}"
    paths = paths or SENSITIVE_PATHS
    out: List[Dict[str, object]] = []
    try:
        with httpx.Client(timeout=timeout, verify=False,
                          follow_redirects=False) as c:
            # 首页 baseline：用于排除 SPA 单页兜底（history fallback 对任意
            # 路径返回同一 index.html，状态码 200 是假阳性）
            home = ""
            try:
                hr = c.get(base + "/")
                home = (hr.text or "")
            except Exception:
                pass
            for p in paths:
                try:
                    r = c.get(base + p)
                    body = r.text or ""
                    low = body.lower()
                    # SPA 兜底排除：200 且返回与首页完全相同的 HTML → 非真暴露
                    if home and body == home and r.status_code == 200:
                        continue
                    note = ""
                    if r.status_code in (200, 302):
                        is_json = body.strip().startswith(("{", "["))
                        if "actuator" in p:
                            # Actuator 必须是 JSON（含 _links/spring），HTML 兜底不算
                            note = ("Actuator 端点（可能泄露配置/环境变量）"
                                    if is_json else "")
                        elif p in ("/.git/HEAD", "/.git/config"):
                            note = ("源码仓库泄露"
                                    if "ref:" in low or "[core]" in low else "")
                        elif p.endswith((".zip", ".sql", ".php")):
                            note = "敏感文件"
                        elif p == "/.env":
                            # .env 需 key=value 文本，HTML 兜底不算
                            note = ("敏感文件 .env"
                                    if not low.startswith("<!doctype html")
                                    and ("=" in body or "key" in low) else "")
                        elif "swagger" in p or "api-docs" in p:
                            note = ("API 文档暴露"
                                    if is_json or "swagger" in low else "")
                        elif "admin" in p or p == "/console" or "manager" in p:
                            # 管理后台需非首页（非兜底已排除）才报
                            note = "管理后台"
                        elif "actuator/env" in p and r.status_code == 200 and is_json:
                            note = "配置泄露风险"
                    if r.status_code == 200 and note:
                        out.append({"path": p, "status": r.status_code,
                                    "note": note})
                    elif r.status_code in (401, 403) and "actuator" in p:
                        out.append({"path": p, "status": r.status_code,
                                    "note": "Actuator 可达（需认证）"})
                except Exception:
                    continue
    except Exception:
        pass
    return out
