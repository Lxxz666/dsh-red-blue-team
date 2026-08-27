"""深层次攻击 · 版本指纹 → 已知 CVE 精确匹配（基于 banner 版本号）。

对端口扫描得到的服务 + banner 版本，精确匹配公开已知高危 CVE（版本区间）。
这是对真实服务的"精确打击"——比泛路径检测更深入。

⚠️ 版本 CVE 匹配是**风险提示**（基于版本区间推断），不构成已利用确认；
实际可利用性取决于环境配置与补丁状态。
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

# ---- 版本比较工具 ----


def _norm(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4] or [0])


def _in_range(v: str, lo: str, hi: str) -> bool:
    return _norm(lo) <= _norm(v) <= _norm(hi)


def _ver_lt(v: str, hi: str) -> bool:
    return _norm(v) < _norm(hi)


def _ver_ge(v: str, lo: str) -> bool:
    return _norm(v) >= _norm(lo)


# ---- 服务 banner → (服务, 版本) 解析 ----


def parse_service_version(service: str, banner: str) -> Optional[Tuple[str, str]]:
    """从服务名 + banner 提取 (服务, 版本号)。返回 None 表示无法解析。"""
    b = banner
    if service == "SSH" or "openssh" in b.lower():
        m = re.search(r"OpenSSH_([\d.]+)", b)
        if m:
            return ("OpenSSH", m.group(1))
    if "nginx/" in b.lower():
        m = re.search(r"nginx/([\d.]+)", b.lower())
        if m:
            return ("Nginx", m.group(1))
    if "apache/" in b.lower():
        m = re.search(r"apache/([\d.]+)", b.lower())
        if m:
            return ("Apache", m.group(1))
    if "redis_version" in b.lower():
        m = re.search(r"redis_version[:\s]+([\d.]+)", b.lower())
        if m:
            return ("Redis", m.group(1))
    if service in ("Apache Tomcat",) or "tomcat" in b.lower() or "coyote" in b.lower():
        m = re.search(r"Tomcat/([\d.]+)", b, re.I)
        if m:
            return ("Tomcat", m.group(1))
    return None


# ---- 版本 → CVE 库（保守、区间明确、知名） ----
# (服务, 版本谓词, CVE, 风险, 说明)
VERSION_CVES: List[Tuple[str, Callable[[str], bool], str, str, str]] = [
    # Nginx
    ("Nginx", lambda v: _in_range(v, "0.6.18", "1.20.0"),
     "CVE-2021-23017", "high", "DNS resolver 堆溢出 RCE（影响 ≤1.20.0）"),
    ("Nginx", lambda v: _in_range(v, "1.21.0", "1.23.2"),
     "CVE-2022-41742", "high", "mp4 模块内存泄露"),
    # Apache HTTP
    ("Apache", lambda v: v.startswith("2.4.49"),
     "CVE-2021-41773", "critical", "路径穿越 + RCE（2.4.49）"),
    ("Apache", lambda v: v.startswith("2.4.50"),
     "CVE-2021-42013", "critical", "路径穿越绕过 + RCE（2.4.50）"),
    # OpenSSH
    ("OpenSSH", lambda v: _in_range(v, "8.5", "9.7"),
     "CVE-2024-6387", "critical", "regreSSHion 信号竞争 RCE（8.5p1-9.7p1）"),
    ("OpenSSH", lambda v: _in_range(v, "2.0", "7.7"),
     "CVE-2018-15473", "high", "用户名枚举（≤7.7）"),
    # Redis
    ("Redis", lambda v: _in_range(v, "0.0", "5.0.14"),
     "CVE-2015-4335", "high", "EVAL Lua sandbox 逃逸（≤5.0.x）"),
    # Tomcat
    ("Tomcat", lambda v: _in_range(v, "7.0.0", "9.0.0") or _in_range(v, "0.0", "7.0.108"),
     "CVE-2017-12615", "critical", "PUT 方法任意写 JSP（影响特定配置）"),
    ("Tomcat", lambda v: _in_range(v, "8.5.0", "8.5.50"),
     "CVE-2020-1938", "critical", "AJP 文件读取/包含 Ghostcat"),
]


def match_version_cves(service: str, version: str) -> List[Dict[str, str]]:
    """按 (服务, 版本) 匹配已知 CVE。"""
    out: List[Dict[str, str]] = []
    for svc, pred, cve, risk, desc in VERSION_CVES:
        if svc == service and pred(version):
            out.append({"cve": cve, "risk": risk, "detail": desc,
                        "service": service, "version": version})
    return out


def run_version_cves(open_ports: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """对端口扫描结果（service+banner）跑版本 CVE 匹配。"""
    out: List[Dict[str, object]] = []
    for p in open_ports:
        service = str(p.get("service", ""))
        banner = str(p.get("banner", ""))
        parsed = parse_service_version(service, banner)
        if parsed is None:
            continue
        svc, ver = parsed
        for hit in match_version_cves(svc, ver):
            out.append({"port": p.get("port"), **hit})
    return out


def summarize_version_cves(checks: List[Dict[str, object]]) -> str:
    if not checks:
        return "版本 CVE 精确匹配未命中"
    lines = [f"版本CVE精确匹配命中 {len(checks)} 项"]
    for c in checks:
        lines.append(f"  - [{c.get('risk')}] :{c.get('port')} "
                     f"{c.get('service')} {c.get('version')} → {c.get('cve')} "
                     f"｜ {c.get('detail','')}")
    return "\n".join(lines)
