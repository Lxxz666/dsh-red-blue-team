/* ============================================================
   dsh-python Web UI 前端（Hermes 级标准）
   - 完整 markdown 渲染（标题/列表/表格/引用/代码块+复制/粗斜体/链接/hr）
   - 流式打字 + 光标、工具卡片折叠、消息操作、空状态、会话管理
   - SSE 事件流、审批/问答弹窗、移动端抽屉
   ============================================================ */
"use strict";

const state = {
  sessionId: null,
  eventSource: null,
  rendered: new Map(),      // seq -> element
  pendingTools: new Map(),  // call_id -> card element
  streamingMsg: null,       // 当前流式 assistant 消息元素
  statusEl: null,
  messagesEl: null,
  emptyEl: null,
  lastSeq: 0,
  echoedContent: null,      // 本地回显的用户消息（SSE 事件到达时去重）
};

const $ = (sel) => document.querySelector(sel);

/* ---------------- API ---------------- */
async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    // 把后端 JSON 响应体里的具体错误带出来（如 "agent not live"），
    // 让 toast 显示真实原因而不是只有状态码
    let detail = "";
    try { const e = await resp.json(); detail = e.error || e.detail || ""; } catch (_) {}
    throw new Error(detail || `${path}: ${resp.status}`);
  }
  return resp.json();
}

/* ---------------- 工具函数 ---------------- */
function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;")
             .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() - ts * 1000;
  if (diff < 60e3) return "刚刚";
  if (diff < 3600e3) return `${Math.floor(diff / 60e3)} 分钟前`;
  if (diff < 86400e3) return `${Math.floor(diff / 3600e3)} 小时前`;
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}-${d.getDate()}`;
}
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 1800);
}
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("已复制");
  }
}

/* ============================================================
   Markdown 渲染（自研，零依赖、离线可用）
   ============================================================ */
function renderMarkdown(text) {
  const codeBlocks = [];
  // 1) 先抽出代码块（含语言标注）
  let out = escapeHtml(text);
  out = out.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang, code });
    return `\u0000CB${idx}\u0000`;
  });

  // 2) 行内元素
  out = out
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,
             '<img src="$2" alt="$1" loading="lazy">');

  // 3) 块级元素
  const lines = out.split("\n");
  const html = [];
  let list = null, quote = null, table = null;

  function flushList() {
    if (list) { html.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`); list = null; }
  }
  function flushQuote() {
    if (quote) { html.push(`<blockquote>${quote.join("")}</blockquote>`); quote = null; }
  }
  function flushTable() {
    if (table) { html.push(`<table>${table.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</table>`); table = null; }
  }

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");

    // 表格行
    if (/^\|.*\|$/.test(line)) {
      flushList(); flushQuote();
      if (/^\|[\s:|-]+\|$/.test(line)) {
        // 分隔行：仅跳过，不 flush（表头/数据行同属一个 table）
        continue;
      }
      const cells = line.slice(1, -1).split("|").map(c => c.trim());
      if (!table) table = [];
      table.push(cells);
      continue;
    }
    flushTable();

    // 标题
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { flushList(); flushQuote(); html.push(`<h${h[1].length}>${h[2]}</h${h[1].length}>`); continue; }

    // 引用（文本已转义，> 变为 &gt;）
    if (/^&gt;\s?/.test(line)) {
      flushList();
      const q = line.replace(/^&gt;\s?/, "");
      if (!quote) quote = [];
      quote.push(`<p>${q}</p>`);
      continue;
    }
    flushQuote();

    // 无序列表
    const ul = line.match(/^[-*+]\s+(.*)$/);
    if (ul) {
      if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
      list.items.push(`<li>${ul[1]}</li>`);
      continue;
    }
    // 有序列表
    const ol = line.match(/^\d+\.\s+(.*)$/);
    if (ol) {
      if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
      list.items.push(`<li>${ol[1]}</li>`);
      continue;
    }
    flushList();

    // 分割线
    if (/^([-*_]\s*){3,}$/.test(line)) { html.push("<hr>"); continue; }

    // 空行
    if (!line) { html.push(""); continue; }

    // 代码块占位（原样保留，统一在最终阶段渲染成代码卡片：语言徽章+行号+复制）
    if (/^\u0000CB(\d+)\u0000$/.test(line)) { html.push(line); continue; }

    html.push(`<p>${line}</p>`);
  }
  flushList(); flushQuote(); flushTable();

  let result = html.join("\n");
  // 恢复代码块：语言徽章 + 行号 + 一键复制
  result = result.replace(/\u0000CB(\d+)\u0000/g, (_, i) => {
    const b = codeBlocks[+i];
    const lang = b.lang || "text";
    const lines = b.code.split("\n");
    const nums = lines.map((_, n) => `<span class="ln">${n + 1}</span>`).join("");
    const body = lines.map((l) => escapeHtml(l)).join("\n");
    return `<div class="code-card" data-idx="${+i}">
      <div class="code-head">
        <span class="lang-tag">${escapeHtml(lang)}</span>
        <button class="copy-btn" data-idx="${+i}" title="复制代码">⧉ 复制</button>
      </div>
      <div class="code-body">
        <div class="line-nums">${nums}</div>
        <pre><code class="lang-${lang}">${body}</code></pre>
      </div>
    </div>`;
  });
  return result;
}

/* 代码块复制（事件委托：动态渲染的代码块也生效）+ 按钮动效 */
function copyCodeToClipboard(text) {
  // 优先 Clipboard API，失败降级 textarea + execCommand（headless/受限环境也成功）
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text)
      .catch(() => legacyCopy(text));
  }
  return Promise.resolve(legacyCopy(text));
}
function legacyCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  if (!ok) throw new Error("copy failed");
}
function initCodeCopy() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    const card = btn.closest(".code-card");
    if (!card) return;
    const code = card.querySelector("code").innerText;
    copyCodeToClipboard(code)
      .then(() => {
        btn.textContent = "✓ 已复制";
        btn.classList.add("copied");
        setTimeout(() => { btn.textContent = "⧉ 复制"; btn.classList.remove("copied"); }, 1500);
      })
      .catch(() => { btn.textContent = "复制失败"; });
  });
}

