"""dsh._env —— 项目根目录 .env 加载（无第三方依赖）。

支持 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL /
DEEPSEEK_DISABLE_THINKING，用于把 LLM 接缝指向火山方舟 Agent Plan 等
OpenAI 兼容端点。已存在的环境变量优先（不覆盖）。

注意：.env 是本地密钥文件，已在 .gitignore 中，绝不提交。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("dsh.env")


def load_dotenv() -> bool:
    """加载项目根目录 .env；返回是否读取到文件。幂等（环境变量已存在则不覆盖）。"""
    dotenv_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env")
    if not os.path.exists(dotenv_path):
        return False
    try:
        with open(dotenv_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
        log.debug("已加载项目 .env（%s）", dotenv_path)
        return True
    except OSError:
        return False
