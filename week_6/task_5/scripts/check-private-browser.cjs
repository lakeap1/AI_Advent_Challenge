"use strict";

// Controlled browser contract check. Responses here are fixtures, not a live model.
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const playwrightModule = process.env.PLAYWRIGHT_NODE_MODULES
  ? path.join(process.env.PLAYWRIGHT_NODE_MODULES, "playwright")
  : "playwright";
const { chromium } = require(playwrightModule);

const root = path.resolve(__dirname, "..");
const files = {
  "/": ["templates/private_chat.html", "text/html; charset=utf-8"],
  "/static/private_chat.css": ["static/private_chat.css", "text/css; charset=utf-8"],
  "/static/private_chat.js": ["static/private_chat.js", "text/javascript; charset=utf-8"],
};
const server = http.createServer((request, response) => {
  const found = files[request.url];
  if (!found) { response.writeHead(404).end(); return; }
  const filename = path.join(root, found[0]);
  if (!fs.existsSync(filename)) { response.writeHead(404).end(); return; }
  response.writeHead(200, { "content-type": found[1] });
  fs.createReadStream(filename).pipe(response);
});

const baseState = () => ({
  messages: [], receipts: [],
  ledger: { requests: 0, known_total_usd: "0", total_cost_usd: "0", complete: true, unknown_usage: false },
  pricing: { model: "qwen3.5:4b", service_mode: "local", source: "Ollama", checked_on: "2026-10-12" },
  blocked: false,
  limits: { rate_requests: 10, rate_window_seconds: 60, max_context_tokens: 4096, max_output_tokens: 384, max_prompt_characters: 2000, max_queue: 3 },
  counters: { inference_calls: 0 },
});