/* ---------------- 消息渲染 ---------------- */
function renderUserMessage(event) {
  const content = String(event.data.content ?? "");
  // 去重：发送时本地回显过了，SSE 的 user/message 事件跳过（避免重复文本）
  if (state.echoedContent === content) {
    state.echoedContent = null;
    return null;
  }
  const wrap = el("div", "msg user");
  const tag = el("div", "role-tag");
  tag.appendChild(el("span", "avatar", "你"));
  tag.appendChild(document.createTextNode("你"));
  wrap.appendChild(tag);
  const bubble = el("div", "bubble md");
  bubble.innerHTML = renderMarkdown(content);
  wrap.appendChild(bubble);
  const actions = el("div", "msg-actions");
  const copy = el("button", "act-btn", "复制");
  copy.onclick = () => copyText(content);
  actions.appendChild(copy);
  wrap.appendChild(actions);
  return wrap;
}

function renderAssistantChunk(event) {
  const chunk = event.data.chunk || {};
  if (chunk.type === "text-delta" && state.streamingMsg) {
    const bubble = state.streamingMsg.querySelector(".bubble");
    const cur = bubble.dataset.md || "";
    bubble.dataset.md = cur + (chunk.text || "");
    trackSpeed((chunk.text || "").length);
    bubble.innerHTML = renderMarkdown(bubble.dataset.md) + '<span class="cursor"></span>';
    state.messagesEl.scrollTop = state.messagesEl.scrollHeight;
  }
  return null;
}

/* 实时生成速度统计：字符增量 → tok/s（中文 ≈ 1.5 字符/token） */
function trackSpeed(chars) {
  const now = Date.now();
  if (!state._spd) state._spd = { start: now, chars: 0, last: now, lastChars: 0 };
  const s = state._spd;
  s.chars += chars;
  if (now - s.last >= 450) {
    const cps = (s.chars - s.lastChars) / ((now - s.last) / 1000);
    const tokps = Math.max(0, cps / 1.5);
    s.last = now; s.lastChars = s.chars;
    const el = $("#speed-label");
    el.textContent = "⚡ " + tokps.toFixed(1) + " tok/s";
    el.classList.remove("hidden");
  }
}

function renderAssistantMessage(event) {
  // 移除流式占位气泡，用完整消息替换（避免双气泡）
  if (state.streamingMsg && state.streamingMsg.isConnected) {
    state.streamingMsg.remove();
    state.streamingMsg = null;
  }
  const wrap = el("div", "msg assistant");
  const tag = el("div", "role-tag");
  tag.appendChild(el("span", "avatar", "d"));
  tag.appendChild(document.createTextNode("助手"));
  if (event.data.model) {
    tag.appendChild(el("span", "model-chip-mini", event.data.model));
  }
  wrap.appendChild(tag);
  const bubble = el("div", "bubble md");
  const blocks = event.data.blocks || [];
  // 可见文本与思考过程分离：思考过程 → 可折叠面板（默认收起，不占正文）
  const text = blocks.filter((b) => b.kind === "text").map((b) => b.text).join("");
  const reasoning = blocks.filter((b) => b.kind === "reasoning")
                          .map((b) => b.text).join("");
  bubble.dataset.md = text;
  bubble.innerHTML = renderMarkdown(text);
  wrap.appendChild(bubble);

  // 思考过程折叠面板
  if (reasoning) {
    const rp = el("div", "reasoning collapsed");
    const head = el("div", "reasoning-head");
    head.appendChild(el("span", "reasoning-icon", "🧠"));
    head.appendChild(el("span", "reasoning-title", "思考过程"));
    head.appendChild(el("span", "reasoning-chev", "▶"));
    const body = el("div", "reasoning-body md");
    body.style.display = "none";
    body.innerHTML = renderMarkdown(reasoning);
    head.onclick = () => {
      const collapsed = rp.classList.toggle("collapsed");
      body.style.display = collapsed ? "none" : "block";
      head.querySelector(".reasoning-chev").textContent = collapsed ? "▶" : "▼";
    };
    rp.appendChild(head);
    rp.appendChild(body);
    wrap.appendChild(rp);
  }

  // 操作条：复制
  if (text) {
    const actions = el("div", "msg-actions");
    const copy = el("button", "act-btn", "复制");
    copy.onclick = () => copyText(text);
    actions.appendChild(copy);
    wrap.appendChild(actions);
  }
  // 反馈
  if (text) {
    const feedback = el("div", "feedback");
    const up = el("button", "fb-btn", "👍");
    const down = el("button", "fb-btn", "👎");
    up.onclick = () => sendFeedback(event.seq, "up", up, down);
    down.onclick = () => sendFeedback(event.seq, "down", down, up);
    feedback.appendChild(up); feedback.appendChild(down);
    wrap.appendChild(feedback);
  }
  state.streamingMsg = null;
  return wrap;
}

async function sendFeedback(seq, kind, btn, other) {
  if (!state.sessionId) return;
  try {
    await api(`/api/sessions/${state.sessionId}/feedback`, {
      method: "POST", body: JSON.stringify({ seq, kind }),
    });
    btn.classList.add("active");
    if (other) other.classList.remove("active");
    toast(kind === "up" ? "已记录好评" : "已记录差评");
  } catch { /* ignore */ }
}

/* ---- 工具卡片 ---- */
function renderToolCall(event) {
  const card = el("div", "tool-card open tilt");
  const args = String(event.data.arguments ?? "{}");
  card.innerHTML = `
    <div class="tool-head">
      <span class="tool-icon">⚙️</span>
      <span class="name">${escapeHtml(event.data.name || "tool")}</span>
      <span class="tool-args">${escapeHtml(args.slice(0, 90))}</span>
      <span class="state"><span class="badge running"></span><span class="state-text">执行中</span></span>
      <span class="chev">▶</span>
    </div>`;
  card.querySelector(".tool-head").onclick = () => {
    card.classList.toggle("open");
    const body = card.querySelector(".tool-body");
    if (body) body.style.display = card.classList.contains("open") ? "" : "none";
  };
  state.pendingTools.set(event.data.call_id, card);
  return card;
}

