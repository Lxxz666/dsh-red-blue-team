/* 前端 JS 冒烟验收（node 运行）：
   用最小 DOM/fetch 桩跑 redteam/web/static/index.html 的 init()，
   捕获 ReferenceError / TypeError 这类浏览器运行时错误。
   用法: node frontend_harness.js <index.html 路径> */
"use strict";
const fs = require("fs");
const htmlPath = process.argv[2] || "redteam/web/static/index.html";
const html = fs.readFileSync(htmlPath, "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function makeEl(id) {
  return {
    id, textContent: "", innerHTML: "", style: {},
    disabled: false, checked: false, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild() {}, remove() {},
    querySelectorAll() { return []; }, querySelector() { return null; },
  };
}
const els = {};
global.document = {
  getElementById: (id) => (els[id] = els[id] || makeEl(id)),
  createElement: () => makeEl("x"),
  createDocumentFragment: () => ({ appendChild() {} }),
  querySelectorAll: () => [],
  addEventListener() {},
};
global.window = {};
global.setTimeout = () => 0;   // 不回环，避免死循环
global.clearTimeout = () => {};

async function run(llmAvailable) {
  global.fetch = async (url) => {
    if (String(url).includes("/api/config")) {
      return { ok: true, headers: { get: () => "application/json" },
               json: async () => ({ target: "test-lab", type: "lab",
                                    llm_available: llmAvailable,
                                    llm_model: "deepseek-v4-flash" }) };
    }
    if (String(url).includes("/api/tasks")) {
      return { ok: true, headers: { get: () => "application/json" },
               json: async () => ({ tasks: [] }) };
    }
    return { ok: false, status: 404, statusText: "nf",
             headers: { get: () => "application/json" },
             json: async () => ({ detail: "x" }) };
  };
  try {
    eval(script + `
global.__p = (async () => {
  try {
    await init();
    const badge = document.getElementById("llmBadge");
    const target = document.getElementById("targetBadge");
    console.log("llm_available=${llmAvailable} → INIT OK");
    console.log("  llmBadge:", (badge.innerHTML || badge.textContent || "").replace(/<[^>]+>/g, ""));
    console.log("  targetBadge:", (target.innerHTML || target.textContent || ""));
    updateTargetBadge({ name: "我的业务系统", target: "http://192.168.1.50:8080" });
    console.log("  targetBadge(选中任务后):", (target.innerHTML || target.textContent || ""));
  } catch (e) {
    console.log("llm_available=${llmAvailable} → INIT FAIL:", e.message);
    process.exitCode = 1;
  }
})();
`);
    await global.__p;
  } catch (e) {
    console.log(`llm_available=${llmAvailable} → INIT FAIL:`, e.message);
    process.exitCode = 1;
  }
}

(async () => {
  await run(true);
  await run(false);
})();
