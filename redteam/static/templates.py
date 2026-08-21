"""redteam.static.templates —— 静态扫描类别的修复模板（代码级 before/after）。"""
from __future__ import annotations

from ..blueteam.templates import FixTemplate, register_template

register_template(FixTemplate(
    "secret-rotation", "hardcoded_secret", "移除硬编码密钥并轮换（密钥管理服务）",
    explanation=(
        "【现象】源代码中发现硬编码的 API 密钥/密码。\n"
        "【根因】密钥直接写入代码/配置文件并提交版本库。\n"
        "【影响】仓库可见者（含泄露的仓库）即获得系统权限，评级 critical。"),
    rationale="代码中的密钥等同公开泄露：必须移除+轮换+入密钥管理。",
    how_to_fix=[
        "立即轮换（rotate）已泄露的密钥，旧密钥作废",
        "从代码/配置中删除密钥，改用环境变量/密钥管理服务（KMS/Secrets Manager）",
        "git 历史清洗（git filter-repo）+ 添加 .gitignore 规则",
        "CI 接入密钥扫描（gitleaks/trufflehog）阻断回归",
    ],
    code_before=(
        "# 修复前：硬编码\n"
        "API_KEY = \"sk-live-9f8a7b6c9f8a7b6c9f8a7b6c\"\n"
        "client = SDK(api_key=API_KEY)"),
    code_after=(
        "# 修复后：环境变量注入\n"
        "import os\n"
        "API_KEY = os.environ[\"API_KEY\"]   # 由部署环境/KMS 注入，不入库\n"
        "client = SDK(api_key=API_KEY)"),
    auto_fixable=False,
    manual_steps=("轮换已泄露密钥", "密钥移入 KMS/环境变量",
                  "git 历史清洗 + 密钥扫描进 CI"),
    verify_steps=["grep 代码库无密钥明文", "旧密钥调用被拒绝"]))

register_template(FixTemplate(
    "crypto-hash-upgrade", "weak_crypto", "弱哈希升级（MD5/SHA1 → SHA-256/argon2）",
    explanation=(
        "【现象】代码使用 MD5/SHA1 处理密码或做完整性校验。\n"
        "【根因】弱哈希算法已可碰撞/破解。\n"
        "【影响】密码可离线破解、完整性校验可被绕过，评级 high。"),
    rationale="MD5/SHA1 已不安全：密码场景用 argon2/bcrypt，完整性用 SHA-256。",
    how_to_fix=[
        "密码存储：argon2id/bcrypt/scrypt（带盐+自适应成本）",
        "完整性/指纹场景：SHA-256 起",
        "旧哈希迁移：登录时重新哈希（rehash-on-login）",
    ],
    code_before=(
        "# 修复前\n"
        "import hashlib\n"
        "stored = hashlib.md5(password.encode()).hexdigest()"),
    code_after=(
        "# 修复后：argon2\n"
        "from argon2 import PasswordHasher\n"
        "ph = PasswordHasher()\n"
        "stored = ph.hash(password)   # 自适应成本 + 随机盐"),
    auto_fixable=False,
    manual_steps=("密码哈希换 argon2/bcrypt", "完整性哈希换 SHA-256",
                  "旧哈希 rehash-on-login 迁移"),
    verify_steps=["审计无 md5()/sha1() 调用"]))

register_template(FixTemplate(
    "deserialization-safe", "unsafe_deserialization", "安全反序列化（禁 pickle/yaml.load）",
    explanation=(
        "【现象】代码对不可信数据使用 pickle.loads / yaml.load / eval。\n"
        "【根因】不安全反序列化可执行任意代码。\n"
        "【影响】攻击者提交构造载荷即 RCE，评级 critical。"),
    rationale="不可信数据绝不进入 pickle/eval；yaml.load 一律换 yaml.safe_load。",
    how_to_fix=[
        "禁用 pickle/yaml.load/eval/exec 处理不可信输入",
        "yaml.load → yaml.safe_load；pickle → JSON/明文协议",
        "确需序列化：用 JSON + schema 校验",
    ],
    code_before=(
        "# 修复前\n"
        "import pickle, yaml\n"
        "obj = pickle.loads(user_input)\n"
        "cfg = yaml.load(user_input)      # 均可被构造 RCE"),
    code_after=(
        "# 修复后\n"
        "import json, yaml\n"
        "obj = json.loads(user_input)     # 仅数据，无代码执行\n"
        "cfg = yaml.safe_load(user_input)"),
    auto_fixable=False,
    manual_steps=("移除 pickle/yaml.load/eval", "换 JSON + safe_load",
                  "协议输入做 schema 校验"),
    verify_steps=["审计无 pickle.loads / yaml.load / eval("])),

