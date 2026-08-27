"""基础设施/服务器深度渗透模块测试（redteam.infra）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from redteam.infra import infra_scan, scan_ports, summarize
from redteam.infra.fingerprint import _detect_framework
from redteam.infra.scanner import _guess_service


def test_scan_ports_localhost_ok():
    """对无开放端口目标扫描不崩，返回列表。"""
    r = scan_ports("127.0.0.1", ports=[22, 80, 443], timeout=0.5)
    assert isinstance(r, list)
    for p in r:
        assert "port" in p and "service" in p


def test_infra_scan_structure():
    """infra_scan 返回完整分层结构 + summarize 可读。"""
    r = infra_scan("127.0.0.1", ports=[22, 80], timeout=0.5)
    assert set(r.keys()) >= {
        "host", "open_ports", "fingerprints", "sensitive_paths", "vuln_checks",
    }
    assert isinstance(summarize(r), str)
    assert "端口扫描" in summarize(r)


def test_guess_service():
    """服务猜测：banner 特征识别。"""
    assert _guess_service(22, "SSH-2.0-OpenSSH_8.9") == "SSH"
    assert _guess_service(6379, "+PONG") == "Redis"
    assert _guess_service(80, "Server: nginx/1.24") == "Nginx"
    assert "Tomcat" in _guess_service(8080, "Apache-Coyote/1.1")
    assert "MySQL" in _guess_service(3306, "8.0.33-log")


def test_detect_framework():
    """框架指纹识别。"""
    assert "Spring Boot" in _detect_framework(
        {"X-Application-Context": "app"}, "Whitelabel Error Page")
    assert "Nginx" in _detect_framework({"Server": "nginx/1.24"}, "")
    assert "Shiro" in _detect_framework(
        {"Set-Cookie": "rememberMe=deleteMe"}, "")
