"""redteam.storage —— SQLite 存储服务（扫描/攻击结果/漏洞/修复/自适应地形）。

基于 dsh Service 形态挂载（``ctx.storage``），写操作经 asyncio.to_thread
落盘，不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from dsh.kernel import Service

from .errors import StorageError
from .models import Finding, ScanResult, VerdictResult, now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id TEXT UNIQUE, target TEXT, profile TEXT, mode TEXT,
  started_at TEXT, finished_at TEXT, status TEXT,
  total_samples INT DEFAULT 0, success_count INT DEFAULT 0,
  suspicious_count INT DEFAULT 0,
  report_path TEXT, report_json_path TEXT, audit_path TEXT,
  probe TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS attack_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id TEXT, sample_uid TEXT, sample_id TEXT, category TEXT,
  role TEXT, surface TEXT, verdict TEXT, confidence REAL,
  evidence TEXT, response TEXT, signals TEXT, chain TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_attack_scan ON attack_results(scan_id);
CREATE INDEX IF NOT EXISTS idx_attack_uid ON attack_results(sample_uid);

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id TEXT UNIQUE, scan_id TEXT, category TEXT, owasp TEXT,
  severity TEXT, sample_id TEXT, sample_uid TEXT, payload TEXT,
  role TEXT, chain TEXT, evidence TEXT, signals TEXT,
  confidence REAL, fix_status TEXT DEFAULT 'pending',
  fix_template TEXT DEFAULT '', fix_plan TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_finding_scan ON findings(scan_id);

CREATE TABLE IF NOT EXISTS fixes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fix_id TEXT UNIQUE, finding_id TEXT, plan_id TEXT, status TEXT,
  applied_to TEXT, backup TEXT, detail TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS regressions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id TEXT, passed INT, detail TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS terrain_state (
  domain TEXT PRIMARY KEY, snapshot TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT
);
"""