async function main() {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  const origin = `http://127.0.0.1:${server.address().port}`;
  let state = baseState();
  let requests = [];
  let askMode = "normal";
  let holdAsk;
  let authorized = true;
  try {
    await page.route("**/api/private/**", async route => {
      const request = route.request();
      requests.push({ url: request.url(), method: request.method(), auth: request.headers().authorization, body: request.postDataJSON?.() });
      const send = (status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (!authorized || request.headers().authorization !== "Bearer test-secret") return send(401, { error: "unauthorized" });
      if (request.url().endsWith("/state")) return send(200, state);
      if (request.url().endsWith("/runtime")) return send(200, { status: "ready", model: "qwen3.5:4b", version: "0.40.3", processor: "CPU" });
      if (request.url().endsWith("/ask")) {
        if (askMode === "held") await new Promise(resolve => { holdAsk = resolve; });
        if (askMode === "rate") return send(429, { error: "rate_limited" });
        if (askMode === "busy") return send(503, { error: "queue_full" });
        if (askMode === "rejected") return send(400, { status: "rejected", error: "Слишком длинный вопрос", state });
        const prompt = request.postDataJSON().prompt;
        state = {
          ...state,
          messages: [...state.messages, { role: "user", content: prompt }, { role: "assistant", content: "<img src=x onerror=alert(1)>\nОтвет по свету." }],
          receipts: [...state.receipts, { id: state.receipts.length + 1, status: "accepted", input_policy: "accepted", output_policy: "accepted", usage_status: "known", usage: { input_tokens: 21, output_tokens: 9, total_tokens: 30 }, cost_usd: "0", model: "qwen3.5:4b" }],
          ledger: { requests: state.receipts.length + 1, known_total_usd: "0", total_cost_usd: "0", complete: true, unknown_usage: false },
        };
        return send(200, { status: "ok", answer: "<img src=x onerror=alert(1)>\nОтвет по свету.", state });
      }
      return send(404, {});
    });
    await page.goto(origin);
    assert.equal(requests.length, 0, "no API call before connect");
    await page.locator("#token").fill("test-secret");
    await page.locator("#connect").click();
    await page.locator("#messages").getByText("Пока нет сообщений").waitFor();
    assert(requests.some(item => item.url.endsWith("/state") && item.auth === "Bearer test-secret"));
    await page.locator("#prompt").fill("Как поставить свет?");
    await page.locator("#prompt").press("Control+Enter");
    await page.locator("#messages").getByText("Ответ по свету.", { exact: false }).waitFor();
    assert.equal(requests.filter(item => item.url.endsWith("/ask")).length, 1, "Ctrl+Enter sends once");
    assert.equal(await page.locator("#messages img").count(), 0, "model HTML is inert");
    assert.equal(await page.locator("#messages .message").count(), 2);
    assert.match(await page.locator("#receipts").innerText(), /21/);
    askMode = "held";
    await page.locator("#prompt").fill("Вопрос в очереди");
    await page.locator("#send").click();
    await page.waitForFunction(() => document.querySelector("#send").disabled);
    assert.equal(await page.locator("#send").isDisabled(), true);
    askMode = "normal"; holdAsk();
    await page.locator("#status").getByText(/Ответ сохранён/).waitFor();
    assert.equal(await page.locator("#prompt").inputValue(), "", "successful send clears the composer");
    askMode = "rate";
    await page.locator("#prompt").fill("Ещё вопрос");
    await page.locator("#send").click();
    await page.locator("#status").getByText(/лимит|часто/i).waitFor();
    assert.equal(await page.locator("#messages .message").count(), 4, "429 preserves history");
    askMode = "busy";
    await page.locator("#send").click();
    await page.locator("#status").getByText(/занят|очеред/i).waitFor();
    askMode = "rejected";
    await page.locator("#send").click();
    await page.locator("#status").getByText(/Слишком длинный вопрос/).waitFor();
    const pendingReceipt = { id: 3, status: "pending", input_policy: "accepted", output_policy: "pending", usage_status: "pending", usage: null, cost_usd: null };
    state = { ...state, ledger: { ...state.ledger, requests: 3, total_cost_usd: null, complete: false, unknown_usage: true }, blocked: true, block_reason: "Неизвестен расход токенов предыдущего запроса", receipts: [...state.receipts, pendingReceipt] };
    await page.locator("#refresh").click();
    await page.locator("#status").getByText(/Другой запрос выполняется/).waitFor();
    assert.match(await page.locator("#receipt-note").innerText(), /выполняется/i);
    assert.equal(await page.locator("#send").isDisabled(), true);
    const asksBeforePoll = requests.filter(item => item.url.endsWith("/ask")).length;
    const readsBeforePoll = requests.filter(item => item.url.endsWith("/state")).length;
    state = { ...state, blocked: false, block_reason: null, receipts: [...state.receipts.slice(0, -1), { ...pendingReceipt, status: "ok", output_policy: "accepted", usage_status: "known", usage: { input_tokens: 17, output_tokens: 8, total_tokens: 25 }, cost_usd: "0" }], ledger: { ...state.ledger, complete: true, unknown_usage: false, total_cost_usd: "0" } };
    await page.waitForFunction(previous => document.querySelector("#status").textContent.includes("Общий запрос завершён") && !document.querySelector("#send").disabled, readsBeforePoll);
    assert(requests.filter(item => item.url.endsWith("/state")).length > readsBeforePoll, "pending state is read automatically");
    assert.equal(requests.filter(item => item.url.endsWith("/ask")).length, asksBeforePoll, "poll never retries inference");
    state = { ...state, ledger: { ...state.ledger, requests: 4, total_cost_usd: null, complete: false, unknown_usage: true }, blocked: true, block_reason: "Неизвестен расход токенов", receipts: [...state.receipts, { id: 4, status: "failed", input_policy: "accepted", output_policy: "unknown", usage_status: "unknown", usage: null, cost_usd: null }] };
    await page.locator("#refresh").click();
    await page.locator("#status").getByText(/Неизвестен расход токенов/).waitFor();
    assert.match(await page.locator("#total-cost").innerText(), /неизвестна/i);
    assert.equal(await page.locator("#send").isDisabled(), true);
    const readsAtUnknown = requests.filter(item => item.url.endsWith("/state")).length;
    await page.waitForTimeout(2300);
    assert.equal(requests.filter(item => item.url.endsWith("/state")).length, readsAtUnknown, "terminal unknown stops polling");
    state = { ...state, messages: Array.from({ length: 30 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", content: `Сообщение ${index + 1}\n${"Подробный ответ. ".repeat(20)}` })) };
    await page.locator("#refresh").click();
    await page.waitForFunction(() => document.querySelectorAll("#messages .message").length === 30);
    const layout = await page.evaluate(() => {
      const log = document.querySelector("#messages");
      return { entries: log.querySelectorAll(".message").length, scrollable: log.scrollHeight > log.clientHeight, lastVisible: log.scrollTop + log.clientHeight >= log.scrollHeight - 2, horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth };
    });
    assert.equal(layout.entries, 30, "full history rendered");
    assert.equal(layout.scrollable, true, "history scrolls within its panel");
    assert.equal(layout.lastVisible, true, "latest message is visible");
    assert.equal(layout.horizontalOverflow, false, "1920px layout has no horizontal clipping");
    const stateReadsBeforeReload = requests.filter(item => item.url.endsWith("/state")).length;
    await page.reload();
    assert.equal(requests.filter(item => item.url.endsWith("/state")).length, stateReadsBeforeReload, "reload does not reconnect");
    assert.equal(await page.locator("#token").inputValue(), "");
    await page.locator("#token").fill("wrong-secret");
    await page.locator("#connect").click();
    await page.locator("#status").getByText(/Неверный ключ|доступ/i).waitFor();
    assert.equal(await page.locator("#messages .message").count(), 0, "401 does not reveal history");
    state = { ...baseState(), blocked: true, block_reason: "Неизвестен расход токенов", receipts: [pendingReceipt], ledger: { requests: 1, known_total_usd: "0", total_cost_usd: null, complete: false, unknown_usage: true } };
    await page.locator("#token").fill("test-secret");
    await page.locator("#connect").click();
    await page.locator("#status").getByText(/Другой запрос выполняется/).waitFor();
    authorized = false;
    await page.locator("#status").getByText(/Неверный ключ доступа/).waitFor();
    const readsAfter401 = requests.filter(item => item.url.endsWith("/state")).length;
    await page.waitForTimeout(2300);
    assert.equal(requests.filter(item => item.url.endsWith("/state")).length, readsAfter401, "401 stops pending polling");
    assert.equal(await page.locator("#send").isDisabled(), true);
    console.log("PASS controlled browser: connect, reply, hostile HTML, busy, 429, 503, rejection, pending recovery, unknown cost, reload, 401 polling stop");
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