function renderToolResult(event) {
  const card = state.pendingTools.get(event.data.call_id);
  if (!card) return null;
  const body = el("div", "tool-body");
  const content = String(event.data.content ?? "");
  body.textContent = content.slice(0, 4000) + (content.length > 4000 ? "\n…(截断)" : "");
  if (event.data.is_error) {
    card.classList.add("error");
    const err = event.data.error ? ` · ${event.data.error.code || ""}` : "";
    card.querySelector(".badge").className = "badge error";
    card.querySelector(".state-text").textContent = "失败" + err;
  } else {
    card.querySelector(".badge").className = "badge done";
    card.querySelector(".state-text").textContent = "完成";
  }
  card.appendChild(body);
  state.pendingTools.delete(event.data.call_id);
  return card;
}

/* ---- 事件标签 ---- */
function renderEventTag(text, extraCls = "") {
  const node = el("div", "event-tag " + extraCls);
  node.appendChild(document.createTextNode(text));
  return node;
}

function renderEvent(event) {
  const seq = event.seq;
  if (state.rendered.has(seq)) return;
  if (seq <= state.lastSeq && state.lastSeq > 0) {
    // 回放阶段：只渲染完整消息（assistant/chunk 跳过）
  }
  let node = null;
  switch (event.type) {
    case "user/message":
      node = renderUserMessage(event); break;
    case "assistant/chunk":
      node = renderAssistantChunk(event); break;
    case "assistant/message":
      node = renderAssistantMessage(event); break;
    case "tool/call":
      node = renderToolCall(event); break;
    case "tool/result":
      node = renderToolResult(event); break;
    case "compaction/summary":
      node = renderEventTag("上下文已压缩： " + (event.data.summary || "").slice(0, 60) + "…", "compaction");
      break;
    case "turn/start":
      node = null; break;
    case "turn/end":
      if (event.data.reason && event.data.reason.kind === "error") {
        node = renderEventTag("任务出错：" + ((event.data.reason.error || {}).message || "").slice(0, 80), "error");
      }
      break;
  }
  if (node) {
    state.messagesEl.appendChild(node);
    state.rendered.set(seq, node);
    state.messagesEl.scrollTop = state.messagesEl.scrollHeight;
  } else {
    state.rendered.set(seq, null);
  }
}

function beginStreaming() {
  // 若已有流式消息则复用；否则新建
  if (state.streamingMsg && state.streamingMsg.isConnected) return;
  const wrap = el("div", "msg assistant");
  const tag = el("div", "role-tag");
  tag.appendChild(el("span", "avatar", "d"));
  tag.appendChild(document.createTextNode("助手"));
  wrap.appendChild(tag);
  const bubble = el("div", "bubble md");
  bubble.innerHTML = '<span class="cursor"></span>';
  wrap.appendChild(bubble);
  wrap.classList.add("streaming");
  state.streamingMsg = wrap;
  state.messagesEl.appendChild(wrap);
  state.messagesEl.scrollTop = state.messagesEl.scrollHeight;
}

/* ---------------- 会话管理 ---------------- */
async function loadSessions(selectId) {
  const list = await api("/api/sessions");
  const container = $("#session-list");
  container.innerHTML = "";
  $("#session-count").textContent = list.length;
  for (const session of list) {
    const item = el("div", "pop-card" + (session.id === state.sessionId ? " active" : ""));
    const icon = el("div", "pc-icon", "💬");
    const body = el("div", "pc-body");
    body.appendChild(el("div", "pc-preview", session.preview || "（新会话）"));
    const meta = el("div", "pc-meta");
    meta.appendChild(el("span", "pc-time", fmtTime(session.created_at)));
    if (session.id === state.sessionId) meta.appendChild(el("span", "pc-live", "● 活跃中"));
    body.appendChild(meta);
    const del = el("button", "pc-del", "✕");
    del.onclick = (e) => { e.stopPropagation(); deleteSession(session.id); };
    item.appendChild(icon); item.appendChild(body); item.appendChild(del);
    item.onclick = () => selectSession(session.id);
    container.appendChild(item);
  }
}

/* 时间格式化：ISO → MM-DD HH:mm */
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function deleteSession(sessionId) {
  try {
    await api(`/api/sessions/${sessionId}`, { method: "DELETE" });
  } catch (e) {
    toast("删除失败，请重试");
    return;
  }
  // 如果删的是当前会话：新建会话承接（selectSession 会自动重建 SSE）
  if (sessionId === state.sessionId) {
    await createSession();
  } else {
    loadSessions();
  }
  toast("会话已删除");
}

async function createSession() {
  const data = await api("/api/sessions", { method: "POST" });
  await selectSession(data.id);
}

async function selectSession(sessionId) {
  state.sessionId = sessionId;
  state.rendered.clear();
  state.pendingTools.clear();
  state.streamingMsg = null;
  state.lastSeq = 0;
  state.messagesEl.innerHTML = "";
  // 会话切换：消息区从右侧滑入淡入（Apple/Rive 式过渡）
  state.messagesEl.classList.remove("swapped");
  void state.messagesEl.offsetWidth; // 重置动画
  state.messagesEl.classList.add("swapped");
  $("#empty").classList.add("hidden");
  state.messagesEl.classList.remove("hidden");
  $("#session-title").textContent = sessionId;
  closeSessionPop();
  await loadSessions();
  await replay();
  connectStream();
}

async function replay() {
  const data = await api(`/api/sessions/${state.sessionId}/events`);
  const events = data.events || [];
  // 记录回放前最后一个完整消息 seq（用于增量流式判断）
  for (const event of events) {
    if (event.type === "assistant/message") state.lastSeq = event.seq;
    if (event.type !== "assistant/chunk") renderEvent(event);
  }
}

