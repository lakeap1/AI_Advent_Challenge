(() => {
  "use strict";

  const byId = id => document.getElementById(id);
  const ui = Object.fromEntries(["token", "connect", "access-form", "prompt", "send", "composer", "messages", "status", "receipts", "receipt-note", "total-cost", "limits", "runtime", "runtime-model", "runtime-version", "runtime-processor", "refresh", "connection-indicator", "prompt-hint"].map(id => [id, byId(id)]));
  let key = null;
  let busy = false;
  let blocked = false;
  let connected = false;
  let pending = false;
  let pendingTimer = null;
  let pendingPollInFlight = false;
  let pendingPollAttempts = 0;
  let connectionEpoch = 0;
  const PENDING_POLL_MS = 2000;
  const PENDING_POLL_LIMIT = 150;

  function stopPendingPoll() {
    if (pendingTimer !== null) clearTimeout(pendingTimer);
    pendingTimer = null;
  }

  function schedulePendingPoll() {
    if (!connected || !pending || pendingTimer !== null || pendingPollInFlight) return;
    if (pendingPollAttempts >= PENDING_POLL_LIMIT) {
      setStatus("Запрос ещё не завершён. Автоматическая проверка остановлена; обновите состояние вручную.", "busy");
      return;
    }
    const epoch = connectionEpoch;
    pendingTimer = setTimeout(async () => {
      pendingTimer = null;
      if (epoch !== connectionEpoch || !connected || !pending) return;
      if (busy) { schedulePendingPoll(); return; }
      pendingPollInFlight = true;
      setControls();
      pendingPollAttempts += 1;
      try {
        const { response, data } = await api("state");
        if (epoch !== connectionEpoch) return;
        if (response.status === 401) {
          disconnect();
          setStatus(failureMessage(response, data), "error");
          return;
        }
        if (!response.ok) {
          setStatus(failureMessage(response, data), "error");
          return;
        }
        renderState(data);
        if (!blocked) setStatus("Общий запрос завершён. История обновлена.");
      } catch {
        if (epoch === connectionEpoch) setStatus("Нет связи с сервисом. Проверим состояние ещё раз; вопрос не отправляется повторно.", "error");
      } finally {
        pendingPollInFlight = false;
        setControls();
        schedulePendingPoll();
      }
    }, PENDING_POLL_MS);
  }

  function setStatus(message, tone = "normal") {
    ui.status.textContent = message;
    ui.status.className = `status-box ${tone}`;
  }

  function setControls() {
    ui.connect.disabled = busy;
    ui.refresh.disabled = !connected || busy || pendingPollInFlight;
    ui.prompt.disabled = !connected || busy || blocked;
    ui.send.disabled = !connected || busy || blocked || !ui.prompt.value.trim();
    ui["connection-indicator"].textContent = connected ? "Подключено" : "Не подключено";
    ui["connection-indicator"].classList.toggle("connected", connected);
  }

  function printable(value, fallback = "—") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function appendDefinition(list, term, detail) {
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = term;
    dd.textContent = detail;
    row.append(dt, dd);
    list.append(row);
  }

  function renderMessages(messages) {
    ui.messages.replaceChildren();
    if (!Array.isArray(messages) || messages.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const title = document.createElement("h2");
      title.textContent = "Пока нет сообщений";
      const hint = document.createElement("p");
      hint.textContent = "Задайте вопрос о компьютерной графике или художественной работе.";
      empty.append(title, hint);
      ui.messages.append(empty);
      return;
    }
    for (const item of messages) {
      if (!item || (item.role !== "user" && item.role !== "assistant")) continue;
      const article = document.createElement("article");
      article.className = `message ${item.role}`;
      const label = document.createElement("span");
      label.className = "message-label";
      label.textContent = item.role === "user" ? "ВЫ" : "АТЛАС";
      const body = document.createElement("div");
      body.className = "message-body";
      body.textContent = printable(item.content, "");
      article.append(label, body);
      ui.messages.append(article);
    }
    ui.messages.scrollTop = ui.messages.scrollHeight;
  }

  function renderReceipts(receipts, ledger, hasPending) {
    ui.receipts.replaceChildren();
    if (!Array.isArray(receipts) || receipts.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 3;
      cell.textContent = "Запросов пока нет";
      row.append(cell);
      ui.receipts.append(row);
    } else {
      for (const receipt of receipts.slice().reverse()) {
        const row = document.createElement("tr");
        const usage = receipt && receipt.usage && typeof receipt.usage === "object" ? receipt.usage : {};
        const cells = [
          `#${printable(receipt.id)} · ${printable(receipt.status, "статус неизвестен")}\nВход: ${printable(receipt.input_policy)} · Выход: ${printable(receipt.output_policy)}`,
          receipt.usage_status === "not_requested" ? "без вызова API" : `${printable(usage.input_tokens)} / ${printable(usage.output_tokens)} / ${printable(usage.total_tokens)}`,
          receipt.usage_status === "not_requested" ? "без вызова" : receipt.cost_usd === null || receipt.cost_usd === undefined ? "неизвестна" : printable(receipt.cost_usd),
        ];
        for (const value of cells) { const cell = document.createElement("td"); cell.textContent = value; cell.style.whiteSpace = "pre-line"; row.append(cell); }
        ui.receipts.append(row);
      }
    }
    const complete = ledger && Array.isArray(receipts) && (ledger.requests === 0 || receipts.length >= ledger.requests) && ledger.complete === true && ledger.unknown_usage !== true && ledger.total_cost_usd !== null && ledger.total_cost_usd !== undefined;
    ui["total-cost"].textContent = complete ? `$${printable(ledger.total_cost_usd)}` : "неизвестна";
    ui["receipt-note"].textContent = hasPending
      ? "Запрос выполняется: расход ещё не получен. Состояние обновится автоматически."
      : complete
        ? "Квитанции включают input/output policy и фактические токены каждого вызова."
        : "Учёт неполный: неизвестный расход нельзя считать нулём. Следующие вопросы остановлены сервером.";
  }

  function renderLimits(limits) {
    if (!limits || typeof limits !== "object") return;
    ui.limits.replaceChildren();
    appendDefinition(ui.limits, "Вопросов", `${printable(limits.ask_attempts ?? limits.rate_requests, "10")} / ${printable(limits.window_seconds ?? limits.rate_window_seconds, "60")} сек`);
    appendDefinition(ui.limits, "Вопрос", `до ${printable(limits.max_prompt_characters, "2000")} символов`);
    appendDefinition(ui.limits, "Контекст", `${printable(limits.num_ctx ?? limits.max_context_tokens, "4096")} токенов`);
    appendDefinition(ui.limits, "Ответ", `до ${printable(limits.num_predict ?? limits.max_output_tokens, "384")} токенов`);
    appendDefinition(ui.limits, "Очередь", `${printable(limits.waiting ?? limits.max_queue, "3")} ожидания`);
    if (Number.isSafeInteger(limits.max_prompt_characters) && limits.max_prompt_characters > 0) {
      ui.prompt.maxLength = limits.max_prompt_characters;
      ui["prompt-hint"].textContent = `До ${limits.max_prompt_characters} символов · Ctrl+Enter — отправить`;
    }
  }

  function renderState(state) {
    if (!state || typeof state !== "object") return;
    const receipts = Array.isArray(state.receipts) ? state.receipts : [];
    const terminalUnknown = receipts.some(receipt => receipt && receipt.usage_status === "unknown");
    pending = state.blocked === true && !terminalUnknown && receipts.some(receipt => receipt && receipt.usage_status === "pending");
    renderMessages(state.messages);
    renderReceipts(state.receipts, state.ledger, pending);
    renderLimits(state.limits);
    blocked = state.blocked === true;
    if (pending) setStatus("Другой запрос выполняется. История обновится автоматически после его завершения.", "busy");
    else if (blocked) setStatus(printable(state.block_reason, "Учёт предыдущего запроса неизвестен. Новые вопросы остановлены."), "error");
    if (pending) schedulePendingPoll();
    else { stopPendingPoll(); pendingPollAttempts = 0; }
    setControls();
  }

  function renderRuntime(runtime) {
    if (!runtime || typeof runtime !== "object") return;
    const ready = runtime.status === "ready";
    ui.runtime.textContent = ready ? "Модель доступна для запросов." : printable(runtime.error, "Локальная модель сейчас недоступна.");
    ui["runtime-model"].textContent = printable(runtime.model);
    ui["runtime-version"].textContent = printable(runtime.version);
    ui["runtime-processor"].textContent = printable(runtime.processor, "не подтверждён");
  }

  async function api(path, options = {}) {
    const response = await fetch(`/api/private/${path}`, {
      ...options,
      cache: "no-store",
      headers: { Authorization: `Bearer ${key}`, ...(options.body ? { "Content-Type": "application/json" } : {}) },
    });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    return { response, data };
  }

  function failureMessage(response, data) {
    if (response.status === 401) return "Неверный ключ доступа. Проверьте ключ и подключитесь снова.";
    if (response.status === 429) return "Слишком часто: лимит вопросов исчерпан. Подождите окончания окна и повторите вручную.";
    if (response.status === 503) return "Сервис занят или очередь заполнена. Попробуйте позже вручную.";
    if (response.status === 409) return printable(data.error, "Учёт предыдущего запроса неизвестен; новые вопросы остановлены.");
    if (response.status === 400) return printable(data.error, "Вопрос отклонён политикой или ограничением контекста.");
    return printable(data.error, `Ошибка сервиса: HTTP ${response.status}.`);
  }

  function disconnect() {
    connectionEpoch += 1;
    stopPendingPoll();
    pendingPollAttempts = 0;
    pending = false;
    key = null;
    connected = false;
    blocked = false;
    ui.token.value = "";
    ui.messages.replaceChildren();
    ui.receipts.replaceChildren();
    ui["total-cost"].textContent = "—";
    ui.runtime.textContent = "Сведения появятся после подключения.";
    ui["runtime-model"].textContent = "—";
    ui["runtime-version"].textContent = "—";
    ui["runtime-processor"].textContent = "—";
    setControls();
  }

  async function loadStateAndRuntime() {
    const stateResult = await api("state");
    if (!stateResult.response.ok) {
      if (stateResult.response.status === 401) disconnect();
      throw new Error(failureMessage(stateResult.response, stateResult.data));
    }
    connected = true;
    renderState(stateResult.data);
    const runtimeResult = await api("runtime");
    if (runtimeResult.response.ok) renderRuntime(runtimeResult.data);
    else if (runtimeResult.response.status === 401) {
      disconnect();
      throw new Error(failureMessage(runtimeResult.response, runtimeResult.data));
    } else ui.runtime.textContent = failureMessage(runtimeResult.response, runtimeResult.data);
  }

  ui["access-form"].addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !ui.token.value.trim()) return;
    const candidate = ui.token.value.trim();
    connectionEpoch += 1;
    stopPendingPoll();
    pendingPollAttempts = 0;
    pending = false;
    ui.token.value = "";
    key = candidate;
    busy = true;
    setControls();
    setStatus("Подключаемся к приватному сервису…", "busy");
    try {
      await loadStateAndRuntime();
      if (!blocked) setStatus("Подключено. Общая история загружена.");
    } catch (error) {
      if (!connected) key = null;
      setStatus(error instanceof TypeError ? "Нет связи с сервисом. Проверьте SSH-туннель и повторите подключение." : error.message, "error");
    } finally { busy = false; setControls(); }
  });

  ui.refresh.addEventListener("click", async () => {
    if (!connected || busy) return;
    stopPendingPoll();
    pendingPollAttempts = 0;
    busy = true; setControls();
    try {
      await loadStateAndRuntime();
      if (!blocked) setStatus("Состояние обновлено.");
    } catch (error) {
      setStatus(error instanceof TypeError ? "Нет связи с сервисом. История на экране сохранена." : error.message, "error");
    } finally { busy = false; setControls(); }
  });

  ui.prompt.addEventListener("input", setControls);
  ui.prompt.addEventListener("keydown", event => {
    if (event.key === "Enter" && event.ctrlKey) {
      event.preventDefault();
      if (!busy && !ui.send.disabled) ui.composer.requestSubmit();
    }
  });
  ui.composer.addEventListener("submit", async event => {
    event.preventDefault();
    const prompt = ui.prompt.value.trim();
    if (!connected || busy || blocked || !prompt) return;
    busy = true; setControls();
    setStatus("Модель готовит ответ. Это может занять время на CPU…", "busy");
    try {
      const { response, data } = await api("ask", { method: "POST", body: JSON.stringify({ prompt }) });
      if (data.state) renderState(data.state);
      if (response.status === 401) disconnect();
      if (!response.ok || data.status !== "ok") {
        setStatus(failureMessage(response, data), "error");
      } else {
        ui.prompt.value = "";
        setStatus("Ответ сохранён в общей истории.");
      }
    } catch {
      setStatus("Связь прервалась. Не отправляйте вопрос повторно, пока не проверите состояние и квитанции.", "error");
    } finally { busy = false; setControls(); }
  });
  setControls();
})();
