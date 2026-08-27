"""深层次已知 CVE 检测测试（redteam.infra.cve）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

import redteam.infra.cve as cve_mod
from redteam.infra.cve import (check_actuator_env, check_actuator_heapdump,
                               check_shiro, summarize_cve)


def _resp(status=200, text="", headers=None, content=b"", json=None):
    if json is not None:
        return httpx.Response(status, json=json, headers=headers or {})
    if content:
        return httpx.Response(status, content=content, headers=headers or {})
    return httpx.Response(status, text=text, headers=headers or {})


def test_actuator_env_leak(monkeypatch):
    """Actuator /env 泄露密码 → critical vuln。"""
    monkeypatch.setattr(
        cve_mod, "_http",
        lambda *a, **k: _resp(200, text='{"propertySources":[{"properties":'
                                      '{"spring.datasource.password":{"value":"x"}}}]}'))
    r = check_actuator_env("x", 8080)
    assert r is not None and r["status"] == "vuln"
    assert "password" in r["detail"]


def test_actuator_env_safe_html(monkeypatch):
    """SPA 兜底返回 HTML → 不命中（防误报）。"""
    monkeypatch.setattr(
        cve_mod, "_http",
        lambda *a, **k: _resp(200, text="<!DOCTYPE html><html>Vite</html>"))
    assert check_actuator_env("x", 8080) is None


def test_actuator_heapdump(monkeypatch):
    """heapdump 可下载（Java 魔数）→ critical。"""
    monkeypatch.setattr(
        cve_mod, "_http",
        lambda *a, **k: _resp(200, content=b"JAVA...",
                              headers={"Content-Type": "application/octet-stream"}))
    r = check_actuator_heapdump("x", 8080)
    assert r is not None and r["status"] == "vuln"


def test_shiro_rememberme(monkeypatch):
    """Shiro rememberMe=deleteme → 反序列化面。"""
    monkeypatch.setattr(
        cve_mod, "_http",
        lambda *a, **k: _resp(200, text="ok",
                              headers={"Set-Cookie": "rememberMe=deleteMe; Path=/"}))
    r = check_shiro("x", 80)
    assert r is not None and "CVE-2016-4437" in r["cve"]


def test_shiro_absent(monkeypatch):
    """无 rememberMe → 不命中。"""
    monkeypatch.setattr(cve_mod, "_http", lambda *a, **k: _resp(200, text="ok"))
    assert check_shiro("x", 80) is None


def test_summarize_cve_empty():
    assert "未命中" in summarize_cve([])


def test_summarize_cve_hits():
    s = summarize_cve([{"risk": "critical", "cve": "CVE-2016-4437",
                        "detail": "Shiro 反序列化"}])
    assert "CVE-2016-4437" in s and "critical" in s
