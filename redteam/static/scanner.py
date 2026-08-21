"""redteam.static.scanner —— 静态扫描器（文件夹模式输入）。

对本地项目文件夹做代码级安全审计：逐文件规则匹配（file:line 证据）、
敏感文件检测、依赖 CVE-lite 比对，输出结构化 StaticFinding。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..blueteam.templates import fix_template_for
from . import templates  # noqa: F401  (注册静态修复模板)
from .rules import CVE_LITE, RULES, SENSITIVE_FILES

log = logging.getLogger("redteam.static")

IGNORE_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__",
               ".idea", ".vscode", "dist", "build", ".pytest_cache",
               "target", ".gradle"}
MAX_FILE_SIZE = 256 * 1024
MAX_FILES = 2000
MAX_LINE = 300


@dataclass
class StaticFinding:
    rule_id: str
    category: str
    severity: str
    title: str
    file: str                       # 相对路径
    line: int = 0
    snippet: str = ""
    evidence: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"rule_id": self.rule_id, "category": self.category,
                "severity": self.severity, "title": self.title,
                "file": self.file, "line": self.line,
                "snippet": self.snippet, "evidence": self.evidence}


class StaticScanner:
    """静态扫描器（规则引擎）。"""

    def __init__(self, extra_rules: Optional[List[Any]] = None) -> None:
        self.rules = list(RULES) + list(extra_rules or [])

    def scan(self, folder: str, max_files: int = MAX_FILES) -> List[StaticFinding]:
        folder = os.path.abspath(folder)
        if not os.path.isdir(folder):
            raise ValueError(f"文件夹不存在: {folder}")
        findings: List[StaticFinding] = []
        compiled = [(rule, re.compile(rule.pattern)) for rule in self.rules]
        file_count = 0

        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for name in sorted(files):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, folder).replace("\\", "/")
                # 敏感文件规则（文件名匹配）
                for pattern, category, severity, hint in SENSITIVE_FILES:
                    if pattern.search(rel):
                        findings.append(StaticFinding(
                            rule_id="st-file", category=category,
                            severity=severity, title=hint, file=rel,
                            evidence=f"敏感文件: {rel}"))
                # 依赖清单 CVE-lite 比对
                if name in ("requirements.txt", "pyproject.toml"):
                    findings.extend(self._scan_dependencies(path, rel))
                if not self._match_globs(name):
                    continue
                if os.path.getsize(path) > MAX_FILE_SIZE:
                    continue
                try:
                    with open(path, "r", encoding="utf-8",
                              errors="ignore") as fh:
                        lines = fh.read().splitlines()
                except OSError:
                    continue
                file_count += 1
                if file_count > max_files:
                    log.info("静态扫描达到文件上限 %d，停止", max_files)
                    return findings
                for rule, regex in compiled:
                    if not rule.file_globs or not any(
                            name.endswith(g) for g in rule.file_globs):
                        continue
                    for index, line in enumerate(lines[:MAX_LINE], start=1):
                        if regex.search(line):
                            findings.append(StaticFinding(
                                rule_id=rule.rule_id, category=rule.category,
                                severity=rule.severity, title=rule.title,
                                file=rel, line=index,
                                snippet=line.strip()[:160],
                                evidence=f"{rel}:{index}: {rule.hint}"))
        return findings

    # ---- 依赖 CVE-lite ----

    _REQ_LINE = re.compile(
        r"^\s*([A-Za-z0-9_.\-]+)\s*([<>=!~]=?|==|~=)\s*([0-9][A-Za-z0-9_.\-]*)")

    def _scan_dependencies(self, path: str, rel: str) -> List[StaticFinding]:
        """requirements.txt / pyproject.toml 依赖版本 CVE-lite 比对。"""
        findings: List[StaticFinding] = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError:
            return findings
        for line_index, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            match = self._REQ_LINE.search(stripped)
            if not match:
                continue
            package = match.group(1).lower()
            operator = match.group(2)
            version = match.group(3)
            if package not in CVE_LITE:
                continue
            fixed, hint = CVE_LITE[package]
            if _version_lt(version, fixed) or (
                    operator not in ("==", "===") and
                    operator in ("<", "<=")):
                findings.append(StaticFinding(
                    rule_id="st-012", category="dependency_vuln",
                    severity="high",
                    title=f"依赖 {package} 版本含已知漏洞（CVE-lite）",
                    file=rel, line=line_index,
                    snippet=stripped[:160],
                    evidence=f"{rel}:{line_index}: {package} {version} → {hint}"))
        return findings

    @staticmethod
    def _match_globs(name: str) -> bool:
        return not name.startswith(".") and not name.endswith(
            (".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff",
             ".woff2", ".ttf", ".zip", ".tar", ".gz", ".lock", ".min.js"))


def _version_lt(a: str, b: str) -> bool:
    """简易语义版本比较（去预发布后缀）。"""

    def parts(v: str):
        out = []
        for chunk in v.split("."):
            digits = re.match(r"\d+", chunk)
            out.append(int(digits.group(0)) if digits else 0)
        return out

    return parts(a) < parts(b)


def findings_to_model(findings: List[StaticFinding], scan_id: str
                      ) -> List[Any]:
    """StaticFinding → models.Finding（静态发现并入统一报告/修复工单）。"""
    from ..models import Finding
    out = []
    seq = 0
    for item in findings:
        seq += 1
        template = fix_template_for(item.category)
        out.append(Finding(
            finding_id=f"S-{seq:03d}",
            scan_id=scan_id,
            category=item.category,
            owasp=item.title,
            severity=item.severity,
            sample_id=item.rule_id,
            sample_uid=f"{item.rule_id}-{item.file.replace('/', '_')}-{item.line}",
            payload="",
            role="static",
            evidence=item.evidence,
            signals={"static_rule": {"hit": True,
                                     "evidence": item.evidence}},
            confidence=0.9,
            fix={"auto_fixable": bool(template and template.auto_fixable),
                 "template": template.template_id if template else "",
                 "plan": template.title if template else "需人工研判",
                 "status": "pending"}))
    return out
