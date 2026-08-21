"""
dsh.wanter.plugin —— WanterPlugin：四法则 + 深化方向挂到既有扩展点（零循环修改）。

- 水滴坐标：每会话一个虚拟水滴；每次 turn 结束按状态摘要经 coordinator
  平滑更新坐标（lerp 0.5）+ 沿梯度下移一步；
- ② 沉积/淤积：工具成功 → 正沉积（刻蚀河道）；工具失败 → **负沉积淤积**
  （泥沙抬高地形，反向刻蚀）；权重可配置；
- 奖励连续化：接 `message-feedback`——用户 up → 在对应消息时刻的位置强化
  沉积；down → 淤积；
- ④ 驻留/侵蚀：turn-stopping 观测位置 → 停滞则侵蚀 + steer；
- ③ 多目标：`wanter_goal_add/list/complete` 工具（goal 描述经 coordinator
  映射坐标；complete 移除最近目标势阱 = 子任务完成）。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from ..kernel import Service
from ..prompt import PromptContext
from ..tools import define_tool

log = logging.getLogger("dsh.wanter")


def _session_position(session_id: str, dim: int) -> tuple:
    """会话 id 确定性哈希 → 初始水滴坐标（[-2,2]^dim）。"""
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    return tuple((digest[i] / 255.0) * 4.0 - 2.0
                 for i in range(min(dim, len(digest))))


class WanterPlugin(Service):
    """wanter 集成插件。"""

    inject = ("wanter", "systemPrompt")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        config = config or {}
        self._positions: Dict[str, tuple] = {}
        self._position_history: Dict[str, Dict[int, tuple]] = {}
        self._feedback_seen: set = set()
        self._disposers: List[Any] = []
        self.weights = {
            "success": float(config.get("success_weight", 1.0)),
            "failure": float(config.get("failure_weight", -0.5)),
            "feedback_up": float(config.get("feedback_up_weight", 2.0)),
            "feedback_down": float(config.get("feedback_down_weight", -1.5)),
        }
        self.coordinator_blend = float(config.get("coordinator_blend", 0.5))
        self.history_cap = int(config.get("history_cap", 200))

    def apply(self, ctx) -> None:
        engine = ctx.wanter

        def position_of(session_id: str) -> tuple:
            position = self._positions.setdefault(
                session_id, _session_position(session_id, engine.dim))
            engine.set_position(session_id, position)  # 供 goal_complete 等消费
            return position

        # ③ 动态上下文：模型可见地形状态
        context = PromptContext(
            name="wanter", order=15,
            text=lambda ac: self._render_state(ac, engine, position_of))
        self._disposers.append(ctx.systemPrompt.context(context))

        # 位置历史（feedback 回填用）：assistant/message 的 seq → 当时位置
        # 容量上限 history_cap（feedback 通常只针对最近消息）。
        def on_session_event(session, event) -> None:
            if event.type != "assistant/message":
                return
            pos = position_of(session.id)
            history = self._position_history.setdefault(session.id, {})
            history[event.seq] = pos
            if len(history) > self.history_cap:
                oldest = sorted(history)[:len(history) - self.history_cap]
                for seq in oldest:
                    del history[seq]
        self._disposers.append(ctx.on("session/event", on_session_event))

        # 会话销毁 → 清理该会话的位置/历史/feedback 去重集（防泄漏）
        def on_agent_disposed(payload: Dict[str, Any]) -> None:
            agent = payload.get("agent")
            if agent is None:
                return
            self._positions.pop(agent.id, None)
            self._position_history.pop(agent.id, None)
            stale = {key for key in self._feedback_seen
                     if key[0] == agent.id}
            for key in stale:
                self._feedback_seen.discard(key)
        self._disposers.append(ctx.on("agent/disposed", on_agent_disposed))

        # ② 沉积/淤积：成功 → 正沉积；失败 → 淤积
        def on_tool_result(execution, result) -> None:
            agent = execution.agent
            if agent is None:
                return
            weight = self.weights["success"] if not result.is_error \
                else self.weights["failure"]
            engine.deposit(position_of(agent.id), weight=weight)
        self._disposers.append(ctx.on("tools/result", on_tool_result))

        # 奖励连续化：message-feedback → 沉积/淤积
        def on_feedback(payload: Dict[str, Any]) -> None:
            self._apply_feedback(payload.get("session_id"), engine,
                                 position_of)
        self._disposers.append(ctx.on("message-feedback/updated",
                                      on_feedback))

        # ④ 驻留/侵蚀 + 坐标平滑
        async def on_turn_stopping(payload: Dict[str, Any]) -> None:
            agent = payload.get("agent")
            if agent is None:
                return
            pos = position_of(agent.id)
            # coordinator 平滑：状态摘要 → 坐标 → lerp
            summary = self._summarize(agent)
            if summary:
                coord = await engine.embed(summary)
                blended = tuple(pos[i] * (1 - self.coordinator_blend)
                                + coord[i] * self.coordinator_blend
                                for i in range(engine.dim))
                self._positions[agent.id] = blended
                engine.set_position(agent.id, blended)
                pos = blended
            report = engine.report_and_maybe_erode(pos)
            self._positions[agent.id] = engine.gradient_step(pos)
            engine.set_position(agent.id, self._positions[agent.id])
            if report["eroded"]:
                agent.steer(
                    "【wanter】检测到局部洼地，已降低周边地形开辟新下坡通道。"
                    "尝试与之前不同的方法继续探索。",
                    source={"kind": "plugin", "plugin": "wanter"})
        self._disposers.append(ctx.on("agent/turn-stopping", on_turn_stopping))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
            self._positions.clear()
            self._position_history.clear()
            self._feedback_seen.clear()
        return cleanup

    # ---- feedback 回填 ----

    def _apply_feedback(self, session_id: Optional[str], engine,
                        position_of) -> None:
        if not session_id or not self.ctx.has("messageFeedback"):
            return
        for record in self.ctx.messageFeedback.get(session_id):
            key = (session_id, record["seq"], record["kind"],
                   record.get("time"))
            if key in self._feedback_seen:
                continue
            self._feedback_seen.add(key)
            history = self._position_history.get(session_id, {})
            pos = history.get(record["seq"])
            if pos is None:
                continue
            weight = self.weights["feedback_up"] if record["kind"] == "up" \
                else self.weights["feedback_down"]
            engine.deposit(pos, weight=weight)

    # ---- 状态摘要 ----

    def _summarize(self, agent: Any) -> str:
        """最近用户 + 助手文本拼成状态摘要（coordinator 输入）。"""
        messages = agent.session.derive_messages()
        parts = [m.plain_text().strip() for m in messages[-4:]
                 if m.plain_text().strip()]
        return " ".join(parts)[:300]

    # ---- 渲染 ----

    def _render_state(self, ac: Optional[Dict[str, Any]], engine,
                      position_of) -> str:
        """渲染本会话水滴状态。"""
        scope = (ac or {}).get("scope")
        if not scope or not scope.startswith("agent:"):
            return ""
        session_id = scope[len("agent:"):]
        pos = position_of(session_id)
        energy = engine.phi(pos)
        goal_note = ""
        nearest = engine.nearest_goal(pos)
        if nearest is not None:
            distance = sum((p - g) ** 2 for p, g in zip(pos, nearest)) ** 0.5
            goal_note = f"，距最近目标 {distance:.2f}"
        goals_note = f"，目标数 {len(engine.terrain.goals)}"
        return (f"[wanter 地形状态] 当前位置 {tuple(round(p, 2) for p in pos)}"
                f"，势能 {energy:.3f}{goal_note}{goals_note}。"
                f"水迹越浓的路径越值得复用；淤积抬高的路径应避开；"
                f"若感觉陷入停滞，可主动尝试新方法。")


def build_wanter_goal_tools() -> List[Any]:
    """构造 wanter_goal_* 工具族（子任务分解）。"""

    def _engine_of(run_ctx):
        agent = run_ctx.execution.agent
        ctx = agent.ctx if agent is not None else run_ctx.root_ctx
        if not ctx.has("wanter"):
            from ..errors import ToolError
            raise ToolError("wanter engine not mounted", code="NO_WANTER")
        return ctx.wanter

    @define_tool(
        name="wanter_goal_add",
        description="新增一个子任务目标势阱（描述经 coordinator 映射坐标）。",
        parameters={"goal": {"type": "string", "required": True},
                    "strength": {"type": "number"}},
        output={"type": "string"})
    async def wanter_goal_add(args, run_ctx):
        engine = _engine_of(run_ctx)
        coord = await engine.embed(args["goal"])
        engine.add_goal(coord, float(args.get("strength") or 1.0))
        return f"目标已加入: {tuple(round(c, 2) for c in coord)}"

    @define_tool(
        name="wanter_goal_list",
        description="列出全部子任务目标势阱。",
        parameters={}, output={"type": "array", "items": {"type": "object"}})
    async def wanter_goal_list(args, run_ctx):
        engine = _engine_of(run_ctx)
        return [{"goal": [round(c, 2) for c in g], "strength": s}
                for g, s in engine.terrain.goals]

    @define_tool(
        name="wanter_goal_complete",
        description="移除当前最近的目标势阱（子任务完成）。",
        parameters={}, output={"type": "string"})
    async def wanter_goal_complete(args, run_ctx):
        engine = _engine_of(run_ctx)
        agent = run_ctx.execution.agent
        # 优先用插件维护的「当前水滴位置」（梯度步/coordinator 已移动后），
        # 无记录才回退会话哈希初始坐标。
        pos = engine.get_position(agent.id) if agent is not None else None
        if pos is None:
            pos = tuple(0.0 for _ in range(engine.dim))
            if agent is not None:
                import hashlib as _h
                digest = _h.sha256(agent.id.encode("utf-8")).digest()
                pos = tuple((digest[i] / 255.0) * 4.0 - 2.0
                            for i in range(min(engine.dim, len(digest))))
        nearest = engine.nearest_goal(pos)
        if nearest is None:
            return "无目标"
        engine.remove_goal(nearest)
        if agent is not None:
            agent.steer("【wanter】子任务完成，目标势阱已移除。继续下一目标。",
                        source={"kind": "plugin", "plugin": "wanter"})
        return f"已完成子任务: {tuple(round(c, 2) for c in nearest)}"

    return [wanter_goal_add, wanter_goal_list, wanter_goal_complete]


class ToolWanterGoalsPlugin(Service):
    """注册 wanter_goal_* 工具的插件。"""

    inject = ("tools", "wanter")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        for tool in build_wanter_goal_tools():
            self._disposers.append(ctx.tools.register(tool))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
