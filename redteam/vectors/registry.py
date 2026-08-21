"""redteam.vectors.registry —— 攻击向量注册表（ctx.registry）。

- 从 YAML 样本库加载基础样本（按检测面组织：D1 Web / D2 API / D3 LLM / D7 配置）；
- 确定性变体展开：槽位填充 + 释义模板，固定 seed 可复现（报告可重复审计）；
- 变体 uid 稳定（``sample_id-role-v<n>``），蓝队回归复测依赖它；
- LLM 变体生成（可选）：经 dsh LLM 接缝调用真实模型；mock 模式下降级为仅静态样本。
"""
from __future__ import annotations

import itertools
import logging
import os
import random
import re
from typing import Any, Dict, List, Optional, Sequence

import yaml
from dsh.kernel import Service

from ..errors import SampleError
from ..models import (AttackSample, ConcreteSample, render_template)

log = logging.getLogger("redteam.vectors")

#: 样本库默认位置：优先源码布局（<仓库>/sample_bank），
#: 其次安装布局（与 redteam 包同级的 sample_bank 数据包）。
_BANK_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "sample_bank"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "sample_bank"),
]

#: 特殊槽位值：__filler(N)__ → N 个 'A'（超长输入类载荷，YAML 无法直接写 4000 字符）
_FILLER_RE = re.compile(r"^__filler\((\d+)\)__$")