register_template(FixTemplate(
    "cors-restrict", "cors_misconfig", "CORS 收紧（白名单来源）",
    explanation=(
        "【现象】CORS 配置为通配 *（可能含凭据）。\n"
        "【根因】跨域策略过宽。\n"
        "【影响】恶意站点可跨域读取用户数据，评级 medium。"),
    rationale="CORS 必须显式白名单来源，凭据模式禁通配。",
    how_to_fix=[
        "allow_origins 改为显式域名白名单",
        "携带凭据（credentials）时禁止 * 与反射任意 Origin",
        "预检（OPTIONS）与正式响应策略一致",
    ],
    code_before=(
        "# 修复前\n"
        "CORS(app, allow_origins=['*'], allow_credentials=True)"),
    code_after=(
        "# 修复后\n"
        "CORS(app, allow_origins=['https://app.example.com'],\n"
        "     allow_credentials=True)"),
    auto_fixable=False,
    manual_steps=("CORS 白名单化", "凭据模式禁通配"),
    verify_steps=["跨域请求验证仅白名单来源被允许"]))

register_template(FixTemplate(
    "dependency-upgrade", "dependency_vuln", "升级含已知漏洞的依赖（CVE 比对）",
    explanation=(
        "【现象】requirements.txt 锁定/声明的依赖版本处于已知漏洞区间。\n"
        "【根因】依赖未及时升级、无漏洞比对流程。\n"
        "【影响】已知 CVE 可被直接利用（远程执行/数据泄露），评级 high。"),
    rationale="依赖漏洞是最高性价比的攻击面：升级 + SBOM 持续比对。",
    how_to_fix=[
        "升级到修复版本（比对 CVE 数据库/安全公告）",
        "锁定依赖并生成 SBOM，CI 持续比对（pip-audit/osv-scanner）",
        "评估传递依赖（pip freeze 全量比对）",
    ],
    auto_fixable=False,
    manual_steps=("升级受影响依赖", "SBOM + CI 漏洞比对", "传递依赖评估"),
    verify_steps=["pip-audit / osv-scanner 复扫无已知漏洞"]))

register_template(FixTemplate(
    "docker-hardening", "docker_misconfig", "容器加固（非 root + 去特权）",
    explanation=(
        "【现象】容器以 root 运行或声明 privileged。\n"
        "【根因】镜像/编排配置未加固。\n"
        "【影响】容器逃逸后直接获得宿主控制，评级 critical。"),
    rationale="最小权限原则：非 root 运行 + 禁特权 + 只读根文件系统。",
    how_to_fix=[
        "镜像内创建非 root 用户并以 USER 运行",
        "移除 privileged: true，capabilities 最小化（drop ALL）",
        "只读根文件系统 + 资源限制（内存/CPU）",
    ],
    code_before=(
        "# 修复前\n"
        "FROM python:3.11\n"
        "COPY . /app\n"
        "CMD [\"python\", \"app.py\"]        # 默认 root 运行"),
    code_after=(
        "# 修复后\n"
        "FROM python:3.11\n"
        "RUN useradd -m appuser\n"
        "COPY --chown=appuser:appuser . /app\n"
        "USER appuser                         # 非 root\n"
        "CMD [\"python\", \"app.py\"]"),
    auto_fixable=False,
    manual_steps=("非 root 运行", "去特权 + capabilities 最小化", "只读根文件系统"),
    verify_steps=["docker inspect 验证 User/Privileged"]))

register_template(FixTemplate(
    "sensitive-file-remove", "sensitive_file", "移除敏感文件出库（.env/私钥/备份）",
    explanation=(
        "【现象】项目目录包含 .env、私钥、备份等敏感文件。\n"
        "【根因】敏感文件被提交进版本库/部署包。\n"
        "【影响】密钥/数据直接泄露，评级 critical。"),
    rationale="敏感文件必须移出仓库与部署产物，并用 .gitignore 阻断回归。",
    how_to_fix=[
        "立即从仓库/部署包移除敏感文件，密钥轮换",
        ".gitignore 添加 .env/*.pem/id_rsa/*.bak",
        "git 历史清洗（如曾提交过）",
    ],
    auto_fixable=False,
    manual_steps=("移除敏感文件并轮换密钥", ".gitignore 规则", "git 历史清洗"),
    verify_steps=["仓库与部署包中无敏感文件"]))


def register_all() -> None:
    """占位：模板已在 import 时注册（保证 blueteam 导入路径一致）。"""