class StorageService(Service):
    """SQLite 存储（ctx.storage）。"""

    provides = "storage"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.db_path = os.path.abspath((config or {}).get("db_path", "./redteam.db"))
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    def apply(self, ctx) -> None:
        ctx.set("storage", self)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.commit()
                self._conn.close()
            self._conn = None

    # ---- 通用执行 ----

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- 扫描 ----

    def new_scan(self, scan_id: str, target: str, profile: str,
                 mode: str, started_at: str) -> None:
        self._execute(
            "INSERT INTO scans (scan_id, target, profile, mode, started_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (scan_id, target, profile, mode, started_at))

    def finish_scan(self, scan_id: str, finished_at: str, total: int,
                    success: int, suspicious: int, report_path: str,
                    report_json_path: str, audit_path: str,
                    probe: Dict[str, Any]) -> None:
        self._execute(
            "UPDATE scans SET finished_at=?, status='finished', total_samples=?, "
            "success_count=?, suspicious_count=?, report_path=?, report_json_path=?, "
            "audit_path=?, probe=? WHERE scan_id=?",
            (finished_at, total, success, suspicious, report_path,
             report_json_path, audit_path, json.dumps(probe, ensure_ascii=False),
             scan_id))

    def fail_scan(self, scan_id: str, reason: str) -> None:
        self._execute(
            "UPDATE scans SET status=?, finished_at=? WHERE scan_id=?",
            (f"failed: {reason}"[:200], now_iso(), scan_id))

    def get_scan(self, scan_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM scans WHERE scan_id=?", (scan_id,))
        return dict(rows[0]) if rows else None

    def list_scans(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT scan_id, target, profile, started_at, finished_at, status, "
            "total_samples, success_count, suspicious_count, report_path "
            "FROM scans ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---- 攻击结果 ----

    def record_attack(self, scan_id: str, result: VerdictResult,
                      response_text: str) -> None:
        self._execute(
            "INSERT INTO attack_results (scan_id, sample_uid, sample_id, category, "
            "role, surface, verdict, confidence, evidence, response, signals, chain, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, result.sample_uid, result.sample_uid.split("-")[0],
             result.category, result.role, "", result.verdict, result.confidence,
             result.evidence[:2000], response_text[:8000],
             json.dumps([s.__dict__ for s in result.signals], ensure_ascii=False),
             json.dumps(result.chain, ensure_ascii=False),
             result.created_at or now_iso()))

    def attacks_for_scan(self, scan_id: str) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM attack_results WHERE scan_id=? ORDER BY id", (scan_id,))
        return [dict(r) for r in rows]

    def attacks_for_samples(self, scan_id: str,
                            sample_uids: List[str]) -> List[Dict[str, Any]]:
        if not sample_uids:
            return []
        placeholders = ",".join("?" for _ in sample_uids)
        rows = self._query(
            f"SELECT * FROM attack_results WHERE scan_id=? AND sample_uid IN "
            f"({placeholders}) ORDER BY id", (scan_id, *sample_uids))
        return [dict(r) for r in rows]

    # ---- 漏洞发现 ----

    def add_finding(self, finding: Finding) -> None:
        self._execute(
            "INSERT OR REPLACE INTO findings (finding_id, scan_id, category, owasp, "
            "severity, sample_id, sample_uid, payload, role, chain, evidence, signals, "
            "confidence, fix_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'pending')",
            (finding.finding_id, finding.scan_id, finding.category, finding.owasp,
             finding.severity, finding.sample_id, finding.sample_uid, finding.payload,
             finding.role, json.dumps(finding.chain, ensure_ascii=False),
             finding.evidence[:4000],
             json.dumps(finding.signals, ensure_ascii=False), finding.confidence))

    def findings_for_scan(self, scan_id: str) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM findings WHERE scan_id=? ORDER BY severity DESC, id",
            (scan_id,))
        return [dict(r) for r in rows]

    def update_finding_fix(self, finding_id: str, fix_template: str,
                           fix_plan: str, status: str = "planned") -> None:
        self._execute(
            "UPDATE findings SET fix_template=?, fix_plan=?, fix_status=? "
            "WHERE finding_id=?",
            (fix_template, fix_plan, status, finding_id))

    def mark_finding_verified(self, finding_id: str) -> None:
        self._execute(
            "UPDATE findings SET fix_status='verified' WHERE finding_id=?",
            (finding_id,))

    # ---- 修复与回归 ----

    def add_fix(self, fix_id: str, finding_id: str, plan_id: str, status: str,
                applied_to: str, backup: str, detail: str) -> None:
        self._execute(
            "INSERT INTO fixes (fix_id, finding_id, plan_id, status, applied_to, "
            "backup, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fix_id, finding_id, plan_id, status, applied_to, backup, detail,
             now_iso()))

    def add_regression(self, finding_id: str, passed: bool, detail: str) -> None:
        self._execute(
            "INSERT INTO regressions (finding_id, passed, detail, created_at) "
            "VALUES (?, ?, ?, ?)", (finding_id, 1 if passed else 0, detail,
                                    now_iso()))

    # ---- 自适应地形 ----

    def save_terrain(self, domain: str, snapshot: Dict[str, Any]) -> None:
        self._execute(
            "INSERT OR REPLACE INTO terrain_state (domain, snapshot, updated_at) "
            "VALUES (?, ?, ?)",
            (domain, json.dumps(snapshot, ensure_ascii=False), now_iso()))

    def load_terrain(self, domain: str) -> Optional[Dict[str, Any]]:
        rows = self._query(
            "SELECT snapshot FROM terrain_state WHERE domain=?", (domain,))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["snapshot"])
        except json.JSONDecodeError:
            return None

    # ---- 元数据 ----

    def set_meta(self, key: str, value: str) -> None:
        self._execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                      (key, value))

    def get_meta(self, key: str) -> Optional[str]:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return str(rows[0]["value"]) if rows else None
