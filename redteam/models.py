"""redteam.models —— 核心数据契约。

所有模块共享的数据结构：攻击样本、目标响应、判定结果、漏洞发现、
扫描结果、修复方案、回归结果、报告。
"""
from __future__ import annotations

import enum
import json
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------- 枚举 ----------

class Verdict(str, enum.Enum):
    SUCCESS = "success"        # 攻击成功（确定性信号命中）
    FAILED = "failed"          # 攻击失败（防御生效）
    SUSPICIOUS = "suspicious"  # 弱信号命中，需人工复核
    SKIPPED = "skipped"        # 样本不适配当前目标，跳过
    ERROR = "error"            # 执行出错


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"critical": 5, "high": 4, "medium": 3,
                "low": 2, "info": 1}[self.value]


class Surface(str, enum.Enum):
    CHAT = "chat"        # 对话型攻击（LLM/AI 层）
    API = "api"          # HTTP API 攻击（Web/API 层）
    CONFIG = "config"    # 配置/部署检查（安全头、调试端点）


# ---------- 攻击样本 ----------

@dataclass
class AttackSample:
    """一条基础攻击样本（YAML 样本库中的一行）。"""
    id: str
    category: str
    name: str
    severity: str                      # Severity 值
    surface: str                       # chat / api / config
    owasp: str = ""                    # 权威框架映射（如 LLM01 / A03 / API1）
    target_point: str = ""             # 攻击目标（行为偏离/信息泄露/越权…）
    role_context: List[str] = field(default_factory=list)
    payload: str = ""                  # 模板文本（{var} 槽位）
    variables: Dict[str, List[str]] = field(default_factory=dict)
    paraphrases: List[str] = field(default_factory=list)  # 变体模板
    method: str = "GET"
    path: str = ""                     # API 类样本的路径模板
    params: Dict[str, str] = field(default_factory=dict)  # 查询参数模板
    body: Dict[str, str] = field(default_factory=dict)    # 请求体模板
    headers: Dict[str, str] = field(default_factory=dict) # 额外请求头
    evidence_patterns: List[str] = field(default_factory=list)  # 成功证据正则
    expected_signals: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    repeat: int = 1                     # 同一载荷连续发送次数（幂等/竞态类攻击）
    stateful: bool = False              # 会修改目标状态（串行通道+重置隔离）


@dataclass
class ConcreteSample:
    """实例化后的攻击样本（槽位已填充、变体已选定）。"""
    uid: str                           # 稳定唯一 id（回归复测依赖）
    sample: AttackSample
    role: str
    payload: str
    params: Dict[str, str] = field(default_factory=dict)
    body: Dict[str, str] = field(default_factory=dict)
    path: str = ""
    variant_index: int = 0
    variant_of: str = ""               # 变体来源：variables / paraphrase / base

    @property
    def id(self) -> str:
        return self.sample.id

    @property
    def category(self) -> str:
        return self.sample.category

    @property
    def surface(self) -> str:
        return self.sample.surface

    @property
    def severity(self) -> str:
        return self.sample.severity

    def describe(self) -> str:
        return f"{self.uid} [{self.category}] role={self.role}"

    def to_json(self) -> Dict[str, Any]:
        return {"uid": self.uid, "sample_id": self.sample.id,
                "category": self.category, "role": self.role,
                "payload": self.payload, "surface": self.surface,
                "variant_index": self.variant_index}


# ---------- 目标响应与侦察 ----------

@dataclass
class TargetResponse:
    """目标系统对一次请求的原始响应（完整保留，供判定与审计）。"""
    status: int = 0
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    json: Optional[Dict[str, Any]] = None
    elapsed: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)

    def snippet(self, limit: int = 400) -> str:
        text = self.text.strip()
        if len(text) > limit:
            return text[:limit] + f"...(截断, 共 {len(text)} 字符)"
        return text


@dataclass
class CapabilityProbe:
    """侦察结果：目标可观测能力。"""
    reachable: bool = False
    chat_ok: bool = False
    side_effect_check_ok: bool = False
    security_headers: Dict[str, Optional[str]] = field(default_factory=dict)
    banner: str = ""
    scenarios: List[str] = field(default_factory=list)   # 业务场景指纹
    notes: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ---------- 判定 ----------

@dataclass
class Signal:
    name: str
    hit: bool
    confidence: float
    evidence: str = ""


@dataclass
class VerdictResult:
    sample_uid: str
    category: str
    role: str
    verdict: str                        # Verdict 值
    confidence: float
    signals: List[Signal] = field(default_factory=list)
    evidence: str = ""                  # 关键证据（响应片段/命中文本）
    chain: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""

    @property
    def success(self) -> bool:
        return self.verdict == Verdict.SUCCESS.value

    def to_json(self) -> Dict[str, Any]:
        return {"sample_uid": self.sample_uid, "category": self.category,
                "role": self.role, "verdict": self.verdict,
                "confidence": self.confidence,
                "signals": [asdict(s) for s in self.signals],
                "evidence": self.evidence, "chain": self.chain,
                "created_at": self.created_at}


# ---------- 漏洞发现 ----------

@dataclass
class Finding:
    finding_id: str
    scan_id: str
    category: str
    owasp: str
    severity: str
    sample_id: str
    sample_uid: str
    payload: str
    role: str
    chain: List[Dict[str, Any]] = field(default_factory=list)
    evidence: str = ""
    signals: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    fix: Dict[str, Any] = field(default_factory=dict)  # auto_fixable/template/plan/status

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ---------- 扫描结果 ----------

@dataclass
class ScanResult:
    scan_id: str
    target: str
    started_at: str = ""
    finished_at: str = ""
    status: str = "running"
    probe: Dict[str, Any] = field(default_factory=dict)
    verdicts: List[VerdictResult] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    report_path: str = ""
    report_json_path: str = ""
    audit_path: str = ""

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def success_count(self) -> int:
        return sum(1 for v in self.verdicts if v.success)

    @property
    def suspicious_count(self) -> int:
        return sum(1 for v in self.verdicts
                   if v.verdict == Verdict.SUSPICIOUS.value)

    def severity_counts(self) -> Dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts


# ---------- 蓝队 ----------

@dataclass
class FixPlan:
    plan_id: str
    finding_id: str
    category: str
    auto_fixable: bool
    template_id: str = ""
    title: str = ""
    rationale: str = ""                # 为什么这么修（审计要求）
    ops: List[Dict[str, Any]] = field(default_factory=list)
    manual_steps: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FixResult:
    fix_id: str
    plan_id: str
    finding_id: str
    status: str                         # applied / failed / manual_only
    applied_to: str = ""                # 沙箱/目标位置
    backup: str = ""                    # 回滚备份路径
    detail: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RegressionResult:
    finding_id: str
    passed: bool
    before: List[str] = field(default_factory=list)   # 修复前命中的样本 uid
    after: List[str] = field(default_factory=list)    # 回归复测的判定摘要
    detail: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ---------- 工具函数 ----------

_TPL_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_template(template: str, values: Dict[str, str]) -> str:
    """安全渲染模板：只替换 ``{已知槽位}``，其余花括号原样保留
    （SSTI 载荷 ``{{7*7}}`` 等不会被误伤）。"""
    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)
    return _TPL_RE.sub(_sub, template)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
