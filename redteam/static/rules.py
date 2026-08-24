"""redteam.static.rules —— 静态扫描规则库。

规则引擎：对本地项目文件夹做代码级安全审计（密钥硬编码/危险调用/弱加密/
敏感文件/依赖 CVE-lite/容器配置），输出 file:line 级证据。
每类规则绑定修复模板（问题说明+代码级 before/after 修复指引）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Pattern, Tuple

from ..blueteam.templates import FixTemplate, register_template


@dataclass
class StaticRule:
    rule_id: str
    category: str
    severity: str
    title: str
    pattern: str                       # 匹配正则（行级）
    file_globs: Tuple[str, ...]        # 适用文件 glob（后缀）
    fix_template_id: str
    hint: str                          # 命中提示（证据说明）


# ---------------- 规则定义 ----------------

RULES: List[StaticRule] = [
    StaticRule("st-001", "hardcoded_secret", "critical", "硬编码 API 密钥",
               r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?key)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
               (".py", ".js", ".ts", ".java", ".go", ".rb", ".env", ".yml", ".yaml", ".json"),
               "secret-rotation",
               "源代码中硬编码密钥（泄露即等于失陷，须轮换并移入密钥管理）"),
    StaticRule("st-002", "hardcoded_secret", "high", "硬编码密码",
               r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{4,}['\"]\s*$",
               (".py", ".js", ".ts", ".java", ".go", ".conf", ".yml", ".yaml", ".env"),
               "secret-rotation",
               "源代码中硬编码密码"),
    StaticRule("st-003", "weak_crypto", "high", "弱哈希算法（MD5）",
               r"(?i)\b(md5|hashlib\.md5)\s*\(",
               (".py", ".js", ".ts", ".java", ".go"),
               "crypto-hash-upgrade",
               "MD5 用于密码/完整性场景不安全，应换 SHA-256/bcrypt/argon2"),
    StaticRule("st-004", "weak_crypto", "high", "弱哈希算法（SHA1）",
               r"(?i)\b(sha1|hashlib\.sha1)\s*\(",
               (".py", ".js", ".ts", ".java", ".go"),
               "crypto-hash-upgrade",
               "SHA1 已不满足碰撞安全要求"),
    StaticRule("st-005", "sql_injection", "critical", "SQL 字符串拼接",
               r"(?i)(execute|executemany|query)\s*\(\s*f['\"]",
               (".py",),
               "sqli-filter",
               "SQL 语句 f-string/格式化拼接用户输入，存在注入风险"),
    StaticRule("st-006", "command_injection", "critical", "shell=True 子进程调用",
               r"(?i)shell\s*=\s*True",
               (".py",),
               "cmdi-check",
               "shell=True 拼装用户输入导致命令注入"),
    StaticRule("st-007", "unsafe_deserialization", "critical", "不安全反序列化（pickle）",
               r"(?i)\b(pickle\.loads?|yaml\.load\s*\(|torch\.load|joblib\.load)\s*\(",
               (".py",),
               "deserialization-safe",
               "反序列化不可信数据可导致任意代码执行（yaml.load 应换 yaml.safe_load）"),
    StaticRule("st-008", "unsafe_eval", "critical", "危险动态执行（eval/exec）",
               r"(?i)\b(eval|exec)\s*\(",
               (".py", ".js", ".ts"),
               "deserialization-safe",
               "eval/exec 执行用户可控输入 = 任意代码执行"),
    StaticRule("st-009", "debug_mode", "medium", "调试模式开启（生产风险）",
               r"(?i)\b(debug\s*=\s*True|DEBUG\s*=\s*True)\b",
               (".py",),
               "debug-off",
               "生产环境开启 debug 会泄露堆栈与内部配置"),
    StaticRule("st-010", "cors_misconfig", "medium", "CORS 通配配置",
               r"(?i)(allow_origins?\s*=\s*\[\s*['\"]\*['\"]|Access-Control-Allow-Origin\s*:\s*\*)",
               (".py", ".js", ".ts", ".conf", ".yml", ".yaml"),
               "cors-restrict",
               "CORS 允许任意来源 + 凭据组合可导致跨域数据窃取"),
    StaticRule("st-011", "xss_sink", "high", "XSS 危险 sink（innerHTML/注入模板）",
               r"(?i)\.(innerHTML|outerHTML)\s*=|dangerouslySetInnerHTML",
               (".js", ".ts", ".jsx", ".tsx", ".vue"),
               "xss-encode",
               "未转义写入 DOM 导致 XSS"),
    StaticRule("st-013", "docker_root", "high", "容器以 root 运行",
               r"(?i)^\s*USER\s+(root|0)\b",
               ("Dockerfile",),
               "docker-hardening",
               "容器以 root 运行放大逃逸影响"),
    StaticRule("st-014", "docker_privileged", "critical", "特权容器",
               r"(?i)privileged\s*:\s*true|--privileged",
               ("docker-compose.yml", "docker-compose.yaml", "*.yml", "*.yaml"),
               "docker-hardening",
               "特权容器可访问宿主资源，逃逸风险极高"),
    StaticRule("st-015", "unsafe_yaml", "high", "不安全 YAML 加载",
               r"(?i)\byaml\.load\s*\(",
               (".py",),
               "deserialization-safe",
               "yaml.load 可执行任意对象构造（换 yaml.safe_load）"),
    StaticRule("st-016", "weak_jwt_secret", "critical", "JWT 弱密钥/硬编码签名密钥",
               r"(?i)(jwt|SECRET_KEY|JWT_SECRET)\s*[:=(]\s*['\"][^'\"]{1,20}['\"]",
               (".py", ".js", ".ts", ".java", ".go", ".env", ".yml", ".yaml"),
               "jwt-secret-hardening",
               "JWT 签名密钥过短/硬编码可被离线爆破伪造令牌"),
    StaticRule("st-017", "plaintext_http", "medium", "明文 HTTP 调用",
               r"http://(?!localhost|127\.0\.0\.1)[\w.\-]+",
               (".py", ".js", ".ts", ".java", ".go"),
               "tls-enforce",
               "业务代码使用明文 http:// 调用外部服务（应强制 https）"),
    StaticRule("st-018", "tls_verify_disabled", "high", "TLS 证书校验被禁用",
               r"(?i)(verify\s*=\s*False|rejectUnauthorized\s*[:=]\s*false|InsecureSkipVerify\s*:\s*true)",
               (".py", ".js", ".ts", ".go", ".java"),
               "tls-enforce",
               "禁用 TLS 证书校验使通信可被中间人劫持"),
    StaticRule("st-019", "terraform_open_cidr", "critical", "Terraform 安全组全网段开放",
               r"cidr_blocks\s*=\s*\[\s*['\"]0\.0\.0\.0/0['\"]",
               (".tf",),
               "terraform-hardening",
               "安全组对 0.0.0.0/0 开放（管理端口应限制来源 IP）"),
    StaticRule("st-020", "k8s_insecure_workload", "critical", "K8s 工作负载不安全配置",
               r"(?i)(privileged\s*:\s*true|hostNetwork\s*:\s*true|automountServiceAccountToken\s*:\s*true)",
               (".yml", ".yaml"),
               "k8s-hardening",
               "特权容器/宿主网络/默认挂载 SA 令牌违反 K8s 安全基线（K01/K02）"),
    StaticRule("st-021", "java_hardcoded_secret", "critical", "Java 硬编码密钥/密码",
               r"(?i)(String\s+\w*(secret|password|apikey|api_key)\w*\s*=\s*['\"][^'\"]{6,}['\"])",
               (".java",),
               "secret-rotation",
               "Java 源码硬编码凭据（可被反编译提取）"),
    StaticRule("st-022", "go_hardcoded_secret", "critical", "Go 硬编码密钥/密码",
               r"(?i)(secret|password|apikey|api_key)\w*\s*:?=\s*['\"][A-Za-z0-9_\-]{8,}['\"]",
               (".go",),
               "secret-rotation",
               "Go 源码硬编码凭据（二进制可被 strings 提取）"),
]

#: 令牌文件规则（按文件名匹配，无行号）
_TOKEN_FILE_RULES: List[Tuple[str, str, str, str]] = [
    (r"(^|/)\.npmrc$", "sensitive_file", "high", "npm 令牌文件被纳入项目"),
    (r"(^|/)\.pypirc$", "sensitive_file", "high", "PyPI 凭据文件被纳入项目"),
    (r"(^|/)\.dockercfg$", "sensitive_file", "high", "Docker Registry 凭据被纳入项目"),
    (r"credentials\.(json|xml)$", "sensitive_file", "critical", "云服务凭据文件被纳入项目"),
]

#: 敏感文件规则（按文件名匹配，无行号）
_SENSITIVE_FILE_RULES: List[Tuple[str, str, str, str]] = [
    # (文件名正则, 类别, 严重度, 说明)
    (r"(^|/)\.env$", "sensitive_file", "high", "环境变量文件被纳入项目（含密钥）"),
    (r"\.pem$", "sensitive_file", "critical", "私钥文件被纳入项目"),
    (r"id_rsa$", "sensitive_file", "critical", "SSH 私钥被纳入项目"),
    (r"(^|/)\.git/config$", "sensitive_file", "medium", ".git 目录被纳入项目"),
    (r"\.(bak|sql|tar\.gz|zip)$", "sensitive_file", "medium", "备份/数据库导出文件被纳入项目"),
]
SENSITIVE_FILES: List[Tuple[Pattern, str, str, str]] = [
    (re.compile(pattern), category, severity, hint)
    for pattern, category, severity, hint
    in _SENSITIVE_FILE_RULES + _TOKEN_FILE_RULES]

#: CVE-lite 依赖漏洞表：包名 → (首个修复版本, 说明)
#: 说明：教学用启发式清单（非完整 CVE 数据库），生产建议接入 osv-scanner/pip-audit。
CVE_LITE = {
    "django": ("3.2.24", "Django 存在已知漏洞（CVE 系列），升级至 ≥3.2.24/5.0.x"),
    "flask": ("1.1.4", "Flask 旧版本存在已知漏洞，升级至 ≥1.1.4/2.2.5"),
    "jinja2": ("3.1.3", "Jinja2 <3.1.3 存在 SSTI/拒绝服务漏洞"),
    "pyyaml": ("5.4", "PyYAML <5.4 不安全反序列化漏洞"),
    "pillow": ("9.3.0", "Pillow <9.3.0 存在图像解析漏洞"),
    "urllib3": ("1.26.18", "urllib3 旧版本存在代理注入等漏洞"),
    "requests": ("2.31.0", "requests 旧版本存在代理头泄露漏洞"),
    "aiohttp": ("3.9.0", "aiohttp 旧版本存在 HTTP 走私漏洞"),
    "cryptography": ("41.0.4", "cryptography 旧版本存在越界读取漏洞"),
    "sqlalchemy": ("1.4.50", "SQLAlchemy 旧版本存在 SQL 注入漏洞"),
    "werkzeug": ("2.2.3", "Werkzeug 旧版本存在调试器 RCE 风险"),
    "torch": ("1.13.1", "torch 旧版本存在反序列化漏洞（torch.load）"),
}

#: npm CVE-lite 依赖漏洞表：包名 → (首个修复版本, 说明)
NPM_CVE_LITE = {
    "lodash": ("4.17.21", "lodash <4.17.21 存在原型污染漏洞（CVE-2021-23337）"),
    "axios": ("1.6.0", "axios 旧版本存在 SSRF/原型污染漏洞"),
    "express": ("4.19.2", "express <4.19.2 存在开放重定向漏洞"),
    "jsonwebtoken": ("9.0.0", "jsonwebtoken 旧版本存在算法混淆漏洞"),
    "minimist": ("1.2.6", "minimist <1.2.6 存在原型污染漏洞"),
    "node-fetch": ("3.2.10", "node-fetch 旧版本存在 SSRF 漏洞"),
    "semver": ("7.5.2", "semver 旧版本存在 ReDoS 漏洞"),
    "tar": ("6.1.15", "tar 旧版本存在路径穿越漏洞"),
    "webpack-dev-server": ("4.15.1", "webpack-dev-server 旧版本存在任意文件读取"),
    "ws": ("8.17.1", "ws 旧版本存在拒绝服务漏洞"),
}

_RULES_BY_CATEGORY = {r.category for r in RULES}


def rule_categories() -> set:
    return set(_RULES_BY_CATEGORY) | {"sensitive_file", "dependency_vuln"}
