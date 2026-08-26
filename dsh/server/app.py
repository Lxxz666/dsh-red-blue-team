"""
dsh.server.app —— FastAPI Web 服务（对应 dsh-web-app）。

- SSE 广播中枢：订阅 session/event + agent/status，向各会话的订阅者推送；
- 审批通道：ask 决策/ask_user → SSE 推送问题 → 浏览器 POST 答案；
- REST：会话 CRUD、消息投递（斜杠命令直派发）、取消、事件回放。

启动: ``python run.py web``（默认 http://127.0.0.1:3080）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("dsh.server")

STATIC_DIR = Path(__file__).parent / "static"


# ---- SSE 广播中枢 ----

class SseHub:
    """每会话订阅者队列 + 全局事件监听。"""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._disposers = [
            ctx.on("session/event", self._on_session_event),
            ctx.on("agent/status", self._on_agent_status),
        ]

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """订阅一个会话的事件流，返回调用者持有的队列。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        lst = self._subscribers.get(session_id)
        if lst is not None:
            try:
                lst.remove(queue)
            except ValueError:
                pass

    def push(self, session_id: str, payload: Dict[str, Any]) -> None:
        """向某会话的全部订阅者推送（队列满则丢弃最旧）。"""
        for queue in list(self._subscribers.get(session_id, [])):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _on_session_event(self, session: Any, event: Any) -> None:
        self.push(session.id, {"kind": "event", "event": event.to_json()})

    def _on_agent_status(self, payload: Dict[str, Any]) -> None:
        agent = payload.get("agent")
        if agent is None:
            return
        self.push(agent.id, {"kind": "status", "status": payload["status"]})

    def close(self) -> None:
        for disposer in self._disposers:
            disposer()
        self._disposers.clear()
        self._subscribers.clear()


# ---- FastAPI 应用 ----

class MessageIn(BaseModel):
    content: str


class ApprovalAnswer(BaseModel):
    allow: bool


class QuestionAnswer(BaseModel):
    text: str


class FeedbackIn(BaseModel):
    seq: int
    kind: str


