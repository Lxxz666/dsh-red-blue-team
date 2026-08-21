"""
dsh.wanter.coordinator —— 状态坐标投影缝（语义状态 → 连续坐标）。

- ``HashCoordinator``：会话/文本指纹确定性哈希 → [-2,2]^dim（离线默认）；
- ``MockEmbeddingCoordinator``：词级哈希求和归一化（确定性、离线、可测试）；
- ``HttpEmbeddingCoordinator``：OpenAI 兼容 /embeddings 端点（httpx），
  失败时回退 Hash 投影并记录警告（尽力而为）。
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("dsh.wanter")


def _hash_axis(text: str, salt: int, dim: int, axis: int) -> float:
    """FNV-1a 哈希投影到 [-1,1] 的某轴。"""
    digest = hashlib.sha256(f"{salt}:{axis}:{text}".encode("utf-8")).digest()
    return (digest[axis % len(digest)] / 255.0) * 2.0 - 1.0


class HashCoordinator:
    """确定性哈希伪嵌入（默认）。"""

    def __init__(self, dim: int = 2, salt: int = 0, scale: float = 2.0) -> None:
        self.dim = dim
        self.salt = salt
        self.scale = scale

    def embed(self, summary: str) -> tuple:
        return tuple(_hash_axis(summary, self.salt, self.dim, axis)
                     * self.scale for axis in range(self.dim))


class MockEmbeddingCoordinator:
    """词级哈希求和归一化（确定性词袋伪嵌入）。"""

    _TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+")

    def __init__(self, dim: int = 2, scale: float = 2.0) -> None:
        self.dim = dim
        self.scale = scale

    def embed(self, summary: str) -> tuple:
        axes = [0.0] * self.dim
        tokens = self._TOKEN_RE.findall(summary.lower())
        if not tokens:
            return tuple(axes)
        for token in tokens:
            for axis in range(self.dim):
                axes[axis] += _hash_axis(token, salt=7, dim=self.dim,
                                         axis=axis)
        norm = math.sqrt(sum(a * a for a in axes))
        if norm < 1e-9:
            return tuple(axes)
        return tuple(a / norm * self.scale for a in axes)


class HttpEmbeddingCoordinator:
    """OpenAI 兼容 embeddings 端点（尽力而为，失败回退 Hash）。"""

    def __init__(self, base_url: str, model: str, api_key: Optional[str] = None,
                 dim: int = 2, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dim = dim
        self.timeout = timeout
        self._fallback = HashCoordinator(dim=dim)

    async def embed(self, summary: str) -> tuple:
        """请求嵌入端点；任何失败 → 回退 Hash 投影（尽力而为）。"""
        import httpx
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": summary},
                    headers=headers)
                response.raise_for_status()
                data = response.json()
                vector = data["data"][0]["embedding"]
        except Exception as exc:
            log.warning("embedding request failed, fallback to hash: %s", exc)
            return self._fallback.embed(summary)
        # 高维 → 低维：均值池化分块
        if len(vector) <= self.dim:
            padded = list(vector) + [0.0] * (self.dim - len(vector))
            return tuple(padded)
        chunk = math.ceil(len(vector) / self.dim)
        return tuple(sum(vector[i * chunk:(i + 1) * chunk]) / chunk
                     for i in range(self.dim))


def build_coordinator(kind: str, dim: int,
                      config: Optional[Dict[str, Any]] = None):
    """
    工厂：kind ∈ hash | mock | http。

    :param config: http 时需要 base_url/model/api_key。
    """
    config = config or {}
    if kind == "mock":
        return MockEmbeddingCoordinator(dim=dim,
                                        scale=float(config.get("scale", 2.0)))
    if kind == "http":
        return HttpEmbeddingCoordinator(
            base_url=config["base_url"], model=config.get("model", "text-embedding-3-small"),
            api_key=config.get("api_key"), dim=dim)
    return HashCoordinator(dim=dim, scale=float(config.get("scale", 2.0)))
