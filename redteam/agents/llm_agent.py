"""redteam.agents.llm_agent —— V11：LLM 自主攻击 Agent（确定性多轮驱动循环）。

核心设计：不依赖模型"自觉持续调工具"（dpv4flash 等模型常攻击 1~4 次就
输出文本收尾），而是**确定性循环驱动**：

    循环（直到 finalize / 攻击预算耗尽 / 超时）：
        ① LLM 调用（tools + tool_choice=required 强制模型必须给下一步）；
        ② 解析返回：attack_vector → 执行真实攻击 + 确定性判定 → 记录，
           结果以文本历史回喂 LLM；finalize_report → 记录最终报告并结束；
        ③ 历史持续累积，模型每次决策都基于全部攻击历史。

- opt-in：``engine.llm_agent: true``（需 DEEPSEEK_API_KEY；无 LLM 优雅降级）；
- 安全：攻击次数上限 MAX_LLM_ATTACKS + 超时上限 + 每轮模型调用由确定性
  判定管线把关（LLM 无法"自我判定成功"）；
- 离线可测：script 注入 MockAdapter（每条 script 按调用次序消费），
  驱动完整攻击-判定-收尾序列。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dsh.llm.adapters import LlmCallConfig, LlmRequest
from dsh.llm.messages import Message

from ..config import ScanConfig
from ..models import ConcreteSample, Verdict, VerdictResult
from ..runtime import RedTeamRuntime

log = logging.getLogger("redteam.agents.llm")

MAX_LLM_ATTACKS = 100         # LLM 自主攻击次数上限（默认；可经 engine.llm_agent_max_attacks 调大）
DEFAULT_TIMEOUT_S = 600.0     # agent 循环总超时
MAX_SILENT_TURNS = 2          # 连续无工具调用的轮次上限（防止死循环）
MAX_CONSECUTIVE_FAILS = 12    # 连续失败攻击数上限（覆盖趋近饱和 → 提前收尾提速）
MAX_PROBES = 10               # 每个 Agent 侦察（http_probe）次数上限（防止只猜路径不攻击）
STREAM_INTERVAL_S = 0.4       # LLM 文本流式事件节流间隔（Web 日志可见，不刷屏）


@dataclass
class LlmAgentResult:
    """LLM 自主攻击 Agent 的产出。"""
    verdicts: List[VerdictResult] = field(default_factory=list)
    samples: List[ConcreteSample] = field(default_factory=list)
    final_report: str = ""
    turns: int = 0
    skipped: bool = False       # 无 LLM/超时 → 优雅降级
    agent_id: str = "llm-attacker"

    def to_json(self) -> Dict[str, Any]:
        return {"attacks": len(self.verdicts),
                "final_report": self.final_report,
                "turns": self.turns, "skipped": self.skipped}


class LlmAttackAgent:
    """LLM 自主攻击 Agent：确定性多轮循环中自主决策并执行攻击。

    :param runtime: 红蓝队运行时（判定/存储/地形复用）。
    :param adapter: 目标适配器（真实攻击经由它）。
    :param scan_summary: 扫描摘要（LLM 的决策依据）。
    :param script: 测试注入的 LLM 脚本（MockAdapter script），生产为 None。
    :param agent_id: 并行攻击 Agent 的编号（llm-attacker / llm-attacker-2 …）。
    :param mission_hint: 该 Agent 优先负责的攻击类别（并行分工）。
    :param side_lock: 共享串行锁（状态型攻击跨并行 Agent 互斥）。
    :param max_attacks: 覆盖 engine.llm_agent_max_attacks（并行预算均分用）。
    """

    def __init__(self, runtime: RedTeamRuntime, cfg: ScanConfig,
                 adapter: Any, scan_summary: str,
                 scan_id: str = "", script: Optional[List[Dict[str, Any]]] = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 agent_id: str = "llm-attacker",
                 mission_hint: str = "",
                 side_lock: Optional[asyncio.Lock] = None,
                 max_attacks: Optional[int] = None) -> None:
        self.runtime = runtime
        self.cfg = cfg
        self.adapter = adapter
        self.scan_summary = scan_summary
        self.scan_id = scan_id
        self.script = script
        self.timeout_s = timeout_s
        self.agent_id = agent_id
        self.mission_hint = mission_hint
        self.side_lock = side_lock
        # 攻击上限：参数 > 配置项 engine.llm_agent_max_attacks > 默认 100
        self.max_attacks = max(
            1, int(max_attacks or getattr(cfg.engine, "llm_agent_max_attacks",
                                          MAX_LLM_ATTACKS)))
        self.attacks: List[ConcreteSample] = []
        self.verdicts: List[VerdictResult] = []
        self.final_report = ""
        self.skipped = False
        self._turns = 0
        self._consecutive_fails = 0
        self._probes = 0
        self._tool_calls_total = 0
        self._stop_reason = ""
        # 过程预算（防模型只侦察不攻击 → 拖慢整体扫描）：
        # 轮次上限 ≥8 且 ≥攻击预算；工具调用总量上限 = 攻击预算 × 4
        self.max_turns = max(8, self.max_attacks)
        self.max_tool_calls = self.max_attacks * 4

    # ---- 主流程：确定性多轮驱动循环 ----

    async def run(self) -> LlmAgentResult:
        llm = self._setup_llm()
        if llm is None:
            self.skipped = True
            return LlmAgentResult(skipped=True, agent_id=self.agent_id)
        # 启动即广播：Web 面板日志立刻可见（不再等到全部跑完才出现）
        self.runtime.ctx.emit("agent/dispatched", {
            "agent": self.agent_id, "label": f"LLM 自主攻击[{self.agent_id}]",
            "task": f"LLM 自主攻击（预算 {self.max_attacks} 次）"})
        try:
            await asyncio.wait_for(self._loop(llm), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            self._stop_reason = "超时收敛"
            log.warning("LLM 攻击 Agent[%s] 超时（%.0fs），按已执行攻击收敛",
                        self.agent_id, self.timeout_s)
        self.runtime.ctx.emit("agent/report", {
            "agent": self.agent_id, "label": f"LLM 自主攻击[{self.agent_id}]",
            "task_type": "llm-agent", "extra": {
                "attacks": len(self.verdicts),
                "final_report": self.final_report,
                "stop_reason": self._stop_reason}})
        return LlmAgentResult(verdicts=list(self.verdicts),
                              samples=list(self.attacks),
                              final_report=self.final_report,
                              turns=self._turns, skipped=False,
                              agent_id=self.agent_id)

    def _setup_llm(self) -> Any:
        """装配 LLM：测试注入 script mock；生产注册 DeepSeek（读 .env）。"""
        if self.script is not None:
            from dsh.llm.mock import MockAdapter
            return MockAdapter(script=self.script)
        from dsh.llm.deepseek import DeepSeekAdapter
        adapter = DeepSeekAdapter()
        if not adapter.api_key:
            log.info("无 DEEPSEEK_API_KEY，LLM 自主攻击 Agent 优雅降级为空操作")
            return None
        return adapter

    async def _loop(self, llm: Any) -> None:
        system = self._redteam_system_prompt()
        mission = self._mission_prompt()
        messages: List[Message] = [Message.user(mission)]
        silent_turns = 0
        start = time.time()

        while (len(self.verdicts) < self.max_attacks
                and self._turns < self.max_turns
                and self._tool_calls_total < self.max_tool_calls
                and time.time() - start < self.timeout_s
                and not self.final_report):
            self._turns += 1
            text, calls = await self._llm_call(llm, messages, system)
            # 每轮决策广播：Web 日志实时可见（轮次/工具数/模型输出）
            self.runtime.ctx.emit("llm/turn", {
                "agent": self.agent_id, "turn": self._turns,
                "calls": len(calls), "text": text[:200]})
            if not calls:
                # 模型只输出文本（想收尾）：记入历史，给一次继续机会
                silent_turns += 1
                messages.append(Message.user(
                    (text or "请继续攻击或调用 finalize_report 收尾")[:400]))
                if silent_turns >= MAX_SILENT_TURNS:
                    self._stop_reason = "模型连续输出文本收尾"
                    break
                continue
            silent_turns = 0
            for name, args in calls:
                if self.final_report:
                    break
                # 工具调用广播：模型每决定一次攻击/侦察都推给日志
                self.runtime.ctx.emit("llm/tool", {
                    "agent": self.agent_id, "tool": name,
                    "args": str(args)[:160]})
                if name == "attack_vector":
                    verdict = await self._execute_attack(args)
                    if verdict is not None:
                        self._tool_calls_total += 1
                        self._track_fail(verdict)
                        messages.append(Message.user(
                            f"[攻击#{len(self.verdicts)}] {verdict.category} → "
                            f"{verdict.verdict} ｜ 证据: {verdict.evidence[:140]}。"
                            f"继续攻击新的类别/路径/载荷变体；"
                            f"下一轮一次批量给出 3~5 个不同类别的攻击调用；"
                            f"全部完成后调用 finalize_report 提交总结。"))
                elif name == "http_probe":
                    self._tool_calls_total += 1
                    if self._probes >= MAX_PROBES:
                        # 侦察配额用尽：强制转攻击，防止只猜路径拖慢扫描
                        messages.append(Message.user(
                            f"[侦察配额] 已用 {self._probes}/{MAX_PROBES} 次，"
                            f"侦察结束。必须立即用 attack_vector / http_attack "
                            f"发起攻击，不要再探测路径。"))
                        continue
                    self._probes += 1
                    probe_text = await self._execute_http_probe(args)
                    messages.append(Message.user(
                        f"[侦察{self._probes}/{MAX_PROBES}] {probe_text[:300]}。"
                        f"侦察仅剩 {MAX_PROBES - self._probes} 次，"
                        f"尽快转为攻击。"))
                elif name == "http_attack":
                    verdict = await self._execute_http_attack(args)
                    if verdict is not None:
                        self._tool_calls_total += 1
                        self._track_fail(verdict)
                        messages.append(Message.user(
                            f"[主动攻击#{len(self.verdicts)}] {verdict.verdict}"
                            f" ｜ 证据: {verdict.evidence[:140]}。"
                            f"失败就换路径/载荷，成功就深入同类攻击面；"
                            f"下一轮一次批量给出 3~5 个攻击/侦察调用；"
                            f"全部完成后调用 finalize_report 提交总结。"))
                elif name == "host_scan":
                    self._tool_calls_total += 1
                    text = await self._execute_host_scan(args)
                    messages.append(Message.user(
                        f"[基础设施扫描] {text[:500]}。\n"
                        f"根据开放端口/服务，用 service_fp / vuln_scan 深入；"
                        f"发现可攻击服务就用 http_attack / attack_vector 利用。"))
                elif name == "service_fp":
                    self._tool_calls_total += 1
                    text = await self._execute_service_fp(args)
                    messages.append(Message.user(
                        f"[服务指纹] {text[:500]}。\n"
                        f"发现敏感路径/泄露面就继续利用。"))
                elif name == "vuln_scan":
                    self._tool_calls_total += 1
                    text = await self._execute_vuln_scan(args)
                    messages.append(Message.user(
                        f"[未授权/高危] {text[:500]}。\n"
                        f"命中漏洞就尝试利用/深入该服务。"))
                elif name == "deep_vuln":
                    self._tool_calls_total += 1
                    text = await self._execute_deep_vuln(args)
                    messages.append(Message.user(
                        f"[已知CVE/深层次] {text[:500]}。\n"
                        f"命中高危漏洞就尝试利用/深入。"))
                elif name == "version_cve":
                    self._tool_calls_total += 1
                    text = await self._execute_version_cve(args)
                    messages.append(Message.user(
                        f"[版本CVE精确匹配] {text[:500]}。\n"
                        f"命中已知 CVE 就尝试利用/深入该服务。"))
                elif name == "finalize_report":
                    try:
                        parsed = json.loads(args) if args else {}
                        self.final_report = str(
                            parsed.get("summary", "") or args or "")[:3000]
                    except (ValueError, TypeError):
                        self.final_report = str(args)[:3000]
            if self._consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                self._stop_reason = (f"连续 {self._consecutive_fails} 次攻击失败，"
                                     f"覆盖趋近饱和提前收尾")
                self.runtime.ctx.emit("llm/stop", {
                    "agent": self.agent_id,
                    "reason": self._stop_reason})
                break
            self._compact_history(messages)
        if not self.final_report and not self._stop_reason:
            if self._turns >= self.max_turns:
                self._stop_reason = f"轮次预算耗尽（{self.max_turns} 轮）"
            elif self._tool_calls_total >= self.max_tool_calls:
                self._stop_reason = (f"工具调用预算耗尽"
                                     f"（{self.max_tool_calls} 次）")
            if self._stop_reason:
                self.runtime.ctx.emit("llm/stop", {
                    "agent": self.agent_id, "reason": self._stop_reason})

    def _track_fail(self, verdict: VerdictResult) -> None:
        """连续失败计数（覆盖饱和 → 提前收尾提速）。"""
        if verdict.verdict == Verdict.SUCCESS.value:
            self._consecutive_fails = 0
        elif verdict.verdict == Verdict.FAILED.value:
            self._consecutive_fails += 1

    def _compact_history(self, messages: List[Message]) -> None:
        """历史压缩：上下文只保留任务 + 最近 12 条结果，防止每轮延迟随历史线性增长。"""
        if len(messages) <= 30:
            return
        done, hit = len(self.verdicts), sum(
            1 for v in self.verdicts if v.success)
        summary = (f"[历史压缩] 此前已执行 {done} 次攻击，命中 {hit} 次；"
                   f"未命中的类别/载荷无需重复，继续攻击剩余攻击面。")
        messages[1:-12] = [Message.user(summary)]

    # ---- 单步：LLM 调用与解析 ----

    async def _llm_call(self, llm: Any, messages: List[Message],
                        system: str) -> "tuple[str, List[tuple]]":
        """一次 LLM 调用，返回 (文本, [(工具名, 参数JSON)])。

        - max_tokens 取 engine.llm_agent_max_tokens（默认 800）：允许模型一轮
          批量返回 3~5 个工具调用，减少 API 往返次数（提速核心）；
        - 文本增量按 STREAM_INTERVAL_S 节流广播 llm/output 事件（Web 日志流式可见）。
          注意：不能叫 llm/stream——那是 dsh LlmRuntime 的 waterfall 派发通道，
          同名 emit 监听器会让叙事综述的 LLM 流返回 None。
        """
        max_tokens = max(
            100, int(getattr(self.cfg.engine, "llm_agent_max_tokens", 800)))
        request = LlmRequest(
            config=LlmCallConfig(provider="deepseek",
                                 model=os.environ.get("DEEPSEEK_MODEL",
                                                      "deepseek-chat"),
                                 max_tokens=max_tokens, temperature=0.6),
            messages=list(messages), tools=self._tool_schemas(), system=system)
        text, tool_calls = "", []
        current: Optional[Dict[str, str]] = None
        last_stream = 0.0
        async for chunk in llm.stream(request):
            if getattr(chunk, "text", None):
                text += chunk.text
                now = time.time()
                if now - last_stream >= STREAM_INTERVAL_S:
                    last_stream = now
                    self.runtime.ctx.emit("llm/output", {
                        "agent": self.agent_id, "turn": self._turns,
                        "text": text[-160:]})
            tc = getattr(chunk, "tool_call", None)
            if tc:
                index = tc.get("index", 0)
                if current is None or int(current.get("index", -1)) != index:
                    current = {"index": str(index), "name": "", "arguments": ""}
                    tool_calls.append(current)
                if tc.get("name"):
                    current["name"] += tc["name"]
                if tc.get("arguments"):
                    current["arguments"] += tc["arguments"]
        return text.strip(), [(t["name"], t["arguments"])
                              for t in tool_calls if t["name"]]

    def _tool_schemas(self) -> List[Dict[str, Any]]:
        """攻击/收尾工具；explorer 模式额外暴露主动侦察工具（http_probe/http_attack）。"""
        tools: List[Dict[str, Any]] = [
            {"type": "function",
             "function": {"name": "attack_vector",
                          "description": "向目标系统发起一次攻击载荷，返回"
                                         "判定结果（success/failed/suspicious 与证据）。",
                          "parameters": {"type": "object",
                                         "properties": {
                                             "category": {"type": "string",
                                                          "description": "攻击类别（如 direct_injection / sqli / path_traversal）"},
                                             "payload": {"type": "string",
                                                          "description": "攻击载荷文本"}},
                                         "required": ["category", "payload"]}}},
            {"type": "function",
             "function": {"name": "finalize_report",
                          "description": "提交最终攻击报告（总结攻击结论）并结束任务。",
                          "parameters": {"type": "object",
                                         "properties": {
                                             "summary": {"type": "string",
                                                         "description": "攻击结论总结"}},
                                         "required": ["summary"]}}},
        ]
        if getattr(self.cfg.engine, "llm_explorer_tools", False):
            tools.insert(1, {"type": "function",
                             "function": {"name": "http_probe",
                                          "description": "探测目标端点（GET），返回状态码与响应截断，用于主动侦察攻击面。",
                                          "parameters": {"type": "object",
                                                         "properties": {
                                                             "path": {"type": "string",
                                                                      "description": "路径（如 /api/users、/.env、/../etc/passwd）"}},
                                                         "required": ["path"]}}})
            tools.insert(2, {"type": "function",
                             "function": {"name": "http_attack",
                                          "description": "向任意路径发起原始 HTTP 攻击请求，返回确定性判定（敏感泄露/异常状态）。",
                                          "parameters": {"type": "object",
                                                         "properties": {
                                                             "method": {"type": "string",
                                                                        "description": "GET/POST/PUT/DELETE"},
                                                             "path": {"type": "string",
                                                                      "description": "攻击路径"},
                                                             "payload": {"type": "string",
                                                                         "description": "攻击载荷（请求体/查询参数内容）"}},
                                                         "required": ["method", "path", "payload"]}}})
            tools.insert(3, {"type": "function",
                             "function": {"name": "host_scan",
                                          "description": "对目标主机做 TCP 端口扫描 + 服务 banner 识别，返回开放端口与对应服务（服务器层面渗透的信息收集）。",
                                          "parameters": {"type": "object",
                                                         "properties": {
                                                             "host": {"type": "string",
                                                                      "description": "目标主机/IP（默认当前目标）"},
                                                             "ports": {"type": "array", "items": {"type": "integer"},
                                                                       "description": "可选指定端口列表"}}}}})
            tools.insert(4, {"type": "function",
                             "function": {"name": "service_fp",
                                          "description": "对指定端口做 HTTP 服务指纹 + 敏感路径探测（Actuator/备份/.git/管理后台/API文档等泄露面）。",
                                          "parameters": {"type": "object",
                                                         "properties": {
                                                             "port": {"type": "integer",
                                                                      "description": "目标端口（如 8080）"}},
                                                         "required": ["port"]}}})
            tools.insert(5, {"type": "function",
                             "function": {"name": "vuln_scan",
                                          "description": "对开放端口做常见未授权/高危检测（Redis/MongoDB/Memcached/Docker API/Elasticsearch/Nacos 未授权、Spring Actuator 泄露、Shiro 指纹）。",
                                          "parameters": {"type": "object",
                                                         "properties": {
                                                             "ports": {"type": "array", "items": {"type": "integer"},
                                                                       "description": "要检测的开放端口列表"}}}}})
            tools.insert(6, {"type": "function",
                             "function": {"name": "deep_vuln",
                                          "description": "对开放端口做深层次已知高危 CVE 检测（Spring Actuator env/heapdump 泄露、Shiro rememberMe 反序列化、Struts2/WebLogic/Tomcat 利用面、Redis 未授权等）。",
                                          "parameters": {"type": "object",
                                                         "properties": {
                                                             "ports": {"type": "array", "items": {"type": "integer"},
                                                                       "description": "要检测的开放端口"},
                                                             "host": {"type": "string",
                                                                      "description": "目标主机（默认当前目标）"}}}}})
            tools.insert(7, {"type": "function",
                             "function": {"name": "version_cve",
                                          "description": "从开放端口的服务 banner 提取版本号，精确匹配已知 CVE（Nginx/Apache/OpenSSH/Tomcat/Redis 等版本区间，如 CVE-2021-23017/CVE-2024-6387）。对扫出的服务版本做精确打击。",
                                          "parameters": {"type": "object",
                                                         "properties": {
                                                             "ports": {"type": "array", "items": {"type": "integer"},
                                                                       "description": "要匹配的开放端口列表"}}}}})
        return tools

    async def _execute_attack(self, args: str) -> Optional[VerdictResult]:
        """执行一次攻击（category/payload → 真实攻击 → 确定性判定）。"""
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {}
        category = str(parsed.get("category", "")).strip()
        payload = str(parsed.get("payload", "")).strip()
        if not category or not payload:
            return None
        if len(self.verdicts) >= self.max_attacks:
            return None
        base = self.runtime.registry.sample_by_id(
            _pick_sample_id(self.runtime, category))
        if base is None:
            return None
        sample = ConcreteSample(
            uid=f"llm-agent-{category}-{len(self.verdicts)}",
            sample=base, role="student", payload=payload,
            variant_index=990 + len(self.verdicts),
            variant_of="llm-agent")
        from ..engine.scan import execute_sample
        needs_lock = ("side_effect" in base.expected_signals
                      or base.stateful)
        if needs_lock and self.side_lock is not None:
            async with self.side_lock:      # 并行 Agent 共享串行通道
                verdict, response = await execute_sample(
                    self.runtime, self.cfg, self.adapter, sample, reset=True)
        else:
            verdict, response = await execute_sample(
                self.runtime, self.cfg, self.adapter, sample, reset=needs_lock)
        self.attacks.append(sample)
        self.verdicts.append(verdict)
        # 每次攻击实时广播：Web 日志逐条可见（判定 + 证据）
        self.runtime.ctx.emit("attack/executed", {
            "agent": self.agent_id, "sample": sample.describe(),
            "status": response.status, "response": response.snippet(300)})
        self.runtime.ctx.emit("attack/verdict", verdict)
        return verdict

    # ---- Explorer 工具：主动侦察与原始 HTTP 攻击（engine.llm_explorer_tools） ----

    def _target_host(self) -> str:
        """从目标 URL 解析主机名/IP（基础设施渗透的默认目标）。"""
        from urllib.parse import urlparse
        base = (getattr(self.adapter, "base_url", "") or
                self.cfg.target.base_url or "")
        try:
            return urlparse(base).hostname or base
        except Exception:
            return base

    async def _execute_host_scan(self, args: str) -> str:
        """host_scan：TCP 端口扫描 + 服务识别（服务器层面信息收集）。"""
        from ..infra import infra_scan, summarize
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {}
        host = str(parsed.get("host", "")).strip() or self._target_host()
        ports = parsed.get("ports")
        try:
            result = infra_scan(host, ports=ports, timeout=1.5)
            return summarize(result)
        except Exception as exc:
            return f"host_scan 失败: {exc}"

    async def _execute_service_fp(self, args: str) -> str:
        """service_fp：HTTP 服务指纹 + 敏感路径探测。"""
        from ..infra import http_fingerprint, probe_sensitive
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {}
        port = int(parsed.get("port") or 80)
        host = self._target_host()
        scheme = "https" if port == 443 else "http"
        fp = http_fingerprint(host, port, scheme)
        lines = [
            f"指纹 :{port} server={fp.get('server','')} "
            f"title={fp.get('title','')} "
            f"框架={'、'.join(fp.get('frameworks') or []) or '-'}",
        ]
        if not fp.get("error"):
            for s in probe_sensitive(host, port, scheme)[:8]:
                lines.append(f"敏感路径 {s.get('path')} HTTP {s.get('status')} "
                             f"{s.get('note','')}")
        return "\n".join(lines)

    async def _execute_vuln_scan(self, args: str) -> str:
        """vuln_scan：对开放端口做未授权/高危检测。"""
        from ..infra import run_vuln_checks
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {}
        ports = parsed.get("ports") or []
        host = self._target_host()
        checks = run_vuln_checks(host, [int(p) for p in ports])
        vulns = [c for c in checks if c.get("status") == "vuln"]
        if not vulns:
            return "未授权/高危检测未命中"
        return "\n".join(f"[{c.get('check')}] {c.get('detail','')}"
                         for c in vulns)

    async def _execute_deep_vuln(self, args: str) -> str:
        """deep_vuln：深层次已知高危 CVE 检测。"""
        from ..infra.cve import run_cve_checks, summarize_cve
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {}
        ports = parsed.get("ports") or []
        host = str(parsed.get("host", "")).strip() or self._target_host()
        checks = run_cve_checks(host, [int(p) for p in ports])
        return summarize_cve(checks)

    async def _execute_version_cve(self, args: str) -> str:
        """version_cve：从端口服务 banner 提取版本精确匹配已知 CVE。"""
        from ..infra import infra_scan
        from ..infra.cve_version import run_version_cves, summarize_version_cves
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {}
        ports = parsed.get("ports")
        host = str(parsed.get("host", "")).strip() or self._target_host()
        try:
            result = infra_scan(host, ports=ports, timeout=1.5)
            open_ports = result.get("open_ports", []) or []
            checks = run_version_cves(open_ports)
            return summarize_version_cves(checks)
        except Exception as exc:
            return f"version_cve 失败: {exc}"

    async def _execute_http_probe(self, args: str) -> str:
        """http_probe：GET 任意路径，返回状态码/响应头/响应截断（侦察用，不落判定）。"""
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {}
        path = str(parsed.get("path", "")).strip() or "/"
        url = self._raw_url(path)
        try:
            import httpx
            async with httpx.AsyncClient(
                    timeout=self.cfg.target.timeout_s,
                    follow_redirects=False) as client:
                resp = await client.get(url)
            header_notes = "、".join(
                f"{k}={v}" for k, v in resp.headers.items()
                if k.lower() in {"location", "server", "x-powered-by",
                                 "www-authenticate", "content-type"})
            text = (resp.text or "").strip()
            snippet = text[:160].replace("\n", " ")
            return (f"GET {path} → HTTP {resp.status_code}"
                    f"{' ｜ 头: ' + header_notes if header_notes else ''}"
                    f" ｜ 响应: {snippet if snippet else '(空)'}")
        except Exception as exc:
            return f"GET {path} → 探测失败: {exc}"

    async def _execute_http_attack(self, args: str
                                   ) -> Optional[VerdictResult]:
        """http_attack：对任意路径发起原始 HTTP 攻击，由确定性信号管线判定。"""
        from ..detector.signals import check_leak_patterns
        from ..models import (AttackSample, Signal, TargetResponse, Verdict,
                              now_iso)
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {}
        method = str(parsed.get("method", "GET")).strip().upper() or "GET"
        path = str(parsed.get("path", "")).strip() or "/"
        payload = str(parsed.get("payload", "")).strip()
        if method not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
            method = "GET"
        if len(self.verdicts) >= self.max_attacks:
            return None
        url = self._raw_url(path)
        response = TargetResponse(status=0)

        async def _send() -> TargetResponse:
            import httpx
            kwargs: Dict[str, Any] = {}
            if method in {"POST", "PUT", "PATCH"}:
                try:
                    parsed_body = json.loads(payload)
                except ValueError:
                    parsed_body = None
                if isinstance(parsed_body, (dict, list)):
                    kwargs["json"] = parsed_body       # JSON API（如 /api/ping）
                else:
                    kwargs["content"] = payload.encode("utf-8", "replace")
                    kwargs["headers"] = {
                        "Content-Type": "application/x-www-form-urlencoded"}
            elif payload:
                kwargs["params"] = {"q": payload}
            async with httpx.AsyncClient(
                    timeout=self.cfg.target.timeout_s,
                    follow_redirects=False) as client:
                resp = await client.request(method, url, **kwargs)
            return TargetResponse(
                status=resp.status_code, text=resp.text or "",
                headers=dict(resp.headers))

        try:
            if self.side_lock is not None:
                async with self.side_lock:   # 并行 Agent 串行化原始攻击
                    response = await _send()
            else:
                response = await _send()
        except Exception as exc:
            response = TargetResponse(status=0, text=f"(请求失败: {exc})")
        text = response.text or ""
        leak = check_leak_patterns(text)
        signals: List[Signal] = [leak]
        if leak.hit:
            verdict_name = Verdict.SUCCESS.value
            confidence = 0.85
        elif response.status >= 500:
            verdict_name = Verdict.SUSPICIOUS.value
            confidence = 0.6
            signals.append(Signal(
                name="server_error", hit=True, confidence=0.6,
                evidence=f"HTTP {response.status} 服务端异常"))
        else:
            verdict_name = Verdict.FAILED.value
            confidence = 0.9
        index = len(self.verdicts)
        sample = ConcreteSample(
            uid=f"llm-explored-{index}",
            sample=AttackSample(
                id=f"llm-explored-{index}", category="llm_explored",
                name=f"LLM 主动攻击（{method} {path}）", severity="medium",
                surface="api", method=method, path=path,
                expected_signals=["leak_pattern"]),
            role="student", payload=payload, path=path,
            variant_index=990 + index, variant_of="llm_explored")
        verdict = VerdictResult(
            sample_uid=sample.uid, category="llm_explored", role="student",
            verdict=verdict_name, confidence=confidence, signals=signals,
            evidence=(leak.evidence if leak.hit
                      else response.snippet(240)),
            created_at=now_iso())
        self.attacks.append(sample)
        self.verdicts.append(verdict)
        self.runtime.ctx.emit("attack/executed", {
            "agent": self.agent_id, "sample": sample.describe(),
            "status": response.status,
            "response": response.snippet(300)})
        self.runtime.ctx.emit("attack/verdict", verdict)
        return verdict

    def _raw_url(self, path: str) -> str:
        base = (getattr(self.adapter, "base_url", "") or
                self.cfg.target.base_url or "").rstrip("/")
        if not base:
            raise ValueError("目标未提供可探测的 HTTP 基地址")
        return base + (path if path.startswith("/") else "/" + path)

    # ---- 提示词 ----

    def _reference_samples(self, max_total: int = 20) -> str:
        """从攻击样本库取代表性样本作为 LLM 的参考攻击手法。"""
        lines: List[str] = []
        seen: set = set()
        for sample in self.runtime.registry.samples:
            if sample.category in seen or len(lines) >= max_total:
                continue
            payload = (sample.payload or "").strip()
            if not payload:
                continue
            seen.add(sample.category)
            lines.append(
                f"- [{sample.category}](面:{sample.surface}·目标:"
                f"{sample.target_point or '通用'}) 载荷示例: {payload[:140]}")
        return "\n".join(lines) if lines else "(样本库为空)"

    def _mission_prompt(self) -> str:
        refs = self._reference_samples()
        explorer = getattr(self.cfg.engine, "llm_explorer_tools", False)
        batch = ("3. 每轮一次批量返回 3~5 个不同类别/路径的工具调用"
                 "（少说废话、直接给工具调用，减少来回次数）；\n")
        if explorer:
            rules = (
                "1. http_probe 侦察最多 10 次（只确认关键端点），"
                "把主要精力放在攻击上，不要反复猜路径；\n"
                "2. 用 attack_vector / http_attack 持续多轮攻击，越多越好；\n"
                + batch +
                "4. 每轮攻击后根据返回的判定调整策略：失败就换攻击点，成功就深入同类；\n"
                "5. 全部攻击完成后，必须调用 finalize_report 提交最终攻击报告"
                "（总结发现、成功载荷、修复建议）。")
            intro = (
                "【主动侦察】你额外拥有 http_probe（探测任意路径）与 "
                "http_attack（对任意路径/方法发起原始 HTTP 攻击）两个工具，"
                "可以主动寻找样本库之外的攻击面。")
        else:
            rules = (
                "1. 每轮用 attack_vector 工具发起攻击，持续多轮、越多越好；\n"
                "2. 每次换不同的 category / payload / 路径 / 措辞，避免重复；\n"
                + batch +
                "4. 每轮攻击后根据返回的判定调整策略：失败就换攻击点，成功就深入同类；\n"
                "5. 全部攻击完成后，必须调用 finalize_report 提交最终攻击报告"
                "（总结发现、成功载荷、修复建议）。")
            intro = ""
        hint = (f"\n【分工】你优先负责这些攻击类别：{self.mission_hint}。"
                f"先覆盖分工内的类别，再自由扩展其他攻击面。\n"
                if self.mission_hint else "")
        return (
            "你是红队攻击 Agent（自主攻击决策者），任务是发现目标尽可能多的真实漏洞。\n\n"
            "【参考攻击手法】以下是你所在攻击系统样本库中的示例（类别 · 攻击面/目标点 + "
            "载荷模板）。请模仿这些手法，结合当前目标自由组合、变体、换措辞/路径/参数，"
            "生成你自己的攻击载荷：\n"
            f"{refs}\n\n"
            f"{intro}\n"
            f"{hint}\n"
            "【执行规则（必须遵守）】\n"
            f"{rules}\n"
            "【扫描摘要】\n" + self.scan_summary[:3000])

    def _redteam_system_prompt(self) -> str:
        explorer = getattr(self.cfg.engine, "llm_explorer_tools", False)
        batch = ("效率要求：每轮一次返回 3~5 个不同类别的工具调用，"
                 "不要只输出文字，尽量少说话多调用工具。")
        if explorer:
            return (
                "你是 dsh-red-blue-team 的红队攻击 Agent（自主攻击决策者）。\n"
                "你的全部能力只有四个工具：\n"
                "- attack_vector: 向目标发起一次攻击载荷，返回判定结果（含证据）；\n"
                "- http_probe: 探测目标任意路径（GET），返回状态码与响应截断；\n"
                "- http_attack: 对任意路径/方法发起原始 HTTP 攻击，返回确定性判定；\n"
                "- finalize_report: 提交最终攻击报告并结束任务。\n"
                "你没有文件系统、没有 shell——只能通过这四个工具攻击目标；"
                "http_probe/http_attack 的判定同样由系统确定性管线把关，"
                "你自己不能宣称攻击成功。\n"
                "行为准则：先侦察再攻击；持续多轮，尽可能多地发现漏洞；"
                "每次攻击后分析判定结果并调整策略；全部完成后调用 "
                "finalize_report 提交总结。\n" + batch)
        return (
            "你是 dsh-red-blue-team 的红队攻击 Agent（自主攻击决策者）。\n"
            "你的全部能力只有两个工具：\n"
            "- attack_vector: 向目标发起一次攻击载荷，返回判定结果（含证据）；\n"
            "- finalize_report: 提交最终攻击报告并结束任务。\n"
            "你没有文件系统、没有 shell、没有网络工具——只能通过 attack_vector "
            "攻击目标。\n"
            "行为准则：持续多轮攻击，尽可能多地发现漏洞；每次攻击后分析判定结果"
            "并调整策略；全部完成后调用 finalize_report 提交总结。\n" + batch)


def _pick_sample_id(runtime: RedTeamRuntime, category: str) -> str:
    """按类别挑一个基础样本（优先对话样本）作为载荷模板。"""
    candidates = [s for s in runtime.registry.samples
                  if s.category == category]
    chat = [s for s in candidates if s.surface == "chat"]
    for sample in chat or candidates:
        return sample.id
    return ""
