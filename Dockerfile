# dsh-red-blue-team · 企业级部署镜像
# 基于 python:3.11-slim，内置 dsh 框架 + 红蓝队检测平台 + Web 面板
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# 系统依赖（靶场/扫描需要的基本工具）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目（排除项见 .dockerignore）
COPY . .

# 默认启动 Web 面板（含内置靶场作默认目标）
# 生产建议：通过 REDTEAM_API_TOKEN 强制鉴权 + 挂载持久卷
EXPOSE 8766
CMD ["python", "-m", "redteam.cli", "web", "--with-lab", "--port", "8766", "--host", "0.0.0.0"]