function connectStream() {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = new EventSource(`/api/sessions/${state.sessionId}/stream`);
  state.eventSource.addEventListener("event", (e) => {
    const payload = JSON.parse(e.data);
    const event = payload.event;
    if (event.type === "assistant/chunk") {
      beginStreaming();
      renderEvent(event);
    } else {
      renderEvent(event);
    }
  });
  state.eventSource.addEventListener("status", (e) => {
    setStatus(JSON.parse(e.data).status);
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
  state.statusEl.classList.toggle("running", status === "running");
  $(".tb-brand").classList.toggle("thinking", status === "running");  // AI 思考星轨
  $("#btn-send").classList.toggle("sending", status === "running");   // 发送按钮 loading
  $("#pet-whale").classList.toggle("excited", status === "running");  // 小鲸鱼兴奋加速
  $("#status-text").textContent = status;
  $("#btn-cancel").classList.toggle("visible", status === "running");
  if (status === "running") beginStreaming();
  else {
    // 结束：隐藏速度标签 + 去掉流式呼吸高亮
    state._spd = null;
    $("#speed-label").classList.add("hidden");
    document.querySelectorAll(".msg.streaming").forEach((m) => m.classList.remove("streaming"));
  }
}

/* ---------------- 发送 ---------------- */
async function sendMessage() {
  const input = $("#input");
  const content = input.value.trim();
  if (!content || !state.sessionId) return;
  input.value = "";
  autosize();
  $("#empty").classList.add("hidden");

  // 发送粒子爆发（从发送按钮迸发光点）
  const btn = $("#btn-send");
  if (btn && window.__burst) {
    const r = btn.getBoundingClientRect();
    window.__burst(r.left + r.width / 2, r.top + r.height / 2, 12, 5);
  }

  // 本地回显
  state.echoedContent = content;  // 供 SSE user/message 事件去重
  const wrap = el("div", "msg user");
  const tag = el("div", "role-tag");
  tag.appendChild(el("span", "avatar", "你"));
  tag.appendChild(document.createTextNode("你"));
  wrap.appendChild(tag);
  const bubble = el("div", "bubble md");
  bubble.innerHTML = renderMarkdown(content);
  wrap.appendChild(bubble);
  state.messagesEl.appendChild(wrap);
  state.messagesEl.scrollTop = state.messagesEl.scrollHeight;

  beginStreaming();
  try {
    const data = await api(`/api/sessions/${state.sessionId}/messages`, {
      method: "POST", body: JSON.stringify({ content }),
    });
    if (data && data.command && data.command.reply) {
      // 命令回复：替换流式占位
      if (state.streamingMsg) { state.streamingMsg.remove(); state.streamingMsg = null; }
      const reply = el("div", "msg assistant");
      const rtag = el("div", "role-tag");
      rtag.appendChild(el("span", "avatar", "d"));
      rtag.appendChild(document.createTextNode("命令"));
      reply.appendChild(rtag);
      const rb = el("div", "bubble md");
      rb.innerHTML = renderMarkdown(data.command.reply);
      reply.appendChild(rb);
      state.messagesEl.appendChild(reply);
    }
  } catch (err) {
    toast("发送失败：" + err.message);
  }
}

function autosize() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}

/* ---------------- 审批 / 问答 ---------------- */
function showApproval(payload) {
  $("#modal-kind").textContent = "需要人工确认";
  $("#modal-icon").textContent = "🛡️";
  $("#modal-question").textContent = payload.question;
  $("#modal-detail").textContent = payload.detail || "";
  const input = $("#modal-input");
  input.classList.add("hidden");
  $("#modal-deny").style.display = "";
  $("#modal-deny").textContent = "拒绝";
  $("#modal-allow").textContent = "允许";
  $("#modal-allow").classList.remove("hidden");
  $("#modal").classList.remove("hidden");
  const answer = async (allow) => {
    $("#modal").classList.add("hidden");
    await api(`/api/approval/${payload.qid}`, {
      method: "POST", body: JSON.stringify({ allow }),
    });
  };
  $("#modal-allow").onclick = () => answer(true);
  $("#modal-deny").onclick = () => answer(false);
}

function showQuestion(payload) {
  $("#modal-kind").textContent = "需要你回答";
  $("#modal-icon").textContent = "💬";
  $("#modal-question").textContent = payload.question;
  $("#modal-detail").textContent = payload.detail || "";
  const input = $("#modal-input");
  input.value = "";
  input.classList.remove("hidden");
  $("#modal-deny").style.display = "none";
  $("#modal-allow").textContent = "提交回答";
  $("#modal").classList.remove("hidden");
  input.focus();
  const submit = async () => {
    $("#modal").classList.add("hidden");
    input.classList.add("hidden");
    await api(`/api/questions/${payload.qid}`, {
      method: "POST", body: JSON.stringify({ text: input.value || "(无回答)" }),
    });
  };
  $("#modal-allow").onclick = submit;
  input.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  };
}

/* ============================================================
   会话浮层 + 鼠标光晕 + 极光强度
   ============================================================ */
function openSessionPop() {
  loadSessions();
  $("#session-pop").classList.remove("hidden");
}
function closeSessionPop() {
  $("#session-pop").classList.add("hidden");
}

function initCursorGlow() {
  document.addEventListener("mousemove", (e) => {
    const st = document.documentElement.style;
    st.setProperty("--mx", e.clientX + "px");
    st.setProperty("--my", e.clientY + "px");
  });
}

