"""弱口令检测模块测试（redteam.infra.bruteforce，opt-in）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import redteam.infra.bruteforce as bf


def test_redis_weak_hit(monkeypatch):
    """Redis 弱口令命中。"""
    monkeypatch.setattr(bf, "check_redis_password", lambda *a, **k: True)
    r = bf.brute_redis("x", 6379)
    assert r["status"] == "vuln"
    assert "credential" in r


def test_redis_safe(monkeypatch):
    """Redis 常见弱口令未命中。"""
    monkeypatch.setattr(bf, "check_redis_password", lambda *a, **k: False)
    r = bf.brute_redis("x", 6379)
    assert r["status"] == "safe"


def test_check_redis_password_empty_unauth(monkeypatch):
    """空密码 = 未授权（PING +PONG）。"""
    import socket
    class _Fake:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def sendall(self, d): pass
        def recv(self, n): return b"+PONG\r\n"
        def settimeout(self, t): pass
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _Fake())
    assert bf.check_redis_password("x", 6379, "") is True


def test_summarize_empty():
    assert "未命中" in bf.summarize_brute([{"status": "safe"}])


def test_summarize_hit():
    s = bf.summarize_brute([{"status": "vuln", "service": "Redis",
                             "credential": "admin", "detail": "x"}])
    assert "弱口令" in s and "admin" in s


def test_run_brute_skips_non_target():
    """run_brute 只对 Redis/HTTP 端口跑。"""
    r = bf.run_brute("x", [22, 3306])  # SSH/MySQL 不在零依赖范围内
    assert r == []
