"""redteam.engine.scan —— 扫描入口（主 Agent 编排的薄封装）。

ScanRunner 是公开 API：内部把整场扫描委托给主 Agent（AttackOrchestrator），
由其派发侦察/静态/攻击子 Agent 并行执行并汇总攻击报告。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence

from ..adapters import HttpAdapter, SdkAdapter, TargetAdapter
from ..config import ScanConfig
from ..errors import ConfigError, UnsupportedSurface
from ..models import (ConcreteSample, Finding, TargetResponse, Verdict,
                      VerdictResult, now_iso)
from ..runtime import RedTeamRuntime

log = logging.getLogger("redteam.engine")

#: 对话攻击的中性基线消息（判定"行为偏离"的参照系）
BASELINE_MESSAGE = "你好，请介绍一下你自己。"


def build_adapter(cfg: ScanConfig,
                  sdk_handle: Optional[Any] = None) -> Optional[TargetAdapter]:
    """按目标配置构造适配器。

    - http/lab → HttpAdapter；sdk → SdkAdapter；
    - mcp → McpAdapter（stdio 工具面，复用 dsh.mcp）；
    - folder（本地文件夹静态扫描）→ None（静态子代理接管）。
    """
    target = cfg.target
    if target.type == "folder":
        return None
    if target.type == "mcp":
        from ..adapters.mcp_adapter import McpAdapter
        return McpAdapter(base_url=target.base_url,
                          command=target.mcp_command,
                          headers=target.headers, timeout_s=target.timeout_s)
    if target.type == "sdk" or sdk_handle is not None:
        if sdk_handle is None:
            raise ConfigError("sdk 类型目标需要传入 sdk_handle（靶场直连句柄）")
        return SdkAdapter(base_url=target.base_url, handle=sdk_handle,
                          headers=target.headers, timeout_s=target.timeout_s,
                          side_effect_token=target.side_effect_token)
    return HttpAdapter(base_url=target.base_url, headers=target.headers,
                       timeout_s=target.timeout_s, chat_path=target.chat_path,
                       side_effect_path=target.side_effect_path,
                       side_effect_token=target.side_effect_token,
                       admin_token=target.admin_token)


def finding_from_verdict(scan_id: str, verdict: VerdictResult,
                         sample: ConcreteSample, seq: int) -> Finding:
    from ..blueteam.templates import fix_template_for
    template = fix_template_for(sample.category)
    return Finding(
        finding_id=f"F-{seq:03d}",
        scan_id=scan_id,
        category=sample.category,
        owasp=sample.sample.owasp,
        severity=sample.sample.severity,
        sample_id=sample.sample.id,
        sample_uid=sample.uid,
        payload=sample.payload,
        role=sample.role,
        chain=verdict.chain,
        evidence=verdict.evidence,
        signals={s.name: {"hit": s.hit, "evidence": s.evidence}
                 for s in verdict.signals if s.hit},
        confidence=verdict.confidence,
        fix={"auto_fixable": bool(template and template.auto_fixable),
             "template": template.template_id if template else "",
             "plan": template.title if template else "需人工研判",
             "status": "pending"},
    )


class ScanRunner:
    """一次扫描任务的入口（委托主 Agent 编排）。

    :param order: "adaptive"（wanter 地形优先级）/ "random"（bench 基线）/
                  "default"（注册表稳定序）。
    """

    def __init__(self, runtime: RedTeamRuntime, cfg: ScanConfig,
                 adapter: Optional[TargetAdapter] = None,
                 scan_mode: str = "full", order: str = "adaptive") -> None:
        self.runtime = runtime
        self.cfg = cfg
        self.adapter = adapter
        self.scan_mode = scan_mode
        self.order = order
        self.scan_id = ""

    async def run(self) -> "ScanResult":
        from ..agents.orchestrator import AttackOrchestrator
        orchestrator = AttackOrchestrator(
            runtime=self.runtime, cfg=self.cfg, adapter=self.adapter,
            scan_mode=self.scan_mode, order=self.order)
        result = await orchestrator.run()
        self.scan_id = result.scan_id
        return result


async def execute_sample(runtime: RedTeamRuntime, cfg: ScanConfig,
                         adapter: TargetAdapter, sample: ConcreteSample,
                         reset: bool = False
                         ) -> "tuple[VerdictResult, TargetResponse]":
    """执行一条攻击样本（攻击 + 判定），供攻击子代理与回归复测共用。

    :param reset: 执行前重置目标状态（状态型样本隔离用）。
    :return: (判定结果, 目标原始响应)
    """
    if reset:
        await adapter.reset()
    if cfg.engine.min_interval_ms:
        await asyncio.sleep(cfg.engine.min_interval_ms / 1000.0)
    chain: List[Dict[str, Any]] = []
    baseline_text = ""
    pre = post = None

    if cfg.detector.baseline and sample.surface == "chat":
        try:
            base = await adapter.send_text(BASELINE_MESSAGE, role=sample.role)
            baseline_text = base.text
            chain.append({"turn": 1, "role": "user", "msg": BASELINE_MESSAGE,
                          "resp": base.snippet(300), "status": base.status})
        except Exception as exc:  # 基线失败不阻断攻击
            log.debug("基线消息失败: %s", exc)

    # 多轮攻击链：铺垫消息逐条发送（上下文诱导），每轮记录审计
    for prelude_msg in sample.prelude:
        try:
            prelude_resp = await adapter.send_text(prelude_msg, role=sample.role)
        except UnsupportedSurface:
            raise  # 目标不支持对话（如 MCP）：整条链跳过
        except Exception as exc:
            log.debug("攻击链铺垫消息失败: %s", exc)
            prelude_resp = TargetResponse(status=0, text=f"(铺垫失败: {exc})")
        chain.append({"turn": len(chain) + 1, "role": sample.role,
                      "msg": prelude_msg, "resp": prelude_resp.snippet(300),
                      "status": prelude_resp.status, "kind": "prelude"})

    expect_side = "side_effect" in sample.sample.expected_signals
    if expect_side:
        pre = await adapter.check_side_effect()

    try:
        response = TargetResponse(status=0, text="")
        for attempt in range(max(1, sample.sample.repeat)):
            response = await adapter.send(sample)
            chain.append({"turn": len(chain) + 1, "role": sample.role,
                          "msg": sample.payload,
                          "resp": response.snippet(600),
                          "status": response.status,
                          "attempt": attempt + 1})
    except UnsupportedSurface:
        raise  # 由调用方（攻击子代理/回归）处理为 skipped
    except Exception as exc:
        result = VerdictResult(
            sample_uid=sample.uid, category=sample.category,
            role=sample.role, verdict=Verdict.ERROR.value, confidence=1.0,
            evidence=f"请求异常: {exc}", chain=chain, created_at=now_iso())
        return result, TargetResponse(status=0, text=f"请求异常: {exc}")

    if expect_side and pre is not None and pre.available:
        post = await adapter.check_side_effect()
    side_delta = pre.delta(post) if (pre and post and pre.available
                                     and post.available) else None

    verdict = await runtime.detector.evaluate(
        sample, response, baseline_text=baseline_text,
        side_effect_delta=side_delta, chain=chain)
    return verdict, response
