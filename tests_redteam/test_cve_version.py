"""版本指纹 → 已知 CVE 精确匹配测试（redteam.infra.cve_version）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from redteam.infra.cve_version import (match_version_cves,
                                       parse_service_version,
                                       run_version_cves, summarize_version_cves)


def test_parse_openssh():
    assert parse_service_version("SSH", "SSH-2.0-OpenSSH_8.0") == ("OpenSSH", "8.0")


def test_parse_nginx():
    assert parse_service_version("Nginx", "nginx/1.19.4 ...") == ("Nginx", "1.19.4")


def test_parse_none():
    assert parse_service_version("HTTP", "") is None


def test_nginx_cve_hit():
    """Nginx 1.19.4（在 0.6.18-1.20.0 区间）命中 CVE-2021-23017。"""
    hits = match_version_cves("Nginx", "1.19.4")
    assert any(h["cve"] == "CVE-2021-23017" for h in hits)


def test_openssh_90_regression_hit():
    """OpenSSH 9.0（在 8.5-9.7 区间）命中 CVE-2024-6387 regreSSHion。"""
    hits = match_version_cves("OpenSSH", "9.0")
    assert any(h["cve"] == "CVE-2024-6387" for h in hits)


def test_openssh_80_no_regression():
    """OpenSSH 8.0 不在 8.5-9.7 区间 → 不误报 CVE-2024-6387。"""
    hits = match_version_cves("OpenSSH", "8.0")
    assert not any(h["cve"] == "CVE-2024-6387" for h in hits)


def test_apache_249_cve():
    hits = match_version_cves("Apache", "2.4.49")
    assert any(h["cve"] == "CVE-2021-41773" for h in hits)


def test_run_version_cves_from_ports():
    ports = [{"port": 22, "service": "SSH",
              "banner": "SSH-2.0-OpenSSH_9.0"},
             {"port": 80, "service": "Nginx", "banner": "nginx/1.19.4"}]
    hits = run_version_cves(ports)
    cves = {h["cve"] for h in hits}
    assert "CVE-2024-6387" in cves and "CVE-2021-23017" in cves


def test_summarize_empty():
    assert "未命中" in summarize_version_cves([])
