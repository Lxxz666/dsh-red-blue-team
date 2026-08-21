/* dsh-python Web UI：会话列表 + 流式聊天 + 工具卡片 + 审批弹窗 */
"use strict";

const state = {
  sessionId: null,
  eventSource: null,
  rendered: new Map(),      // seq -> element（已渲染事件）
  pendingTools: new Map(),  // callId -> 卡片元素
  statusEl: null,
  messagesEl: null,
};

const $ = (sel) => document.querySelector(sel);

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`);
  return resp.json();
}

/* ---- 微 markdown 渲染（转义 + code/pre/bold） ---- */
function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function renderMarkdown(text) {
  const codeBlocks = [];
  let out = escapeHtml(text).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    codeBlocks.push(`<pre><code>${code.trim()}</code></pre>`);
    return `\u0000BLOCK${codeBlocks.length - 1}\u0000`;
  });
  out = out
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\u0000BLOCK(\d+)\u0000/g, (_, i) => codeBlocks[+i]);
  return out;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ---- 消息渲染 ---- */
function renderUserMessage(event) {
  const wrap = el("div", "msg user");
  wrap.appendChild(el("div", "role-tag", "用户"));
  const bubble = el("div", "bubble");
  bubble.innerHTML = renderMarkdown(String(event.data.content ?? ""));
  wrap.appendChild(bubble);
  return wrap;
}

function renderAssistantChunk(event) {
  // 增量：流式文本直接追加到「当前 assistant」气泡
  const current = state.messagesEl.lastElementChild;
  const chunk = event.data.chunk || {};
  if (chunk.type === "text-delta" && current && current.classList.contains("msg")
      && current.dataset.streaming === "1") {
    const bubble = current.querySelector(".bubble");
    bubble.innerHTML = renderMarkdown(bubble.textContent + (chunk.text || ""));
    state.messagesEl.scrollTop = state.messagesEl.scrollHeight;
  }
  return null;
}

function renderAssistantMessage(event) {
  const wrap = el("div", "msg assistant");
  wrap.appendChild(el("div", "role-tag", "助手"));
  const bubble = el("div", "bubble");
  const blocks = event.data.blocks || [];
  let text = blocks.filter((b) => b.kind === "text").map((b) => b.text).join("");
  bubble.innerHTML = renderMarkdown(text);
  wrap.appendChild(bubble);
  // 消息反馈（点赞/点踩）
  if (text) {
    const feedback = el("div", "feedback");
    const up = el("button", "fb-btn", "👍");
    const down = el("button", "fb-btn", "👎");
    up.onclick = () => sendFeedback(event.seq, "up");
    down.onclick = () => sendFeedback(event.seq, "down");
    feedback.appendChild(up);
    feedback.appendChild(down);
    wrap.appendChild(feedback);
  }
  return wrap;
}

async function sendFeedback(seq, kind) {
  if (!state.sessionId) return;
  try {
    await api(`/api/sessions/${state.sessionId}/feedback`, {
      method: "POST",
      body: JSON.stringify({ seq, kind }),
    });
  } catch (err) {
    console.warn("feedback failed", err);
  }
}

function renderToolCall(event) {
  const card = el("div", "tool-card");
  card.innerHTML = `<div class="tool-head"><span class="name">${escapeHtml(event.data.name)}</span><span>执行中…</span></div>`;
  state.pendingTools.set(event.data.call_id, card);
  return card;
}

function renderToolResult(event) {
  const card = state.pendingTools.get(event.data.call_id);
  if (!card) return null;
  const body = el("div", "tool-body");
  body.textContent = String(event.data.content ?? "");
  if (event.data.is_error) {
    card.classList.add("error");
    card.querySelector(".tool-head").innerHTML =
      `<span class="name">${escapeHtml(event.data.name)}</span><span>失败</span>`;
  } else {
    card.querySelector(".tool-head").innerHTML =
      `<span class="name">${escapeHtml(event.data.name)}</span><span>完成</span>`;
  }
  card.appendChild(body);
  state.pendingTools.delete(event.data.call_id);
  return card;
}

function renderEvent(event) {
  const seq = event.seq;
  if (state.rendered.has(seq)) return;
  let node = null;
  switch (event.type) {
    case "user/message": node = renderUserMessage(event); break;
    case "assistant/chunk": node = renderAssistantChunk(event); break;
    case "assistant/message": node = renderAssistantMessage(event); break;
    case "tool/call": node = renderToolCall(event); break;
    case "tool/result": node = renderToolResult(event); break;
    case "compaction/summary":
      node = el("div", "msg user");
      node.appendChild(el("div", "role-tag", "上下文压缩"));
      const b = el("div", "bubble");
      b.textContent = event.data.summary ?? "";
      node.appendChild(b);
      break;
  }
  if (node) {
    state.messagesEl.appendChild(node);
    state.rendered.set(seq, node);
    state.messagesEl.scrollTop = state.messagesEl.scrollHeight;
  }
}

function beginStreaming() {
  const wrap = el("div", "msg assistant");
  wrap.dataset.streaming = "1";
  wrap.appendChild(el("div", "role-tag", "助手"));
  wrap.appendChild(el("div", "bubble"));
  state.messagesEl.appendChild(wrap);
}

/* ---- 会话管理 ---- */
async function loadSessions() {
  const list = await api("/api/sessions");
  const container = $("#session-list");
  container.innerHTML = "";
  for (const session of list) {
    const item = el("div", "session-item" + (session.id === state.sessionId ? " active" : ""));
    item.textContent = session.preview || session.id.slice(0, 16);
    item.onclick = () => selectSession(session.id);
    container.appendChild(item);
  }
}

async function createSession() {
  const data = await api("/api/sessions", { method: "POST" });
  await selectSession(data.id);
}

async function selectSession(sessionId) {
  state.sessionId = sessionId;
  state.rendered.clear();
  state.pendingTools.clear();
  state.messagesEl.innerHTML = "";
  $("#session-title").textContent = sessionId;
  await loadSessions();
  await replay();
  connectStream();
}

async function replay() {
  const data = await api(`/api/sessions/${state.sessionId}/events`);
  for (const event of data.events) renderEvent(event);
}

function connectStream() {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = new EventSource(`/api/sessions/${state.sessionId}/stream`);
  state.eventSource.addEventListener("event", (e) => {
    const payload = JSON.parse(e.data);
    renderEvent(payload.event);
  });
  state.eventSource.addEventListener("status", (e) => {
    const payload = JSON.parse(e.data);
    setStatus(payload.status);
  });
  state.eventSource.addEventListener("approval", (e) => {
    showApproval(JSON.parse(e.data));
  });
  state.eventSource.addEventListener("user-question", (e) => {
    showQuestion(JSON.parse(e.data));
  });
  state.eventSource.onerror = () => { /* 自动重连 */ };
}

function setStatus(status) {
  state.statusEl.textContent = status;
  state.statusEl.className = "status " + (status === "running" ? "running" : "idle");
  $("#btn-cancel").classList.toggle("visible", status === "running");
}

/* ---- 发送 ---- */
async function sendMessage() {
  const input = $("#input");
  const content = input.value.trim();
  if (!content || !state.sessionId) return;
  input.value = "";
  // 本地回显用户消息
  const wrap = el("div", "msg user");
  wrap.appendChild(el("div", "role-tag", "用户"));
  const bubble = el("div", "bubble");
  bubble.innerHTML = renderMarkdown(content);
  wrap.appendChild(bubble);
  state.messagesEl.appendChild(wrap);
  state.messagesEl.scrollTop = state.messagesEl.scrollHeight;

  beginStreaming();
  const data = await api(`/api/sessions/${state.sessionId}/messages`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
  if (data && data.command && data.command.reply) {
    const reply = el("div", "msg assistant");
    reply.appendChild(el("div", "role-tag", "命令"));
    const rb = el("div", "bubble");
    rb.innerHTML = renderMarkdown(data.command.reply);
    reply.appendChild(rb);
    state.messagesEl.appendChild(reply);
    state.messagesEl.lastElementChild.remove(); // 移除多余流式占位
  }
}

/* ---- 审批/问答弹窗 ---- */
function showApproval(payload) {
  $("#modal-question").textContent = payload.question;
  $("#modal-detail").textContent = payload.detail || "";
  $("#modal").classList.remove("hidden");
  const answer = async (allow) => {
    $("#modal").classList.add("hidden");
    await api(`/api/approval/${payload.qid}`, {
      method: "POST",
      body: JSON.stringify({ allow }),
    });
  };
  $("#modal-allow").onclick = () => answer(true);
  $("#modal-deny").onclick = () => answer(false);
}

function showQuestion(payload) {
  $("#modal-question").textContent = payload.question;
  $("#modal-detail").textContent = payload.detail || "";
  const input = $("#modal-input");
  input.value = "";
  input.classList.remove("hidden");
  $("#modal-deny").style.display = "none";
  $("#modal-allow").textContent = "提交回答";
  $("#modal").classList.remove("hidden");
  const submit = async () => {
    $("#modal").classList.add("hidden");
    $("#modal-deny").style.display = "";
    $("#modal-allow").textContent = "允许";
    input.classList.add("hidden");
    await api(`/api/questions/${payload.qid}`, {
      method: "POST",
      body: JSON.stringify({ text: input.value || "(无回答)" }),
    });
  };
  $("#modal-allow").onclick = submit;
  $("#modal-deny").onclick = submit;
}

/* ---- 初始化 ---- */
async function init() {
  state.statusEl = $("#status");
  state.messagesEl = $("#messages");
  $("#btn-new").onclick = createSession;
  $("#btn-send").onclick = sendMessage;
  $("#btn-cancel").onclick = async () => {
    if (state.sessionId) await api(`/api/sessions/${state.sessionId}/cancel`, { method: "POST" });
  };
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  try {
    const data = await api("/api/providers");
    $("#providers").textContent = "providers: " + data.providers.join(", ");
  } catch { /* ignore */ }
  const sessions = await api("/api/sessions");
  if (sessions.length === 0) await createSession();
  else await selectSession(sessions[sessions.length - 1].id);
}

init();