/* 粒子系统：漂浮光点 + 鼠标推开交互 + 粒子爆发 */
function initParticles() {
  const canvas = document.getElementById("particles");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  let w, h;
  const mouse = { x: -9999, y: -9999 };
  const COLORS = ["34,211,238", "139,92,246", "236,72,153", "255,255,255"];
  const COUNT = 70;
  let particles = [];
  let bursts = [];

  function resize() {
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
  }
  resize();
  window.addEventListener("resize", resize);

  function spawn() {
    particles = [];
    for (let i = 0; i < COUNT; i++) {
      particles.push({
        x: Math.random() * w, y: Math.random() * h,
        r: Math.random() * 2 + .6,
        vx: (Math.random() - .5) * .3,
        vy: -(Math.random() * .35 + .05),
        color: COLORS[Math.floor(Math.random() * COLORS.length)],
        tw: Math.random() * Math.PI * 2,
      });
    }
  }
  spawn();

  /* 粒子爆发：从 (x,y) 迸发 n 个光点（发送消息/点击反馈用） */
  function burst(x, y, n, power) {
    for (let i = 0; i < n; i++) {
      const ang = Math.random() * Math.PI * 2;
      const spd = Math.random() * (power || 4) + 1.5;
      bursts.push({
        x, y,
        vx: Math.cos(ang) * spd,
        vy: Math.sin(ang) * spd - 1.2,
        life: 1,
        decay: Math.random() * .04 + .02,
        r: Math.random() * 2 + 1,
        color: COLORS[Math.floor(Math.random() * COLORS.length)],
      });
    }
  }
  window.__burst = burst;

  function tick() {
    ctx.clearRect(0, 0, w, h);
    // 常驻漂浮粒子
    for (const p of particles) {
      p.x += p.vx; p.y += p.vy; p.tw += .03;
      const dx = p.x - mouse.x, dy = p.y - mouse.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 130 && dist > .01) {
        const f = (130 - dist) / 130 * 1.4;
        p.x += dx / dist * f;
        p.y += dy / dist * f;
      }
      if (p.y < -12) { p.y = h + 12; p.x = Math.random() * w; }
      if (p.x < -12) p.x = w + 12;
      if (p.x > w + 12) p.x = -12;
      const alpha = .35 + Math.sin(p.tw) * .3;
      const size = p.r * (1 + Math.sin(p.tw * .7) * .3);
      ctx.beginPath();
      ctx.arc(p.x, p.y, size, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${p.color},${Math.max(alpha, .05)})`;
      ctx.fill();
      ctx.beginPath();
      ctx.arc(p.x, p.y, size * 3.2, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${p.color},${Math.max(alpha, .05) * .14})`;
      ctx.fill();
    }
    // 爆发粒子（短暂消逝）
    for (let i = bursts.length - 1; i >= 0; i--) {
      const b = bursts[i];
      b.x += b.vx; b.y += b.vy;
      b.vy += .06;
      b.life -= b.decay;
      if (b.life <= 0) { bursts.splice(i, 1); continue; }
      ctx.beginPath();
      ctx.arc(b.x, b.y, b.r * b.life, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${b.color},${b.life})`;
      ctx.fill();
    }
    // 星座连线：邻近粒子连细线（Vercel AI 节点感）
    for (let i = 0; i < particles.length; i++) {
      const a = particles[i];
      for (let j = i + 1; j < particles.length; j++) {
        const b = particles[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < 9800) {  // ~99px
          ctx.beginPath();
          ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = `rgba(34,211,238,${.16 * (1 - d2 / 9800)})`;
          ctx.lineWidth = .6;
          ctx.stroke();
        }
      }
    }
    requestAnimationFrame(tick);
  }
  tick();

  document.addEventListener("mousemove", (e) => {
    mouse.x = e.clientX; mouse.y = e.clientY;
  });
}

/* 全局点击涟漪：任何可点元素点击扩散光波 */
function initRipple() {
  document.addEventListener("click", (e) => {
    const target = e.target.closest("button, .chip, .pop-item, .fb-btn, .act-btn, .reasoning-head, .tool-head");
    if (!target) return;
    const r = target.getBoundingClientRect();
    const size = Math.max(r.width, r.height) * 1.2;
    const span = document.createElement("span");
    span.className = "ripple";
    span.style.width = span.style.height = size + "px";
    span.style.left = (e.clientX - size / 2) + "px";
    span.style.top = (e.clientY - size / 2) + "px";
    document.body.appendChild(span);
    setTimeout(() => span.remove(), 650);
  });
}

/* 背景视差：极光随鼠标轻微移动 */
function initParallax() {
  let tx = 0, ty = 0, cx = 0, cy = 0;
  document.addEventListener("mousemove", (e) => {
    tx = (e.clientX / window.innerWidth - .5) * 26;
    ty = (e.clientY / window.innerHeight - .5) * 18;
  });
  (function loop() {
    cx += (tx - cx) * .04;
    cy += (ty - cy) * .04;
    document.documentElement.style.setProperty("--parallax-x", cx.toFixed(1) + "px");
    document.documentElement.style.setProperty("--parallax-y", cy.toFixed(1) + "px");
    requestAnimationFrame(loop);
  })();
}

/* 输入框聚焦光波 */
function initInputGlow() {
  const wrap = document.querySelector(".input-wrap");
  const input = document.getElementById("input");
  if (!wrap || !input) return;
  input.addEventListener("focus", () => wrap.classList.add("focus"));
  input.addEventListener("blur", () => wrap.classList.remove("focus"));
}

function applyAurora(strength) {
  document.documentElement.style.setProperty(
    "--aurora-strength", String((strength ?? 60) / 100));
}

/* ============================================================
   设置面板
   ============================================================ */
function applyUiSettings(theme, fontSize, density) {
  document.documentElement.dataset.theme = theme || "dark";
  document.documentElement.style.setProperty("--base-size", (fontSize || 15) + "px");
  document.documentElement.style.setProperty(
    "--density-msg-gap", density === "compact" ? "8px" : "14px");
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    const sel = data.agent_default_model || {};
    const s = data.settings || {};
    const bg = s.background || {};
    const ui = s.ui || {};
    const instr = (s.context && s.context.instructions) || {};

    // provider 下拉
    const provSel = $("#set-provider");
    provSel.innerHTML = "";
    (data.providers || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      provSel.appendChild(opt);
    });
    provSel.value = sel.provider || (data.providers && data.providers[0]) || "";

    $("#set-model").value = sel.model || "";
    $("#set-temp").value = sel.temperature !== undefined ? sel.temperature : 0.7;
    $("#set-temp-val").textContent = $("#set-temp").value;
    $("#set-maxtok").value = sel.max_tokens || 0;

    // 外观
    $("#set-theme").value = ui.theme || "dark";
    $("#set-font").value = ui.font_size || 15;
    $("#set-font-val").textContent = $("#set-font").value + "px";
    $("#set-density").value = ui.density || "comfortable";
    applyUiSettings($("#set-theme").value, $("#set-font").value, $("#set-density").value);
    $("#set-aurora").value = ui.aurora !== undefined ? ui.aurora : 70;
    $("#set-aurora-val").textContent = $("#set-aurora").value + "%";
    applyAurora($("#set-aurora").value);

    // 上下文与文档
    const extra = Array.isArray(instr.extra_files) ? instr.extra_files : ["CLAUDE.md"];
    $("#set-agents").checked = instr.enabled !== false;
    $("#set-claude").checked = extra.includes("CLAUDE.md");
    $("#set-extra-md").value = extra.filter((f) => f !== "CLAUDE.md").join(", ");

    // 压缩阈值
    $("#set-compact").value = (s.compaction_threshold && s.compaction_threshold > 0)
      ? s.compaction_threshold : 8000;

    // 会话
    $("#set-autotitle").checked = s.auto_title !== false;
  } catch { /* ignore */ }
}

async function saveSettings() {
  const btn = $("#set-save");
  btn.disabled = true;
  $("#set-status").textContent = "保存中…";

  // 收集附加 .md 文件
  const extraRaw = $("#set-extra-md").value.split(",")
    .map((f) => f.trim()).filter(Boolean);
  const extraFiles = [];
  if ($("#set-claude").checked) extraFiles.push("CLAUDE.md");
  extraRaw.forEach((f) => { if (f !== "CLAUDE.md") extraFiles.push(f); });

  const compactVal = parseInt($("#set-compact").value, 10);
  const payload = {
    agent_default_model: {
      provider: $("#set-provider").value,
      model: $("#set-model").value.trim() || undefined,
      temperature: parseFloat($("#set-temp").value),
      max_tokens: parseInt($("#set-maxtok").value, 10) || undefined,
    },
    ui: {
      theme: $("#set-theme").value,
      font_size: parseInt($("#set-font").value, 10),
      density: $("#set-density").value,
      aurora: parseInt($("#set-aurora").value, 10),
    },
    context: {
      instructions: {
        enabled: $("#set-agents").checked,
        extra_files: extraFiles,
      },
    },
    compaction_threshold: (compactVal > 0) ? compactVal : 0,
    auto_title: $("#set-autotitle").checked,
  };
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    $("#set-status").textContent = "✓ 已保存，新会话生效";
    setTimeout(() => { $("#set-status").textContent = ""; }, 2500);
    // 实时应用 UI 设置
    applyUiSettings($("#set-theme").value, $("#set-font").value, $("#set-density").value);
    applyAurora($("#set-aurora").value);
    // 更新模型标签
    const m = payload.agent_default_model.model
      || (payload.agent_default_model.provider !== "mock" ? "deepseek-v4-flash" : "mock");
    $("#model-label").textContent = m;
  } catch (err) {
    $("#set-status").textContent = "保存失败：" + err.message;
  } finally {
    btn.disabled = false;
  }
}

function initSettings() {
  $("#btn-settings").onclick = () => {
    $("#drawer").classList.add("open");
    loadSettings();
  };
  $("#drawer-close").onclick = () => $("#drawer").classList.remove("open");
  $("#set-save").onclick = saveSettings;
  // 滑块实时值
  $("#set-temp").oninput = () => $("#set-temp-val").textContent = $("#set-temp").value;
  $("#set-font").oninput = () => $("#set-font-val").textContent = $("#set-font").value + "px";
  $("#set-aurora").oninput = () => {
    $("#set-aurora-val").textContent = $("#set-aurora").value + "%";
    applyAurora($("#set-aurora").value);
  };
  $("#set-theme").onchange = () => {
    applyUiSettings($("#set-theme").value, $("#set-font").value, $("#set-density").value);
  };
  $("#set-density").onchange = () => {
    applyUiSettings($("#set-theme").value, $("#set-font").value, $("#set-density").value);
  };
  // 启动时应用已存设置
  loadSettings();
}

/* ============================================================
   第三波创新：命令面板 / 3D 倾斜 / 快捷键 / 输入计数
   ============================================================ */

/* ---- Ctrl+K 命令面板（Raycast 风格） ---- */
function initCmdPalette() {
  const pal = $("#cmd-palette");
  const input = $("#cmd-input");
  const list = $("#cmd-list");
  const hint = $("#cmd-hint");
  let items = [];
  let idx = 0;

  const cmds = [
    { icon: "➕", name: "新建会话", desc: "新开一个干净会话", run: createSession },
    { icon: "🌗", name: "切换主题", desc: "深空 / 晨曦", run: toggleTheme },
    { icon: "⚙️", name: "打开设置", desc: "模型 / 外观 / 上下文", run: openSettings },
    { icon: "🌠", name: "增强极光", desc: "背景光效 +10%", run: () => adjustAurora(10) },
    { icon: "🌌", name: "减弱极光", desc: "背景光效 −10%", run: () => adjustAurora(-10) },
    { icon: "🖥️", name: "列出可用工具", desc: "让助手枚举工具", run: () => sendCommandText("列出当前会话可用的工具") },
    { icon: "📋", name: "复制最后回复", desc: "复制最近一条助手回复", run: copyLastReply },
    { icon: "🗂️", name: "切换会话…", desc: "打开会话浮层", run: openSessionPop },
  ];

  function hl(text, q) {
    if (!q) return text;
    const i = text.toLowerCase().indexOf(q);
    if (i < 0) return text;
    return text.slice(0, i) + "<mark>" + text.slice(i, i + q.length) + "</mark>" + text.slice(i + q.length);
  }

  function paint() {
    if (!items.length) {
      list.innerHTML = '<div class="cmd-empty">没有匹配的命令 <kbd>Esc</kbd> 关闭</div>';
      return;
    }
    // 只切换 active 高亮，绝不重建 DOM（重建会吞掉点击事件 → "点击没反应"）
    Array.from(list.children).forEach((it, i) => {
      it.classList.toggle("active", i === idx);
    });
    const a = list.querySelector(".cmd-item.active");
    if (a) a.scrollIntoView({ block: "nearest" });
  }

  function render() {
    const q = (input.value || "").trim().toLowerCase();
    items = cmds.filter((c) => !q
      || c.name.toLowerCase().includes(q)
      || (c.desc || "").toLowerCase().includes(q));
    idx = 0;
    list.innerHTML = "";
    items.forEach((c, i) => {
      const it = el("div", "cmd-item" + (i === idx ? " active" : ""));
      it.appendChild(el("span", "ci-icon", c.icon));
      const name = el("span", "ci-name");
      name.innerHTML = hl(c.name, input.value.trim());
      it.appendChild(name);
      if (c.desc) it.appendChild(el("span", "ci-desc", hl(c.desc, input.value.trim())));
      // 鼠标：只改高亮不重建；点击直接执行
      it.onmousemove = () => { idx = i; paint(); };
      it.onclick = (e) => { e.preventDefault(); e.stopPropagation(); c.run(); close(); };
      list.appendChild(it);
    });
    paint();
  }

  function open() {
    pal.classList.remove("hidden");
    input.value = "";
    hint.textContent = state.sessionId ? "会话 " + state.sessionId.slice(0, 10) : "尚无会话";
    render();
    input.focus();
  }
  function close() { pal.classList.add("hidden"); }

  input.oninput = render;
  input.onkeydown = (e) => {
    if (e.key === "ArrowDown") { idx = (idx + 1) % items.length; paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { idx = (idx - 1 + items.length) % items.length; paint(); e.preventDefault(); }
    else if (e.key === "Enter") { if (items[idx]) { items[idx].run(); close(); } e.preventDefault(); }
    else if (e.key === "Escape") close();
  };
  pal.addEventListener("mousedown", (e) => { if (e.target === pal) close(); });
  window.__togglePalette = () => (pal.classList.contains("hidden") ? open() : close());
  // 独立命令按钮（输入区左侧 + 空状态入口）
  const b1 = $("#btn-cmd"), b2 = $("#btn-cmd-empty");
  if (b1) b1.onclick = () => window.__togglePalette();
  if (b2) b2.onclick = () => window.__togglePalette();
}

function openSettings() { $("#drawer").classList.add("open"); loadSettings(); }

function toggleTheme() {
  const cur = document.documentElement.dataset.theme || "dark";
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  const sel = $("#set-theme");
  if (sel) sel.value = next;
  toast("已切换为" + (next === "dark" ? "🌑 深空" : "🌅 晨曦") + "主题");
}

function adjustAurora(delta) {
  const cur = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--aurora-strength"));
  const next = Math.max(.1, Math.min(1, (isNaN(cur) ? .7 : cur) + delta / 100));
  document.documentElement.style.setProperty("--aurora-strength", String(next));
  toast("极光强度 " + Math.round(next * 100) + "%");
}

function sendCommandText(text) {
  if (!state.sessionId) { toast("请先新建会话"); return; }
  const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  set.call($("#input"), text);
  $("#input").dispatchEvent(new Event("input", { bubbles: true }));
  sendMessage();
}

function copyLastReply() {
  const msgs = document.querySelectorAll(".msg.assistant .md");
  if (!msgs.length) { toast("还没有助手回复"); return; }
  const text = msgs[msgs.length - 1].innerText;
  navigator.clipboard.writeText(text)
    .then(() => toast("已复制最后回复"))
    .catch(() => toast("复制失败"));
}

/* ---- 3D 倾斜卡片（工具卡悬停 3D 翻转 + 光斑跟随） ---- */
function initTilt() {
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest(".tilt");
    document.querySelectorAll(".tilting").forEach((x) => { x.classList.remove("tilting"); x.style.transform = ""; });
    if (t) t.classList.add("tilting");
  });
  document.addEventListener("mousemove", (e) => {
    const t = document.querySelector(".tilting");
    if (!t) return;
    const r = t.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width - .5;
    const py = (e.clientY - r.top) / r.height - .5;
    t.style.transform = `perspective(900px) rotateX(${(-py * 6).toFixed(2)}deg) rotateY(${(px * 9).toFixed(2)}deg) translateY(-2px) scale(1.015)`;
  });
}

/* ---- 全局快捷键：Ctrl+K / Ctrl+Enter / Esc ---- */
function initShortcuts() {
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (window.__togglePalette) window.__togglePalette();
      return;
    }
    if (e.ctrlKey && e.key === "Enter") {
      const inp = $("#input");
      if (document.activeElement === inp && inp.value.trim() && state.sessionId) {
        e.preventDefault(); sendMessage();
      }
      return;
    }
    if (e.key === "Escape") {
      if (!$("#cmd-palette").classList.contains("hidden")) { $("#cmd-palette").classList.add("hidden"); return; }
      if ($("#drawer").classList.contains("open")) { $("#drawer").classList.remove("open"); return; }
      if (!$("#session-pop").classList.contains("hidden")) closeSessionPop();
    }
  });
}

/* ---- 输入框占位符循环打字（Raycast/Spotlight 感） ---- */
function initPlaceholderCycle() {
  const phrases = [
    "问点什么… 比如：介绍一下你自己",
    "试试：列出当前会话可用的工具",
    "或者：用 code 工具算 2 的 10 次方",
    "也可以输入 / 命令，或按 Ctrl+K 打开面板",
  ];
  const inp = $("#input");
  let p = 0, ch = 0, del = false, timer = null;
  function step() {
    if (inp.value) { timer = setTimeout(step, 2000); return; }  // 有内容则等待
    const ph = phrases[p];
    if (!del) {
      ch++;
      inp.placeholder = ph.slice(0, ch) + "▌";
      if (ch >= ph.length) { del = true; timer = setTimeout(step, 1800); return; }
    } else {
      ch--;
      inp.placeholder = ph.slice(0, ch) + "▌";
      if (ch <= 0) { del = false; p = (p + 1) % phrases.length; }
    }
    timer = setTimeout(step, del ? 26 : 66);
  }
  inp.addEventListener("focus", () => { clearTimeout(timer); inp.placeholder = "输入消息或 / 命令…（Enter 发送，Shift+Enter 换行）"; });
  inp.addEventListener("blur", () => { if (!inp.value) step(); });
  inp.addEventListener("input", () => { if (inp.value) clearTimeout(timer); });
  step();
}

/* ---- 发送按钮磁吸：hover 输入区时按钮轻轻靠近鼠标 ---- */
function initMagnet() {
  const dock = $("#dock");
  const btn = $("#btn-send");
  if (!dock || !btn) return;
  dock.addEventListener("mousemove", (e) => {
    const r = btn.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const dx = e.clientX - cx, dy = e.clientY - cy;
    const dist = Math.hypot(dx, dy);
    if (dist < 130 && dist > .01) {
      const pull = (130 - dist) / 130 * 6;   // 最多位移 6px
      btn.style.transform = `translate(${(dx / dist * pull).toFixed(1)}px, ${(dy / dist * pull).toFixed(1)}px)`;
      btn.classList.add("magnet");
    } else {
      btn.style.transform = "";
      btn.classList.remove("magnet");
    }
  });
  dock.addEventListener("mouseleave", () => { btn.style.transform = ""; btn.classList.remove("magnet"); });
}

/* ---- 消息区滚动进度光条（dock 顶部渐变线随阅读伸缩） ---- */
function initScrollProgress() {
  const prog = $("#scroll-progress");
  const msgs = $("#messages");
  if (!prog || !msgs) return;
  const update = () => {
    const max = msgs.scrollHeight - msgs.clientHeight;
    const ratio = max > 0 ? msgs.scrollTop / max : 0;
    prog.style.transform = `scaleX(${Math.max(ratio, 0.001)})`;
  };
  msgs.addEventListener("scroll", update, { passive: true });
  setInterval(update, 800);  // 消息增长时兜底
}

/* ---- 输入实时计数（字符 / token / 行） ---- */
function initCounter() {
  const input = $("#input");
  const label = $("#count-label");
  const update = () => {
    const v = input.value;
    if (!v.length) { label.textContent = ""; label.classList.remove("hot"); return; }
    const chars = v.length;
    const tok = Math.ceil(chars / 1.5);
    const lines = v.split("\n").length;
    label.textContent = chars + " 字符 · ~" + tok + " token" + (lines > 1 ? " · " + lines + " 行" : "");
    label.classList.add("hot");
  };
  input.addEventListener("input", update);
  input.addEventListener("keydown", update);
}

/* ---------------- 初始化 ---------------- */
function applyProviders() {
  // 从 providers 推断模型标签（后端仅给 provider 名）
  fetch("/api/providers").then(r => r.json()).then((data) => {
    const providers = data.providers || [];
    $("#providers").textContent = providers.join(" · ");
    // 模型标签：env 已知模型名（前端静态标注）
    const model = providers.includes("deepseek") ? "deepseek-v4-flash" : (providers[0] || "");
    $("#model-label").textContent = model;
  }).catch(() => {
    $("#providers").textContent = "offline";
  });
}

function init() {
  state.statusEl = $("#status");
  state.messagesEl = $("#messages");
  state.emptyEl = $("#empty");

  // 空状态 → 有会话后隐藏
  const hasSession = () => state.sessionId !== null;
  const showEmpty = () => {
    if (!hasSession()) { state.emptyEl.classList.remove("hidden"); state.messagesEl.classList.add("hidden"); }
    else { state.emptyEl.classList.add("hidden"); state.messagesEl.classList.remove("hidden"); }
  };

  $("#btn-new").onclick = createSession;
  $("#btn-send").onclick = sendMessage;
  // 会话浮层
  $("#btn-sessions").onclick = (e) => {
    e.stopPropagation();
    if ($("#session-pop").classList.contains("hidden")) openSessionPop();
    else closeSessionPop();
  };
  document.addEventListener("click", (e) => {
    if (!$("#session-pop").classList.contains("hidden")
        && !e.target.closest("#session-pop")
        && !e.target.closest("#btn-sessions")) closeSessionPop();
  });
  initCursorGlow();
  initParticles();
  initRipple();
  initParallax();
  initInputGlow();
  initCmdPalette();
  initTilt();
  initShortcuts();
  initCounter();
  initPlaceholderCycle();
  initMagnet();
  initScrollProgress();
  initCodeCopy();
  initWhale();
  initSettings();
  $("#btn-cancel").onclick = async () => {
    if (state.sessionId) {
      try { await api(`/api/sessions/${state.sessionId}/cancel`, { method: "POST" }); toast("已停止"); }
      catch { /* ignore */ }
    }
  };
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  $("#input").addEventListener("input", autosize);
  $("#input").addEventListener("keyup", autosize);

  // 空状态快捷 chips
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.onclick = () => {
      if (!state.sessionId) { createSession().then(() => setTimeout(() => {
        $("#input").value = chip.dataset.prompt || "";
        sendMessage();
      }, 300)); } else {
        $("#input").value = chip.dataset.prompt || "";
        sendMessage();
      }
    };
  });

  // 全局代码复制委托
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".copy-btn");
    if (btn) {
      const pre = btn.closest("pre");
      if (pre) {
        const code = pre.querySelector("code");
        const text = code ? code.innerText.replace(/复制$/, "").trim() : "";
        if (text) copyText(text);
      }
    }
  });

  applyProviders();

  // 初始会话
  api("/api/sessions").then((sessions) => {
    if (sessions.length === 0) {
      showEmpty();
      $("#session-title").textContent = "新会话";
      createSession().then(showEmpty);
    } else {
      selectSession(sessions[sessions.length - 1].id);
      showEmpty();
    }
  }).catch(() => { showEmpty(); });
}

/* ============================================================
   DeepSeek 小鲸鱼页宠：眼睛跟随鼠标 + 点击冒台词 + AI 状态联动
   ============================================================ */
function initWhale() {
  const pet = document.querySelector("#pet-whale");
  if (!pet) return;
  const bub = document.querySelector("#whale-bubble");
  const eyes = document.querySelectorAll(".whale-eye");
  const LINES = [
    "深度思考中… 🐳",
    "让我潜入数据的深海",
    "这道题我见过！",
    "正在浮出水面…",
    "让我想想 🤔",
    "嘘…在思考",
    "答案就在海面下！",
    "我看过 DeepSeek 的文档",
    "深海模式，启动！",
  ];
  // 眼睛跟随鼠标（节流到 rAF）
  let raf = null;
  document.addEventListener("mousemove", (e) => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = null;
      const r = pet.getBoundingClientRect();
      if (r.width === 0) return;
      const cx = r.left + r.width * 0.66;
      const cy = r.top + r.height * 0.42;
      const dx = Math.max(-1, Math.min(1, (e.clientX - cx) / r.width));
      const dy = Math.max(-1, Math.min(1, (e.clientY - cy) / r.height));
      eyes.forEach((ey) => ey.setAttribute("transform", `translate(${dx * 4} ${dy * 3})`));
    });
  });
  // 点击冒随机台词
  pet.addEventListener("click", () => {
    bub.textContent = LINES[Math.floor(Math.random() * LINES.length)];
    pet.classList.add("talking");
    clearTimeout(pet._t);
    pet._t = setTimeout(() => pet.classList.remove("talking"), 2600);
  });
}

document.addEventListener("DOMContentLoaded", init);