def build_app(ctx: Any) -> FastAPI:
    """从已 boot 的 Context 构建 FastAPI 应用。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        hub = getattr(app.state, "hub", None)
        if hub is not None:
            hub.close()

    app = FastAPI(title="dsh-python", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx
    app.state.hub = SseHub(ctx)
    app.state.approval_requests: Dict[str, asyncio.Future] = {}
    app.state.approval_seq = 0
    app.state.question_requests: Dict[str, asyncio.Future] = {}

    # 静态资源（css/js）
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- 文本问答通道（ask_user） ----
    if ctx.has("userQuestions"):
        async def _question_channel(question: str, detail: str) -> Optional[str]:
            app.state.approval_seq += 1
            qid = f"question-{app.state.approval_seq}"
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            app.state.question_requests[qid] = future
            for session in ctx.sessions.list():
                app.state.hub.push(session.id, {
                    "kind": "user-question", "qid": qid,
                    "question": question, "detail": detail})
            try:
                return await future
            finally:
                app.state.question_requests.pop(qid, None)

        ctx.userQuestions.set_channel(_question_channel)

    # ---- 审批通道 ----
    async def _approval_channel(question: str, detail: str) -> bool:
        app.state.approval_seq += 1
        qid = f"approval-{app.state.approval_seq}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        app.state.approval_requests[qid] = future
        # 广播问题（所有活跃会话；前端只显示当前会话的）
        for session in ctx.sessions.list():
            app.state.hub.push(session.id, {
                "kind": "approval", "qid": qid,
                "question": question, "detail": detail})
        try:
            return await future
        finally:
            app.state.approval_requests.pop(qid, None)

    ctx.approval.set_channel(_approval_channel)

    # ---- 页面 ----
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # ---- 会话 ----
    @app.get("/api/sessions")
    async def list_sessions() -> JSONResponse:
        sessions = []
        has_title = ctx.has("sessionTitle")
        for session in ctx.sessions.list():
            if has_title:
                title = ctx.sessionTitle.title_for(session)
            else:
                messages = session.derive_messages()
                title = messages[-1].plain_text()[:60] if messages else ""
            sessions.append({"id": session.id,
                             "created_at": session.header.created_at,
                             "preview": title})
        return JSONResponse(sessions)

    @app.post("/api/sessions")
    async def create_session() -> JSONResponse:
        agent = await ctx.agents.create()
        return JSONResponse({"id": agent.id})

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> JSONResponse:
        """真实删除会话：内存会话 + 运行中的 agent + 磁盘 JSONL 一并清理。"""
        session = ctx.sessions.get(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        # 1) 移除运行中的 agent（若在跑）
        if ctx.has("agents"):
            agent = ctx.agents.get(session_id)
            if agent is not None:
                try:
                    ctx.agents.remove(agent)
                except Exception:
                    log.exception("remove agent failed for %s", session_id)
        # 2) 移除内存会话（公告 session/disposed）
        ctx.sessions.remove(session)
        # 3) 删除磁盘 JSONL 持久化文件
        if ctx.has("sessionPersistence"):
            path = ctx.sessionPersistence._path(session_id)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                log.warning("failed to remove session file %s", path)
        return JSONResponse({"ok": True})

    @app.get("/api/sessions/{session_id}/events")
    async def get_events(session_id: str, since: int = 0) -> JSONResponse:
        session = ctx.sessions.get(session_id)
        if session is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        events = [e.to_json() for e in session.events if e.seq >= since]
        todos = session.todos()
        goal = ctx.goals.get(f"agent:{session_id}") if ctx.has("goals") else None
        return JSONResponse({
            "events": events,
            "todos": todos,
            "goal": goal.to_json() if goal else None,
            "surface": list(session.surface.nodes),
        })

    @app.post("/api/sessions/{session_id}/messages")
    async def post_message(session_id: str, body: MessageIn) -> JSONResponse:
        agent = ctx.agents.get(session_id)
        if agent is None:
            # 懒加载：恢复的会话首次发消息时按需 resume agent（续聊）。
            # 会话不在 store（不是恢复的、也非本进程创建）→ 404，不误建。
            session = ctx.sessions.get(session_id)
            if session is None:
                return JSONResponse({"error": "session not found"},
                                    status_code=404)
            # resume 会 store.enter 新 Session，先移除预置的 Session 避免冲突
            if ctx.has("sessionPersistence"):
                existing = ctx.sessions.get(session_id)
                if existing is not None:
                    ctx.sessions.remove(existing)
                try:
                    agent = await ctx.agents.resume(session_id)
                except Exception as exc:
                    return JSONResponse(
                        {"error": f"resume agent failed: {exc}"},
                        status_code=500)
            if agent is None:
                return JSONResponse({"error": "agent not live"},
                                    status_code=404)
        content = body.content.strip()
        if not content:
            return JSONResponse({"ok": True, "ignored": True})
        # 斜杠命令：不经模型 turn 直接派发
        if content.startswith("/") and ctx.has("commands"):
            result = ctx.commands.dispatch(agent, content)
            return JSONResponse({"ok": True, "command": result})
        agent.followup(content)
        return JSONResponse({"ok": True, "enqueued": True})

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> JSONResponse:
        agent = ctx.agents.get(session_id)
        if agent is None:
            return JSONResponse({"error": "agent not live"}, status_code=404)
        agent.cancel({"kind": "user"})
        return JSONResponse({"ok": True})

    # ---- 消息反馈 ----

    @app.post("/api/sessions/{session_id}/feedback")
    async def post_feedback(session_id: str,
                            body: FeedbackIn) -> JSONResponse:
        if not ctx.has("messageFeedback"):
            return JSONResponse({"error": "feedback unavailable"},
                                status_code=404)
        try:
            record = ctx.messageFeedback.put(session_id, body.seq, body.kind)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "record": record})

    @app.get("/api/sessions/{session_id}/feedback")
    async def get_feedback(session_id: str) -> JSONResponse:
        if not ctx.has("messageFeedback"):
            return JSONResponse({"feedback": []})
        return JSONResponse({"feedback": ctx.messageFeedback.get(session_id)})

    @app.post("/api/approval/{qid}")
    async def answer_approval(qid: str, body: ApprovalAnswer) -> JSONResponse:
        future = app.state.approval_requests.get(qid)
        if future is None or future.done():
            return JSONResponse({"error": "unknown approval"}, status_code=404)
        future.set_result(body.allow)
        return JSONResponse({"ok": True})

    @app.post("/api/questions/{qid}")
    async def answer_question(qid: str, body: QuestionAnswer) -> JSONResponse:
        future = app.state.question_requests.get(qid)
        if future is None or future.done():
            return JSONResponse({"error": "unknown question"}, status_code=404)
        future.set_result(body.text)
        return JSONResponse({"ok": True})

    @app.get("/api/providers")
    async def providers() -> JSONResponse:
        return JSONResponse({"providers": ctx.llm.providers()})

    # ---- 设置（Web 设置面板） ----
    @app.get("/api/settings", include_in_schema=False)
    async def get_settings() -> JSONResponse:
        data: Dict[str, Any] = {"settings": {}, "agent_default_model": {},
                                "providers": ctx.llm.providers()}
        if ctx.has("settings"):
            data["settings"] = ctx.settings.all()
        if ctx.has("agentDefaultModel"):
            data["agent_default_model"] = \
                ctx.agentDefaultModel.current_selection()
        return JSONResponse(data)

    @app.put("/api/settings", include_in_schema=False)
    async def put_settings(request: Request) -> JSONResponse:
        if not ctx.has("settings"):
            return JSONResponse({"error": "settings unavailable"},
                                status_code=404)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "expected object"}, status_code=400)
        for key, value in body.items():
            ctx.settings.set(str(key), value)
        return JSONResponse({"ok": True, "settings": ctx.settings.all()})

    # ---- wanter 地形面板 ----
    @app.get("/api/wanter/terrain", include_in_schema=False)
    async def wanter_terrain() -> Response:
        if not ctx.has("wanter"):
            raise HTTPException(status_code=404,
                                detail="wanter engine not mounted")
        from dsh.wanter.viz import render_terrain_svg
        svg = render_terrain_svg(ctx.wanter)
        return Response(content=svg, media_type="image/svg+xml")

    @app.get("/api/wanter/state", include_in_schema=False)
    async def wanter_state() -> JSONResponse:
        if not ctx.has("wanter"):
            raise HTTPException(status_code=404,
                                detail="wanter engine not mounted")
        from dsh.wanter.viz import render_terrain_state
        return JSONResponse(render_terrain_state(ctx.wanter))

    # ---- 会话节点树（Chat 节点协议：fork 谱系） ----
    @app.get("/api/sessions/tree", include_in_schema=False)
    async def sessions_tree() -> JSONResponse:
        nodes: Dict[str, Dict[str, Any]] = {}

        def ensure(session_id: str) -> Dict[str, Any]:
            return nodes.setdefault(session_id, {
                "id": session_id, "parent": None, "children": [],
                "live": False, "origin": None, "created_at": 0})

        for session in ctx.sessions.list():
            header = session.header
            node = ensure(session.id)
            node.update({"live": True, "parent": header.parent_session,
                         "origin": header.origin,
                         "created_at": header.created_at})
        # 持久化（冷）补齐：header 摘要（不加载日志）
        if ctx.has("sessionQuery"):
            try:
                for row in await ctx.sessionQuery.list(limit=500):
                    node = ensure(row["id"])
                    if not node["live"]:
                        node.update({
                            "parent": row["parent_session"],
                            "origin": row["origin"],
                            "created_at": row["created_at"]})
            except Exception:
                log.exception("session tree: cold enumeration failed")
        for session_id, node in nodes.items():
            parent = node.get("parent")
            if parent and parent in nodes:
                nodes[parent]["children"].append(session_id)
        for node in nodes.values():
            node["children"].sort(
                key=lambda c: nodes[c].get("created_at") or 0)
        roots = [sid for sid, node in nodes.items()
                 if not node.get("parent") or node["parent"] not in nodes]
        roots.sort(key=lambda sid: nodes[sid].get("created_at") or 0)
        return JSONResponse({"roots": roots, "nodes": nodes})

    # ---- SSE ----
    @app.get("/api/sessions/{session_id}/stream")
    async def stream(session_id: str, request: Request) -> StreamingResponse:
        hub: SseHub = app.state.hub
        queue = hub.subscribe(session_id)

        async def gen():
            try:
                yield "event: hello\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                        yield (f"event: {payload['kind']}\n"
                               f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                hub.unsubscribe(session_id, queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


async def _restore_sessions(ctx) -> None:
    """启动时从磁盘恢复历史会话（只恢复 Session 元数据 + 事件 seed，
    不启动 agent driver —— 避免恢复的 agent 自动跑 turn 造成循环）。
    发消息时按需 resume（见 post_message）。"""
    from ..session.events import SessionEvent
    if not ctx.has("sessionPersistence"):
        return
    pers = ctx.sessionPersistence
    store = ctx.sessions
    try:
        ids = await pers.list_ids()
    except Exception:
        log.exception("list persisted sessions failed")
        return
    restored = 0
    for sid in sorted(ids):
        try:
            header, rows = await pers.load(sid)
            if header is None or header.id != sid:
                continue
            events = [SessionEvent.from_json(r) for r in rows]
            store.create(
                session_id=sid,
                meta={"created_at": header.created_at, "cwd": header.cwd,
                      "parent_session": header.parent_session,
                      "seed_length": header.seed_length,
                      "origin": header.origin,
                      "delegation_depth": header.delegation_depth,
                      "agent_preset": header.agent_preset},
                seed=events)
            restored += 1
        except Exception:
            log.warning("skip restore session %s", sid, exc_info=True)
    log.info("restored %d sessions (agents lazy-resumed on first message)",
             restored)


async def run_server(profile: str = "web", workspace: Optional[str] = None,
                     mock: bool = False, provider: Optional[str] = None,
                     model: Optional[str] = None, host: str = "127.0.0.1",
                     port: int = 3080) -> None:
    """boot → 构建应用 → uvicorn 运行。"""
    import uvicorn

    from ..boot import boot
    from .. import setup_logging
    setup_logging()
    ctx, tree = await boot(profile=profile, workspace=workspace,
                           mock_llm=mock, provider=provider, model=model)
    await _restore_sessions(ctx)  # 从磁盘恢复历史会话（跨重启）
    app = build_app(ctx)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    await tree.dispose()
