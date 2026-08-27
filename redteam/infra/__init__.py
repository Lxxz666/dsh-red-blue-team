"""基础设施/服务器深度渗透模块。

从应用层接口测试升级到**服务器层面渗透**（参考攻防演练 / SRC 常见暴露面）：

1. **信息收集**（``scanner.scan_ports``）：TCP 端口扫描 + banner 抓取 + 服务识别
2. **服务指纹**（``fingerprint``）：HTTP/HTTPS 中间件/框架指纹 + 敏感路径探测
3. **未授权/高危检测**（``vuln_check``）：Redis/MongoDB/Memcached/Docker API/
   Elasticsearch/Nacos 未授权 + Spring Actuator 泄露 + Shiro 指纹

全部**只读/被动**探测，不执行破坏性操作。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .fingerprint import http_fingerprint, probe_sensitive
from .scanner import COMMON_PORTS, scan_ports
from .vuln_check import run_vuln_checks

# HTTP(S) 端口：做指纹 + 敏感路径探测
_HTTP_PORTS = {80, 443, 8080, 8081, 8088, 8443, 8888, 9000, 8000, 8001,
               7001, 8161, 10080, 2375, 9200, 8848}


def infra_scan(host: str, ports: Optional[List[int]] = None,
               timeout: float = 1.5, max_sensitive: int = 40) -> Dict[str, object]:
    """三层深度渗透编排，返回结构化结果。

    :param host: 目标 IP 或域名
    :param ports: 指定端口（默认 COMMON_PORTS）
    :param max_sensitive: 敏感路径探测上限（默认 40 个端口×路径，防超时）
    """
    open_ports = scan_ports(host, ports, timeout=timeout)
    open_nums = [int(p["port"]) for p in open_ports]

    # 第二层：HTTP 服务指纹 + 敏感路径
    fingerprints: List[Dict[str, object]] = []
    sensitive: List[Dict[str, object]] = []
    http_ports = [n for n in open_nums if n in _HTTP_PORTS]
    budget = max_sensitive
    for port in http_ports:
        scheme = "https" if port == 443 else "http"
        fp = http_fingerprint(host, port, scheme)
        fingerprints.append(fp)
        if not fp.get("error") and budget > 0:
            # 每个端口探测 <=8 个路径，总量受 budget 限制
            found = probe_sensitive(host, port, scheme)[:8]
            sensitive.extend(found)
            budget -= len(found)

    # 第三层：未授权/高危检测
    vuln = run_vuln_checks(host, open_nums)

    return {
        "host": host,
        "open_ports": open_ports,
        "fingerprints": fingerprints,
        "sensitive_paths": sensitive,
        "vuln_checks": vuln,
    }


def summarize(result: Dict[str, object]) -> str:
    """把 infra_scan 结果压缩成可读文本（供 LLM / CLI / 报告用）。"""
    lines: List[str] = []
    open_ports = result.get("open_ports") or []
    lines.append(f"[端口扫描] {result.get('host')} 开放 {len(open_ports)} 个端口")
    for p in open_ports:
        lines.append(f"  - :{p['port']} {p['service']}"
                     + (f" ｜ {p['banner'][:50]}" if p.get("banner") else ""))
    fps = result.get("fingerprints") or []
    for fp in fps:
        if fp.get("error"):
            continue
        fw = "、".join(fp.get("frameworks") or []) or "-"
        lines.append(f"[指纹] :{fp['port']} {fp.get('server','')} "
                     f"title={fp.get('title','')} 框架={fw}")
    sens = result.get("sensitive_paths") or []
    if sens:
        lines.append(f"[敏感路径] 命中 {len(sens)} 处")
        for s in sens[:10]:
            lines.append(f"  - {s.get('path')} HTTP {s.get('status')} {s.get('note','')}")
    vulns = [v for v in (result.get("vuln_checks") or [])
             if v.get("status") == "vuln"]
    if vulns:
        lines.append(f"[未授权/高危] 命中 {len(vulns)} 项")
        for v in vulns:
            lines.append(f"  - {v.get('check')} ｜ {v.get('detail','')}")
    else:
        lines.append("[未授权/高危] 未命中")
    return "\n".join(lines)
