# dsh-red-blue-team 部署指南（企业级）

红蓝队安全检测平台，支持 **Docker Compose 一键部署** 与 **裸机部署** 两种方式。

---

## 1. Docker Compose 部署（推荐）

```bash
# 1) 复制环境变量模板并填写
cp .env.example .env
# 编辑 .env：必填 DEEPSEEK_API_KEY；生产必填 REDTEAM_API_TOKEN

# 2) 构建并启动
docker compose up -d --build

# 3) 查看健康/日志
docker compose ps            # 状态 healthy
docker compose logs -f redteam

# 4) 访问
#    http://<服务器IP>:8766   （面板）
#    首次访问会要求输入 REDTEAM_API_TOKEN（若已设置）
```

**持久化**：`./web_runtime` 挂载到容器，任务/审计/报告容器重建不丢。
**端口**：`REDTEAM_PORT` 环境变量可改（默认 8766）。

---

## 2. 裸机部署

```bash
cd dsh_red_blue_team
pip install -r requirements.txt
cp .env.example .env && vim .env          # 填写密钥与令牌

# 启动 Web 面板（含内置靶场）
REDTEAM_API_TOKEN=xxx python -m redteam.cli web --with-lab --port 8766 --host 0.0.0.0
```

生产环境建议用 systemd / supervisor 托管，并置于反向代理（nginx/caddy）+ TLS 之后。

---

## 3. 环境变量说明

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | 火山方舟密钥（ark-xxx）|
| `DEEPSEEK_BASE_URL` | 默认 | 方舟 Agent Plan 端点 `/api/plan/v3` |
| `DEEPSEEK_MODEL` | 默认 | 默认 `deepseek-v4-flash` |
| `DEEPSEEK_DISABLE_THINKING` | 推理模型 | `1` 时 content 直出（dpv4flash 必需）|
| `REDTEAM_API_TOKEN` | 生产 ✅ | 面板 Bearer 令牌；**未设置则面板无鉴权** |

> ⚠️ `.env` 已 gitignore，绝不提交。生产必须设置 `REDTEAM_API_TOKEN`。

---

## 4. 安全基线（上线前必读）

1. **必须设置 `REDTEAM_API_TOKEN`**：未设置时面板 `/api` 完全无鉴权，任何人可发起扫描/查看报告。
2. **仅授权目标**：平台只用于已获书面授权的测试。扫描真实目标前确认授权范围。
3. **反向代理 + TLS**：生产暴露到公网务必走 HTTPS。
4. **网络隔离**：扫描引擎/沙箱应部署在与生产隔离的网络/主机上。
5. **密钥最小化**：扫描用独立 API Key，按需开通额度，定期轮换。
6. **审计留痕**：每次攻击的载荷/响应/判定写入 `web_runtime/audit/*.jsonl`，可回放，满足合规审计。

---

## 5. 运维

- **健康检查**：compose 内置 `curl /` healthcheck；容器 `healthy` 才算就绪。
- **日志**：`docker compose logs -f redteam`；应用内每步攻击/任务进度均打点。
- **数据**：`web_runtime/` 含任务、审计、报告；备份该目录即可。
- **升级**：`git pull && docker compose up -d --build`。

---

## 6. 命令行（非面板场景）

```bash
python -m redteam.cli demo --out ./demo_out      # 一键闭环演示（靶场→攻击→修复→复扫0）
python -m redteam.cli scan --config scan.yml --fix  # 命令行扫描+修复
python -m redteam.cli static <文件夹>              # 静态审计
python -m redteam.cli schedule --config scan.yml    # 定时扫描+报告留存
```
