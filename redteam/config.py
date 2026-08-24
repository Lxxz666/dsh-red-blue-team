"""redteam.config —— 扫描配置：数据模型、YAML 加载、profile 预设、授权校验。

合规红线：对非本地（非 127.0.0.1/localhost/内网 lab 类型）目标，
配置中必须包含 ``authorization`` 声明（授权人/联系方式/授权范围），
否则扫描拒绝执行。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from .errors import AuthorizationError, ConfigError

#: 常见安全响应头（D7 配置检查用）
RECOMMENDED_SECURITY_HEADERS = [
    "content-security-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
]

#: profile 预设：覆盖 categories / variants_per_sample
PROFILES = {
    "full": {
        "categories": ["all"],
        "variants_per_sample": 3,
        "baseline": True,
    },
    "quick": {
        "categories": ["all"],
        "variants_per_sample": 1,
        "baseline": False,
    },
    "injection-only": {
        "categories": ["direct_injection", "indirect_injection",
                       "prompt_extraction", "secret_leak", "tool_abuse",
                       "excessive_agency", "data_poisoning",
                       "privilege_escalation", "behavior_hijack",
                       "role_confusion"],
        "variants_per_sample": 1,
        "baseline": False,
    },
    "web-only": {
        "categories": ["sqli", "xss", "path_traversal", "command_injection",
                       "ssti", "ssrf", "open_redirect", "idor", "bopla",
                       "bfla", "mass_assignment", "debug_endpoint",
                       "security_headers", "sensitive_data"],
        "variants_per_sample": 1,
        "baseline": False,
    },
}


@dataclass
class Authorization:
    authorized_by: str = ""
    contact: str = ""
    scope: str = ""
    note: str = ""

    def valid(self) -> bool:
        return bool(self.authorized_by and self.contact and self.scope)


@dataclass
class TargetConfig:
    name: str = "target"
    type: str = "lab"                  # lab | http | sdk | folder | mcp
    base_url: str = "http://127.0.0.1:8765"
    headers: Dict[str, str] = field(default_factory=dict)
    admin_token: str = "lab-admin-token"
    roles: List[str] = field(default_factory=lambda: ["student", "customer", "admin"])
    timeout_s: float = 15.0
    chat_path: str = "/api/chat"
    side_effect_path: str = "/api/state"
    side_effect_token: str = "scanner-side-effect-token"
    guards_file: str = ""              # 靶场防护配置文件（蓝队修复目标）
    folder_path: str = ""              # 本地项目文件夹（type=folder 静态扫描）
    scenario: str = "auto"             # 业务场景：auto / ecommerce,education,...
    mcp_command: List[str] = field(default_factory=list)  # MCP 服务器启动命令（argv）

    @property
    def is_local(self) -> bool:
        host = self.base_url.split("//")[-1].split("/")[0].split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1")


@dataclass
class VectorsConfig:
    categories: List[str] = field(default_factory=lambda: ["all"])
    roles: List[str] = field(default_factory=lambda: ["student", "customer", "admin"])
    variants_per_sample: int = 2
    seed: int = 42
    bank_dir: str = ""                 # 空 = 包内 sample_bank
    llm_variants: bool = False         # LLM 变体生成（需 DeepSeek 密钥；mock 自动降级）
    llm_variants_per_sample: int = 2   # 每个基础样本的 LLM 变体数上限
    llm_chains: bool = False           # LLM 多轮攻击链生成（需 DeepSeek 密钥；失败降级）
    llm_chains_per_sample: int = 1     # 每个基础样本的 LLM 链数上限


@dataclass
class DetectorConfig:
    baseline: bool = True              # 对话样本先发中性基线消息
    llm_judge: bool = False            # LLM 弱信号裁判（仅存疑样本，不推翻确定性判定）
    min_confidence: float = 0.6        # success 判定置信度下限（低于→suspicious）


@dataclass
class BlueTeamConfig:
    enabled: bool = False
    sandbox_dir: str = "./sandbox"
    auto_apply: bool = True            # 仅对 lab 类型目标生效；外部目标只出方案
    rollback_on_fail: bool = True


@dataclass
class AdaptiveConfig:
    enabled: bool = True
    temperature: float = 0.5           # 优先级选择的 Boltzmann 温度
    domain: str = "default"            # 攻击地形分区（跨目标泛化时按 target.type 自动隔离）


@dataclass
class StorageConfig:
    db_path: str = "./redteam.db"
    audit_dir: str = "./audit"


@dataclass
class EngineConfig:
    concurrency: int = 4
    min_interval_ms: int = 20          # 请求节流（防把目标打挂）
    max_errors: int = 20               # 连续/累计错误上限 → 终止扫描
    samples_limit: int = 0             # 0 = 不限（bench 预算用）
    agent_mode: bool = True            # 主Agent+子Agent 编排（关闭=单进程直跑）


@dataclass
class ScanConfig:
    profile: str = "full"
    authorization: Authorization = field(default_factory=Authorization)
    target: TargetConfig = field(default_factory=TargetConfig)
    vectors: VectorsConfig = field(default_factory=VectorsConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    blueteam: BlueTeamConfig = field(default_factory=BlueTeamConfig)
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    out_dir: str = "./reports"
    source_path: str = ""              # 配置文件路径（审计留存）

    # ---- 加载 ----

    @classmethod
    def from_yaml(cls, path: str) -> "ScanConfig":
        if not os.path.exists(path):
            raise ConfigError(f"配置文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            try:
                raw = yaml.safe_load(fh) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"YAML 解析失败 {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("配置顶层必须是映射")
        return cls.from_dict(raw, source_path=os.path.abspath(path))

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], source_path: str = "") -> "ScanConfig":
        cfg = cls()
        cfg.source_path = source_path
        cfg.profile = str(raw.get("profile", cfg.profile))

        # profile 预设（显式配置优先于预设）
        preset = PROFILES.get(cfg.profile, {})
        if "authorization" in raw:
            auth = raw["authorization"] or {}
            cfg.authorization = Authorization(
                authorized_by=str(auth.get("authorized_by", "")),
                contact=str(auth.get("contact", "")),
                scope=str(auth.get("scope", "")),
                note=str(auth.get("note", "")),
            )
        target = raw.get("target") or {}
        cfg.target = TargetConfig(
            name=str(target.get("name", cfg.target.name)),
            type=str(target.get("type", cfg.target.type)),
            base_url=str(target.get("base_url", cfg.target.base_url)).rstrip("/"),
            headers={str(k): str(v) for k, v in (target.get("headers") or {}).items()},
            admin_token=str(target.get("admin_token", cfg.target.admin_token)),
            roles=[str(r) for r in target.get("roles", cfg.target.roles)],
            timeout_s=float(target.get("timeout_s", cfg.target.timeout_s)),
            chat_path=str(target.get("chat_path", cfg.target.chat_path)),
            side_effect_path=str(target.get("side_effect_path", cfg.target.side_effect_path)),
            side_effect_token=str(target.get("side_effect_token", cfg.target.side_effect_token)),
            guards_file=str(target.get("guards_file", "")),
            folder_path=str(target.get("folder_path", "")),
            scenario=str(target.get("scenario", "auto")),
            mcp_command=[str(c) for c in target.get("mcp_command") or []],
        )
        vectors = raw.get("vectors") or {}
        cfg.vectors = VectorsConfig(
            categories=[str(c) for c in vectors.get("categories", preset.get("categories", ["all"]))],
            roles=[str(r) for r in vectors.get("roles", cfg.target.roles)],
            variants_per_sample=int(vectors.get(
                "variants_per_sample", preset.get("variants_per_sample", 2))),
            seed=int(vectors.get("seed", 42)),
            bank_dir=str(vectors.get("bank_dir", "")),
            llm_variants=bool(vectors.get("llm_variants", False)),
            llm_variants_per_sample=max(0, int(vectors.get(
                "llm_variants_per_sample", 2))),
            llm_chains=bool(vectors.get("llm_chains", False)),
            llm_chains_per_sample=max(0, int(vectors.get(
                "llm_chains_per_sample", 1))),
        )
        detector = raw.get("detector") or {}
        cfg.detector = DetectorConfig(
            baseline=bool(detector.get("baseline", preset.get("baseline", True))),
            llm_judge=bool(detector.get("llm_judge", False)),
            min_confidence=float(detector.get("min_confidence", 0.6)),
        )
        blue = raw.get("blueteam") or {}
        cfg.blueteam = BlueTeamConfig(
            enabled=bool(blue.get("enabled", False)),
            sandbox_dir=str(blue.get("sandbox_dir", "./sandbox")),
            auto_apply=bool(blue.get("auto_apply", True)),
            rollback_on_fail=bool(blue.get("rollback_on_fail", True)),
        )
        adaptive = raw.get("adaptive") or {}
        cfg.adaptive = AdaptiveConfig(
            enabled=bool(adaptive.get("enabled", True)),
            temperature=float(adaptive.get("temperature", 0.5)),
            domain=str(adaptive.get("domain", "default")),
        )
        storage = raw.get("storage") or {}
        cfg.storage = StorageConfig(
            db_path=str(storage.get("db_path", "./redteam.db")),
            audit_dir=str(storage.get("audit_dir", "./audit")),
        )
        engine = raw.get("engine") or {}
        cfg.engine = EngineConfig(
            concurrency=max(1, int(engine.get("concurrency", 4))),
            min_interval_ms=max(0, int(engine.get("min_interval_ms", 20))),
            max_errors=max(1, int(engine.get("max_errors", 20))),
            samples_limit=max(0, int(engine.get("samples_limit", 0))),
            agent_mode=bool(engine.get("agent_mode", True)),
        )
        cfg.out_dir = str(raw.get("out_dir", "./reports"))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.target.type not in ("lab", "http", "sdk", "folder", "mcp"):
            raise ConfigError(f"未知目标类型: {self.target.type!r}")
        if self.target.type == "folder":
            if not self.target.folder_path:
                raise ConfigError("folder 目标必须指定 target.folder_path")
            if not os.path.isdir(self.target.folder_path):
                raise ConfigError(
                    f"文件夹不存在: {self.target.folder_path}")
            return  # 本地代码静态审计：无需授权声明
        if self.target.type == "mcp":
            if not self.target.mcp_command:
                raise ConfigError(
                    "mcp 目标必须指定 target.mcp_command（MCP 服务器启动命令 argv）")
            return  # MCP 服务器为本地进程（stdio），同 lab 免授权声明
        if not self.target.base_url.startswith(("http://", "https://")):
            raise ConfigError(f"base_url 必须是 http(s):// 形式: {self.target.base_url!r}")
        if self.vectors.variants_per_sample < 0:
            raise ConfigError("variants_per_sample 不能为负")
        if self.target.type != "lab" and not self.target.is_local \
                and not self.authorization.valid():
            raise AuthorizationError(
                "合规红线：对非本地目标扫描必须有书面授权。请在配置中填写 authorization "
                "块（authorized_by / contact / scope）。仅限授权测试，违法测试后果自负。")

    def ensure_dirs(self) -> None:
        for path in (self.out_dir, self.storage.audit_dir,
                     self.blueteam.sandbox_dir):
            if path:
                os.makedirs(path, exist_ok=True)
        db_dir = os.path.dirname(os.path.abspath(self.storage.db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def domain_key(self) -> str:
        """自适应地形分区：目标类型 × 业务域（跨目标泛化隔离）。"""
        if self.adaptive.domain != "default":
            return f"{self.target.type}:{self.adaptive.domain}"
        return f"{self.target.type}:{self.target.name}"
