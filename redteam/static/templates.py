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

register_template(FixTemplate(
    "jwt-secret-hardening", "weak_jwt_secret", "JWT 签名密钥加固（长随机密钥 + 算法固定）",
    explanation=(
        "【现象】JWT 签名密钥过短或硬编码在代码/配置中。\n"
        "【根因】弱密钥可被离线爆破；算法未固定可被混淆攻击（alg=none/HS256↔RS256）。\n"
        "【影响】攻击者伪造任意身份令牌，评级 critical。"),
    rationale="JWT 安全依赖密钥强度与算法固定：256-bit 随机密钥 + 服务端固定允许算法。",
    how_to_fix=[
        "密钥改为 ≥32 字节密码学随机值，从 KMS/环境变量注入（不入库）",
        "验证时固定允许算法（如仅 HS256 或 RS256，拒绝 alg=none）",
        "密钥定期轮换 + 泄露立即作废",
    ],
    code_before=(
        "# 修复前：弱密钥 + 算法不固定\n"
        "import jwt\n"
        "SECRET = \"secret123\"\n"
        "payload = jwt.decode(token, SECRET, algorithms=[\"HS256\"])  # 密钥可爆破"),
    code_after=(
        "# 修复后：强随机密钥 + 算法白名单\n"
        "import os, jwt\n"
        "SECRET = os.environ[\"JWT_SECRET\"]          # ≥32 字节随机，KMS 注入\n"
        "payload = jwt.decode(token, SECRET, algorithms=[\"HS256\"])  # 显式白名单"),
    auto_fixable=False,
    manual_steps=("密钥换 ≥32 字节随机值并移入 KMS", "固定允许算法白名单",
                  "密钥轮换机制"),
    verify_steps=["alg=none/弱密钥伪造令牌被拒绝"]))

register_template(FixTemplate(
    "tls-enforce", "plaintext_http", "强制 HTTPS（明文调用与 TLS 校验禁用）",
    explanation=(
        "【现象】代码使用明文 http:// 调用外部服务，或禁用了 TLS 证书校验。\n"
        "【根因】传输层未加密/未验证对端身份。\n"
        "【影响】流量可被窃听/篡改（中间人攻击），评级 high。"),
    rationale="外部通信必须 HTTPS 且开启证书校验；内网自签证书走显式白名单。",
    how_to_fix=[
        "所有外部调用改 https://",
        "移除 verify=False / rejectUnauthorized:false / InsecureSkipVerify",
        "自签证书场景：显式指定 CA 包而非全局关闭校验",
    ],
    code_before=(
        "# 修复前\n"
        "requests.get(\"http://api.internal.example.com\", verify=False)"),
    code_after=(
        "# 修复后\n"
        "requests.get(\"https://api.internal.example.com\",\n"
        "             verify=\"/etc/ssl/ca-bundle.pem\")"),
    auto_fixable=False,
    manual_steps=("外部调用改 https", "移除 TLS 校验禁用",
                  "自签证书用显式 CA 包"),
    verify_steps=["代码审计无 http:// 与 verify=False"]))

register_template(FixTemplate(
    "terraform-hardening", "terraform_open_cidr", "Terraform 安全组收紧（禁 0.0.0.0/0）",
    explanation=(
        "【现象】Terraform 安全组规则对 0.0.0.0/0 开放端口。\n"
        "【根因】基础设施即代码未按最小暴露原则配置。\n"
        "【影响】管理端口/服务对全网开放，评级 critical。"),
    rationale="安全组只允许必要的来源 IP/网段；管理端口绑定内网或堡垒机。",
    how_to_fix=[
        "cidr_blocks 改为业务来源网段/固定办公 IP",
        "管理端口（22/3389/数据库）只对内网/堡垒机开放",
        "IaC 变更走评审 + tfsec/checkov 扫描进 CI",
    ],
    code_before=(
        "# 修复前\n"
        "ingress {\n"
        "  from_port = 22\n"
        "  cidr_blocks = [\"0.0.0.0/0\"]      # 全网可连 SSH\n"
        "}"),
    code_after=(
        "# 修复后\n"
        "ingress {\n"
        "  from_port = 22\n"
        "  cidr_blocks = [\"10.0.0.0/8\"]      # 仅内网\n"
        "}"),
    auto_fixable=False,
    manual_steps=("安全组来源网段收紧", "管理端口内网化", "tfsec 扫描进 CI"),
    verify_steps=["安全组无 0.0.0.0/0 入站规则"]))

register_template(FixTemplate(
    "k8s-hardening", "k8s_insecure_workload", "K8s 工作负载加固（去特权/禁宿主网络/禁自动挂 SA）",
    explanation=(
        "【现象】工作负载声明 privileged/hostNetwork/automountServiceAccountToken。\n"
        "【根因】K8s 安全基线未执行（OWASP K8s Top10 K01/K02）。\n"
        "【影响】容器逃逸放大、跨节点网络、SA 令牌被盗用，评级 critical。"),
    rationale="按 K8s Pod Security 基线加固：最小特权、网络隔离、SA 令牌按需挂载。",
    how_to_fix=[
        "移除 privileged: true 与 hostNetwork: true",
        "automountServiceAccountToken 按需开启（默认 false）",
        "启用 PodSecurityAdmission/OPA 策略强制基线",
    ],
    code_before=(
        "# 修复前\n"
        "spec:\n"
        "  containers:\n"
        "    - name: app\n"
        "      securityContext: { privileged: true }\n"
        "  hostNetwork: true"),
    code_after=(
        "# 修复后\n"
        "spec:\n"
        "  automountServiceAccountToken: false\n"
        "  containers:\n"
        "    - name: app\n"
        "      securityContext: { allowPrivilegeEscalation: false }"),
    auto_fixable=False,
    manual_steps=("去特权/宿主网络", "SA 令牌按需挂载", "PSA 策略强制"),
    verify_steps=["kubectl 审计无 privileged/hostNetwork 工作负载"]))


def register_all() -> None:
    """占位：模板已在 import 时注册（保证 blueteam 导入路径一致）。"""