class VectorRegistry(Service):
    """攻击样本注册表（ctx.registry）。"""

    provides = "registry"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.bank_dir = (config or {}).get("bank_dir") or ""
        if not self.bank_dir:
            self.bank_dir = next((p for p in _BANK_DIR_CANDIDATES
                                  if os.path.isdir(p)), _BANK_DIR_CANDIDATES[0])
        self.seed = int((config or {}).get("seed", 42))
        self._samples: List[AttackSample] = []
        self._by_category: Dict[str, List[AttackSample]] = {}

    def apply(self, ctx) -> None:
        ctx.set("registry", self)
        self.load()

    # ---- 加载 ----

    def load(self) -> None:
        self._samples = []
        self._by_category = {}
        if not os.path.isdir(self.bank_dir):
            raise SampleError(f"样本库目录不存在: {self.bank_dir}")
        files = sorted(f for f in os.listdir(self.bank_dir)
                       if f.endswith((".yaml", ".yml")))
        if not files:
            raise SampleError(f"样本库为空: {self.bank_dir}")
        for filename in files:
            path = os.path.join(self.bank_dir, filename)
            with open(path, "r", encoding="utf-8") as fh:
                try:
                    raw = yaml.safe_load(fh)
                except yaml.YAMLError as exc:
                    raise SampleError(f"样本库解析失败 {path}: {exc}") from exc
            self._load_file(raw or {}, path)
        self._by_category = {}
        for sample in self._samples:
            self._by_category.setdefault(sample.category, []).append(sample)
        log.info("样本库加载完成：%d 条基础样本，%d 个分类（%s）",
                 len(self._samples), len(self._by_category), self.bank_dir)

    def _load_file(self, raw: Any, path: str) -> None:
        """样本库文件 = 分类块列表：
        ``[{category, owasp, surface, samples: [...]}, ...]``
        兼容单分类文件（顶层直接是分类块映射）。"""
        if isinstance(raw, dict):
            blocks = [raw]
        elif isinstance(raw, list):
            blocks = raw
        else:
            raise SampleError(f"{path}: 样本库必须是映射或分类块列表")
        for block in blocks:
            if not isinstance(block, dict):
                raise SampleError(f"{path}: 分类块必须是映射")
            self._load_block(block, path)

    def _load_block(self, raw: Dict[str, Any], path: str) -> None:
        defaults = {"category": str(raw.get("category", "")),
                    "surface": str(raw.get("surface", "api")),
                    "owasp": str(raw.get("owasp", ""))}
        for row in raw.get("samples") or []:
            if not isinstance(row, dict):
                raise SampleError(f"{path}: samples 必须是映射列表")
            sample_id = str(row.get("id", ""))
            if not sample_id:
                raise SampleError(f"{path}: 样本缺少 id")
            if any(s.id == sample_id for s in self._samples):
                raise SampleError(f"样本 id 重复: {sample_id}")
            self._samples.append(AttackSample(
                id=sample_id,
                category=str(row.get("category", defaults["category"]) or
                            defaults["category"]),
                name=str(row.get("name", sample_id)),
                severity=str(row.get("severity", "medium")),
                surface=str(row.get("surface", defaults["surface"])),
                owasp=str(row.get("owasp", defaults["owasp"])),
                target_point=str(row.get("target_point", "")),
                role_context=[str(r) for r in row.get("role_context") or []],
                payload=str(row.get("payload", "")),
                variables={str(k): [str(v) for v in vals]
                           for k, vals in (row.get("variables") or {}).items()},
                paraphrases=[str(p) for p in row.get("paraphrases") or []],
                method=str(row.get("method", "GET")),
                path=str(row.get("path", "")),
                params={str(k): str(v) for k, v in (row.get("params") or {}).items()},
                body={str(k): str(v) for k, v in (row.get("body") or {}).items()},
                headers={str(k): str(v) for k, v in (row.get("headers") or {}).items()},
                evidence_patterns=[str(p) for p in row.get("evidence_patterns") or []],
                expected_signals=[str(s) for s in row.get("expected_signals") or []],
                tags=[str(t) for t in row.get("tags") or []],
                repeat=max(1, int(row.get("repeat", 1))),
                stateful=bool(row.get("stateful", False)),
            ))

    # ---- 查询 ----

    @property
    def samples(self) -> List[AttackSample]:
        return list(self._samples)

    def categories(self) -> List[str]:
        return sorted(self._by_category.keys())

    def category_names(self) -> Dict[str, str]:
        """分类 id → 首个样本名（CLI 展示用）。"""
        return {c: samples[0].name for c, samples in self._by_category.items()}

    def sample_by_id(self, sample_id: str) -> Optional[AttackSample]:
        for sample in self._samples:
            if sample.id == sample_id:
                return sample
        return None

    def concrete_for_uid(self, uid: str) -> Optional[ConcreteSample]:
        """由稳定 uid（``sample_id-role-vN``）重建实例化样本（回归复测用）。

        变体展开是确定性的（固定 seed），因此重建结果与原扫描完全一致。
        """
        try:
            sample_id, role, variant = uid.rsplit("-", 2)
            variant_index = int(variant[1:])
        except (ValueError, IndexError):
            return None
        sample = self.sample_by_id(sample_id)
        if sample is None:
            return None
        concretes = self.expand(sample, [role],
                                variants_per_sample=variant_index + 1)
        for concrete in concretes:
            if concrete.variant_index == variant_index:
                return concrete
        return None

    # ---- 展开 ----

    def samples_for(self, roles: Sequence[str],
                    categories: Optional[Sequence[str]] = None,
                    variants_per_sample: int = 1) -> List[ConcreteSample]:
        """按角色 × 分类展开全部实例化样本（确定性顺序）。"""
        wanted = set(categories or ["all"])
        out: List[ConcreteSample] = []
        for sample in self._samples:
            if "all" not in wanted and sample.category not in wanted:
                continue
            sample_roles = [r for r in (sample.role_context or list(roles))
                            if r in roles]
            out.extend(self.expand(sample, sample_roles, variants_per_sample))
        out.sort(key=lambda s: (s.category, s.sample.id, s.role, s.variant_index))
        return out

    def expand(self, sample: AttackSample, roles: Sequence[str],
               variants_per_sample: int = 1) -> List[ConcreteSample]:
        """展开一个基础样本为实例化样本（槽位填充 + 释义变体，seed 可复现）。"""
        if not roles:
            return []
        templates = [sample.payload] + list(sample.paraphrases)
        combos: List[Dict[str, str]] = []
        if sample.variables:
            keys = sorted(sample.variables)
            values = [sample.variables[k] for k in keys]
            for combo in itertools.product(*values):
                combos.append(dict(zip(keys, combo)))
        else:
            combos.append({})
        if not combos:
            combos = [{}]

        rng = random.Random(self.seed)
        rng.shuffle(combos)

        budget = max(0, int(variants_per_sample))
        # 变体预算 0 = 只保留基础样本（每个变量组合的模板 0 号）
        if budget == 0:
            selected = [(0, combos[0])]
        else:
            selected = []
            template_index = 0
            for combo in combos:
                selected.append((template_index % len(templates), combo))
                template_index += 1
                if len(selected) >= budget:
                    break
        if not selected:
            selected = [(0, combos[0])]

        out: List[ConcreteSample] = []
        for role in roles:
            for variant_index, (tpl_idx, combo) in enumerate(selected):
                template = templates[tpl_idx % len(templates)]
                values = {k: _expand_filler(v) for k, v in combo.items()}
                payload = render_template(template, values)
                if not payload.strip() and sample.surface == "chat":
                    continue
                # params/body/path 槽位可引用 {payload}（先渲染载荷再渲染其余）
                render_values = {**values, "payload": payload}
                params = {k: render_template(v, render_values)
                          for k, v in sample.params.items()}
                body = {k: render_template(v, render_values)
                        for k, v in sample.body.items()}
                path = render_template(sample.path, render_values)
                variant_of = ("base" if tpl_idx % len(templates) == 0
                              else "paraphrase")
                if sample.variables and variant_of == "base":
                    variant_of = "variables"
                out.append(ConcreteSample(
                    uid=f"{sample.id}-{role}-v{variant_index}",
                    sample=sample, role=role, payload=payload,
                    params=params, body=body, path=path,
                    variant_index=variant_index, variant_of=variant_of))
        return out


def _expand_filler(value: str) -> str:
    """``__filler(N)__`` → N 个 'A'（超长输入载荷）。"""
    match = _FILLER_RE.match(value)
    if match:
        return "A" * int(match.group(1))
    return value

    # ---- LLM 变体（可选扩展：mock 模式下降级为空） ----

    async def generate_llm_variants(self, sample: AttackSample,
                                    n: int = 3) -> List[ConcreteSample]:
        """用 dsh LLM 接缝生成攻击变体。

        仅当注册了非 mock 适配器时生效；离线/测试环境返回空列表
        （确定性静态变体已保证核心能力）。
        """
        llm = self.ctx.llm
        if not llm or llm.providers() == ["mock"]:
            return []
        raise NotImplementedError("LLM 变体生成（V2 扩展点）：需要目标 prompt 模板")
