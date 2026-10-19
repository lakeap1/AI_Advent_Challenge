"use strict";

const $ = (id) => document.getElementById(id);
const state = { csrf: "", chats: [], chat: null, branch: null, profile: {}, memories: [],
  branches: [], busy: false, navigating: false, navigationVersion: 0, forkPending: false, explicitRefreshing: false,
  drawer: null, source: null, previousFocus: null, authGeneration: 0 };
const ACTIVE_KEY = "grani.activeBranch";

class StaleSessionError extends Error {}
const currentSession = (generation) => generation === state.authGeneration;
function requireSession(generation) {
  if (!currentSession(generation)) throw new StaleSessionError();
}
function resetSession(csrf = "") {
  state.authGeneration++;
  state.navigationVersion++;
  Object.assign(state, { csrf, chats: [], chat: null, branch: null, branches: [],
    profile: {}, memories: [], source: null, previousFocus: null,
    busy: false, navigating: false, forkPending: false, explicitRefreshing: false });
  closeDrawer();
  for (const id of ["conversation", "chat-list", "drawer-body"]) $(id).replaceChildren();
  $("chat-title").textContent = "";
  $("question").value = "";
  try { localStorage.removeItem(ACTIVE_KEY); } catch (_) { /* Storage may be unavailable. */ }
  $("login-form").querySelector("button").disabled = false;
  notice("");
  syncComposer();
  return state.authGeneration;
}

function remembered() {
  try { return JSON.parse(localStorage.getItem(ACTIVE_KEY) || "null"); }
  catch (_) { return null; }
}
function rememberActive(chatId, branchId) {
  try { localStorage.setItem(ACTIVE_KEY, JSON.stringify({ chatId, branchId })); }
  catch (_) { /* Private browser settings may disable storage. */ }
}

function node(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value !== undefined) item.textContent = String(value);
  return item;
}
function button(label, action, className = "") {
  const item = node("button", className, label);
  item.type = "button";
  item.addEventListener("click", action);
  return item;
}
function notice(message, error = false) {
  $("notice").textContent = message || "";
  $("notice").classList.toggle("error", error);
}
async function api(path, options = {}) {
  const generation = state.authGeneration;
  const method = options.method || "GET";
  const headers = { Accept: "application/json" };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = options.csrfToken ?? state.csrf;
  }
  let response;
  try {
    response = await fetch(path, { method, headers, credentials: "same-origin",
      body: method === "GET" ? undefined : JSON.stringify(options.data || {}) });
  } catch (_) {
    requireSession(generation);
    throw new Error("Нет соединения. Проверьте сеть и повторите.");
  }
  requireSession(generation);
  let data = {};
  try { data = await response.json(); } catch (_) { /* A proxy may return non-JSON. */ }
  requireSession(generation);
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new Error(data.error || "Не удалось выполнить действие. Повторите позже.");
  }
  return data;
}
function showLogin() {
  resetSession(state.csrf);
  $("app-view").hidden = true;
  closeDrawer();
  $("login-view").hidden = false;
  $("username").focus();
}
function showApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
}
function setBusy(value) {
  state.busy = value;
  syncComposer();
}
const mobileSidebar = window.matchMedia("(max-width: 900px)");
function syncSidebarMode() {
  const sidebar = $("sidebar");
  if (!mobileSidebar.matches) {
    sidebar.classList.remove("open");
    $("mobile-scrim").hidden = true;
    $("menu-toggle").setAttribute("aria-expanded", "false");
  }
  const closedMobile = mobileSidebar.matches && !sidebar.classList.contains("open");
  sidebar.inert = closedMobile;
  if (closedMobile) sidebar.setAttribute("aria-hidden", "true");
  else sidebar.removeAttribute("aria-hidden");
  if (closedMobile && sidebar.contains(document.activeElement)) $("menu-toggle").focus();
}
function syncComposer() {
  const disabled = state.busy || state.navigating || state.forkPending;
  $("send").disabled = disabled;
  $("question").disabled = disabled;
  $("compress").disabled = disabled;
}
function beginNavigation() {
  state.navigating = true;
  syncComposer();
  notice("Загружаем разговор…");
  return ++state.navigationVersion;
}
function endNavigation(version) {
  if (version !== state.navigationVersion) return false;
  state.navigating = false;
  syncComposer();
  return true;
}
function commitNavigation(version, chat, branches, branch) {
  if (version !== state.navigationVersion) return false;
  if (branch.chat_id !== chat.id || !branches.some(item => item.id === branch.id)) {
    throw new Error("Ветка не принадлежит выбранному разговору.");
  }
  state.chat = chat;
  state.branches = branches;
  state.branch = branch;
  rememberActive(chat.id, branch.id);
  $("chat-title").textContent = branch.title;
  renderChats();
  renderMessages();
  notice("");
  if (["branches", "conditions", "usage"].includes(state.drawer)) renderDrawer();
  endNavigation(version);
  return true;
}
function hideSidebar() {
  const wasOpen = $("sidebar").classList.contains("open");
  $("sidebar").classList.remove("open");
  $("mobile-scrim").hidden = true;
  $("menu-toggle").setAttribute("aria-expanded", "false");
  syncSidebarMode();
  if (wasOpen && mobileSidebar.matches) $("menu-toggle").focus();
}
function renderChats() {
  const list = $("chat-list");
  list.replaceChildren();
  if (!state.chats.length) list.append(node("p", "empty-note", "Пока нет разговоров"));
  for (const chat of state.chats) {
    const item = button(chat.title, () => selectChat(chat));
    item.className = "chat-item" + (state.chat?.id === chat.id ? " active" : "");
    list.append(item);
  }
}
async function selectChat(chat, branchId = null) {
  if (state.busy) return false;
  const generation = state.authGeneration;
  const version = beginNavigation();
  hideSidebar();
  try {
    const result = await api(`/api/chats/${encodeURIComponent(chat.id)}/branches`);
    if (!currentSession(generation) || version !== state.navigationVersion) return false;
    const branches = result.branches;
    const saved = remembered();
    const chosen = branchId || (saved?.chatId === chat.id && branches.some((item) => item.id === saved.branchId)
      ? saved.branchId : chat.branch_id);
    const branch = await api(`/api/branches/${encodeURIComponent(chosen)}`);
    if (!currentSession(generation)) return false;
    return commitNavigation(version, chat, branches, branch);
  } catch (error) {
    if (currentSession(generation) && endNavigation(version)) notice(error.message, true);
    return false;
  }
}
async function selectBranch(id, options = {}) {
  const generation = state.authGeneration;
  if (state.busy && !options.allowBusy) return false;
  const chat = state.chat;
  const branches = state.branches;
  if (!chat || !branches.some(item => item.id === id)) return false;
  const version = beginNavigation();
  try {
    const branch = await api(`/api/branches/${encodeURIComponent(id)}`);
    if (!currentSession(generation)) return false;
    return commitNavigation(version, chat, branches, branch);
  } catch (error) {
    if (currentSession(generation) && endNavigation(version)) notice(error.message, true);
    return false;
  }
}
function renderMessages() {
  const box = $("conversation");
  box.replaceChildren();
  const messages = state.branch?.messages || [];
  $("empty-state").hidden = messages.length > 0;
  box.hidden = messages.length === 0;
  for (const message of messages) {
    const article = node("article", `message ${message.role}`);
    article.append(node("div", "message-label", message.role === "user" ? "Вы" : "Грани"));
    article.append(node("div", "message-body", message.content));
    const actions = node("div", "message-actions");
    actions.append(button("Ответвить отсюда", () => forkAt(message.id)));
    if (message.role === "assistant" && Array.isArray(message.sources) && message.sources.length) {
      actions.append(button(`Источники · ${message.sources.length}`, () => openDrawer("sources", message.sources)));
    }
    article.append(actions);
    box.append(article);
  }
  box.scrollTop = box.scrollHeight;
}
async function createChat() {
  if (state.busy) return false;
  const generation = state.authGeneration;
  const version = beginNavigation();
  try {
    const chat = await api("/api/chats", { method: "POST" });
    if (!currentSession(generation)) return false;
    state.chats.unshift(chat);
    renderChats();
    if (version !== state.navigationVersion) return false;
    const selected = await selectChat(chat);
    if (!currentSession(generation)) return false;
    if (selected) $("question").focus();
    return selected;
  } catch (error) {
    if (currentSession(generation) && endNavigation(version)) notice(error.message, true);
    return false;
  }
}
async function forkAt(messageId) {
  if (!state.branch || state.busy || state.navigating || state.forkPending) return;
  const parentId = state.branch.id;
  const parentChatId = state.chat.id;
  const version = state.navigationVersion;
  const generation = state.authGeneration;
  state.forkPending = true;
  syncComposer();
  try {
    const branch = await api(`/api/branches/${encodeURIComponent(parentId)}/fork`,
      { method: "POST", data: { message_id: messageId } });
    if (!currentSession(generation) || version !== state.navigationVersion || state.chat?.id !== parentChatId || state.branch?.id !== parentId) return;
    state.branches.push({ id: branch.id, title: branch.title, revision: branch.revision });
    if (await selectBranch(branch.id) && currentSession(generation)) {
      notice("Создана ветка от выбранной реплики.");
      $("question").focus();
    }
  } catch (error) { if (currentSession(generation) && version === state.navigationVersion) notice(error.message, true); }
  finally { if (currentSession(generation)) { state.forkPending = false; syncComposer(); } }
}
async function sendQuestion(event) {
  event.preventDefault();
  const generation = state.authGeneration;
  const question = $("question").value.trim();
  if (!question || state.busy || state.navigating || state.forkPending) return;
  if (!state.branch) { if (!await createChat()) return; }
  if (!currentSession(generation)) return;
  const branchId = state.branch.id;
  setBusy(true);
  if (/^\/(remember|forget|profile)(?:\s|$)/.test(question)) {
    state.explicitRefreshing = true;
    if (["profile", "memory"].includes(state.drawer)) renderDrawer();
  }
  notice("Грани готовят ответ и проверяют источники…");
  $("empty-state").hidden = true;
  $("conversation").hidden = false;
  const pending = node("article", "message pending");
  pending.append(node("div", "message-label", "Грани"));
  pending.append(node("div", "message-body", "Ищем объяснение…"));
  $("conversation").append(pending);
  try {
    const result = await api(`/api/branches/${encodeURIComponent(branchId)}/ask`,
      { method: "POST", data: { question } });
    if (!currentSession(generation)) return;
    $("question").value = "";
    if (result.status === "updated" || state.explicitRefreshing) {
      state.explicitRefreshing = true;
      if (["profile", "memory"].includes(state.drawer)) renderDrawer();
      const bootstrap = await api("/api/bootstrap");
      if (!currentSession(generation)) return;
      state.profile = bootstrap.profile;
      state.memories = bootstrap.memories;
      state.explicitRefreshing = false;
      if (["profile", "memory"].includes(state.drawer)) renderDrawer();
    }
    await selectBranch(branchId, { allowBusy: true });
    if (!currentSession(generation)) return;
    if (result.status !== "accepted") notice(result.message || "Ответ не появился. Уточните вопрос.", result.status !== "updated");
  } catch (error) {
    if (!currentSession(generation)) return;
    pending.remove();
    notice(error.message, true);
  } finally { if (currentSession(generation)) { setBusy(false); $("question").focus(); } }
}
function closeDrawer() {
  $("drawer").hidden = true;
  $("drawer-scrim").hidden = true;
  state.drawer = null;
  state.source = null;
  state.previousFocus?.focus();
  state.previousFocus = null;
}
function openDrawer(kind, source = null) {
  state.previousFocus = document.activeElement;
  state.drawer = kind;
  state.source = source;
  $("drawer").hidden = false;
  $("drawer-scrim").hidden = false;
  renderDrawer();
  $("drawer-close").focus();
}
function textField(form, label, name, value = "", maxLength = 500) {
  const id = `field-${name}`;
  const caption = node("label", "", label);
  caption.htmlFor = id;
  const input = document.createElement("input");
  input.id = id; input.name = name; input.value = value || ""; input.maxLength = maxLength;
  form.append(caption, input);
  return input;
}
function addEmpty(parent, value) { parent.append(node("p", "empty-note", value)); }
function renderDrawer() {
  const title = { profile: "Профиль", memory: "Память", conditions: "Условия ветки",
    branches: "Ветки разговора", sources: "Источники", usage: "Учёт запросов" };
  $("drawer-title").textContent = title[state.drawer] || "Панель";
  const body = $("drawer-body"); body.replaceChildren();
  if (state.explicitRefreshing && ["profile", "memory"].includes(state.drawer)) {
    body.append(node("p", "", "Обновляем настройки. Если соединение прервано, перезагрузите страницу."));
    return;
  }
  if (state.drawer === "profile") renderProfile(body);
  if (state.drawer === "memory") renderMemory(body);
  if (state.drawer === "conditions") renderConditions(body);
  if (state.drawer === "branches") renderBranches(body);
  if (state.drawer === "sources") renderSources(body);
  if (state.drawer === "usage") renderUsage(body);
}
function renderProfile(body) {
  const generation = state.authGeneration;
  body.append(node("p", "", "Роль и уровень знаний задаются отдельно. Эти настройки действуют во всех разговорах."));
  const form = node("form");
  const role = textField(form, "Роль или профессия", "role", state.profile.role);
  const level = textField(form, "Уровень знаний", "level", state.profile.level);
  const goal = textField(form, "Учебная цель", "goal", state.profile.goal);
  const depth = textField(form, "Глубина объяснения", "depth", state.profile.depth);
  const submit = node("button", "primary", "Сохранить профиль"); submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!currentSession(generation)) return;
    try { const result = await api("/api/profile", { method: "PATCH", data: {
      role: role.value.trim(), level: level.value.trim(), goal: goal.value.trim(), depth: depth.value.trim() } });
      if (!currentSession(generation)) return;
      state.profile = result.profile; notice("Профиль сохранён."); closeDrawer();
    } catch (error) { if (currentSession(generation)) notice(error.message, true); }
  });
  body.append(form);
}
function renderMemory(body) {
  const generation = state.authGeneration;
  body.append(node("p", "", "Сохранённые сведения доступны во всех чатах. Вы можете исправить или забыть любое из них."));
  if (!state.memories.length) addEmpty(body, "Пока нет сохранённых сведений.");
  for (const memory of state.memories) {
    const card = node("div", "data-card");
    card.append(node("strong", "", memory.key), node("p", "", memory.value));
    const actions = node("div", "card-actions");
    actions.append(button("Изменить", () => { $("field-key").value = memory.key; $("field-value").value = memory.value; $("field-value").focus(); }));
    actions.append(button("Забыть", async () => {
      if (!currentSession(generation)) return;
      try { await api(`/api/memories/${encodeURIComponent(memory.id)}`, { method: "DELETE" });
        if (!currentSession(generation)) return;
        state.memories = state.memories.filter((item) => item.id !== memory.id);
        renderDrawer(); notice("Сведение удалено.");
      } catch (error) { if (currentSession(generation)) notice(error.message, true); }
    }));
    card.append(actions); body.append(card);
  }
  const form = node("form");
  form.append(node("h3", "section-title", "Добавить или исправить"));
  const key = textField(form, "Название сведения", "key", "", 80);
  const value = textField(form, "Значение", "value", "", 2000);
  key.required = true; value.required = true;
  const submit = node("button", "primary", "Сохранить"); submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!currentSession(generation)) return;
    try { const result = await api("/api/memories", { method: "POST", data: { key: key.value, value: value.value } });
      if (!currentSession(generation)) return;
      state.memories = state.memories.filter((item) => item.key !== result.key).concat(result);
      state.memories.sort((a, b) => a.key.localeCompare(b.key));
      renderDrawer(); notice("Сведение сохранено.");
    } catch (error) { if (currentSession(generation)) notice(error.message, true); }
  });
  body.append(form);
}
function renderConditions(body) {
  const generation = state.authGeneration;
  if (!state.branch) { addEmpty(body, "Сначала откройте разговор."); return; }
  body.append(node("p", "", "Условия относятся только к этой ветке. Например, укажите версию Unity и render pipeline."));
  const conditions = state.branch.conditions || {};
  for (const [key, value] of Object.entries(conditions)) {
    const card = node("div", "data-card");
    card.append(node("strong", "", key), node("p", "", value));
    card.append(button("Изменить", () => { $("field-condition-key").value = key; $("field-condition-value").value = value; $("field-condition-value").focus(); }));
    card.append(button("Удалить", async () => {
      if (!currentSession(generation)) return;
      const next = { ...conditions }; delete next[key]; await saveConditions(next);
    }));
    body.append(card);
  }
  if (!Object.keys(conditions).length) addEmpty(body, "Пока нет условий ветки.");
  const form = node("form"); form.append(node("h3", "section-title", "Условие"));
  const key = textField(form, "Название", "condition-key", "", 80);
  const value = textField(form, "Значение", "condition-value", "", 1000);
  key.required = true; value.required = true;
  const submit = node("button", "primary", "Сохранить условие"); submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (event) => { event.preventDefault();
    if (!currentSession(generation)) return;
    await saveConditions({ ...state.branch.conditions, [key.value.trim()]: value.value.trim() }); });
  body.append(form);
}
async function saveConditions(conditions) {
  const generation = state.authGeneration;
  const branchId = state.branch?.id;
  const version = state.navigationVersion;
  if (!branchId || state.navigating) return;
  try { const branch = await api(`/api/branches/${encodeURIComponent(branchId)}`,
    { method: "PATCH", data: { conditions } });
    if (!currentSession(generation) || version !== state.navigationVersion || state.branch?.id !== branchId) return;
    state.branch = branch; renderDrawer(); notice("Условия сохранены.");
  } catch (error) { if (currentSession(generation)) notice(error.message, true); }
}
function renderBranches(body) {
  const generation = state.authGeneration;
  if (!state.chat) { addEmpty(body, "Сначала откройте разговор."); return; }
  body.append(node("p", "", "Каждая ветка продолжает историю от выбранной реплики. Чтобы создать новую, используйте действие рядом с сообщением."));
  for (let index = 0; index < state.branches.length; index++) {
    const branch = state.branches[index];
    const item = button(`${index === 0 ? "Основная ветка" : `Ветка ${index + 1}`} · ${branch.title}`,
      async () => { if (currentSession(generation) && await selectBranch(branch.id) && currentSession(generation)) closeDrawer(); },
      "branch-item" + (state.branch?.id === branch.id ? " active" : ""));
    body.append(item);
  }
}
function safeUrl(value) {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : null; }
  catch (_) { return null; }
}
function renderSources(body) {
  const sources = state.source || [];
  if (!sources.length) { addEmpty(body, "У этого ответа нет показанных источников."); return; }
  body.append(node("p", "", "Фрагменты, которые использовались при подготовке ответа. Проверяйте версию и область применения."));
  for (const source of sources) {
    const card = node("article", "source-card");
    card.append(node("strong", "", source.title || "Источник"));
    card.append(node("small", "", [source.section, source.version, source.pipeline].filter(Boolean).join(" · ")));
    if (source.text) card.append(node("pre", "", source.text));
    const local = { "README.md": "readme", "docs/usage.md": "usage", "docs/architecture.md": "architecture",
      "docs/api.md": "api", "corpus/glossary.md": "glossary" }[source.path];
    if (local) { const link = node("a", "", "Открыть текст ↗"); link.href = `/reference/${local}`;
      link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link); }
    else { const url = safeUrl(source.url); if (url) { const link = node("a", "", "Открыть источник ↗");
      link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link); } }
    body.append(card);
  }
}
const STAGE_LABELS = { preparation: "Подготовка", summary: "Сводка истории",
  relevance: "Оценка релевантности", filter: "Отбор источников", embedding: "Поиск по базе",
  generator: "Генерация ответа", validator: "Смысловая проверка" };
const CALL_STATUS = { complete: "Завершён", rejected: "Отклонён", unknown: "Расход неизвестен",
  not_sent: "Не отправлен", pending: "В процессе" };
function tokenCount(value) { return Number.isInteger(value) && value >= 0 ? String(value) : "неизвестно"; }
function costAmount(value) { return typeof value === "number" && Number.isFinite(value)
  ? `$${value.toFixed(8)} USD` : "неизвестно"; }
async function renderUsage(body) {
  const generation = state.authGeneration;
  body.append(node("p", "", "Учёт включает все выполненные обращения, даже если ответ отклонён."));
  if (!state.chat) { addEmpty(body, "Создайте разговор, чтобы увидеть его расходы."); return; }
  const requestedChat = state.chat.id;
  const loading = node("p", "", "Загружаем учёт…"); body.append(loading);
  try {
    const ledger = await api(`/api/ledger?chat_id=${encodeURIComponent(requestedChat)}`);
    if (!currentSession(generation) || state.drawer !== "usage" || state.chat?.id !== requestedChat) return;
    loading.remove();
    const cost = ledger.cost_estimated_usd;
    body.append(node("div", "eyebrow", "Оценка расходов, USD"));
    body.append(node("div", "usage-number", cost === null ? "Неизвестно" : costAmount(cost)));
    body.append(node("p", "", `Известные токены · вход: ${tokenCount(ledger.input_tokens)} · выход: ${tokenCount(ledger.output_tokens)} · всего: ${tokenCount(ledger.total_tokens)}`));
    body.append(node("p", "tariff-note", "Оценка по обычному API-тарифу; фактическое списание bundle неизвестно."));
    if (!ledger.complete) {
      body.append(node("p", "empty-note", `Учёт неполон: часть расхода неизвестна. Известная часть: ${costAmount(ledger.known_cost_estimated_usd)}.`));
    }
    if (!ledger.calls.length) { addEmpty(body, "Запросов к моделям ещё не было."); return; }
    body.append(node("h3", "section-title", "Выполненные этапы"));
    for (const call of ledger.calls) {
      const card = node("article", "data-card usage-call");
      card.append(node("strong", "", STAGE_LABELS[call.stage] || "Вызов модели"));
      card.append(node("small", "", `${call.model || "Модель неизвестна"} · ${CALL_STATUS[call.status] || "Статус неизвестен"}`));
      const usage = call.usage || {};
      card.append(node("p", "", `Вход: ${tokenCount(usage.input_tokens)} · Выход: ${tokenCount(usage.output_tokens)} · Всего: ${tokenCount(usage.total_tokens)}`));
      if (Number.isInteger(usage.cached_input_tokens)) card.append(node("p", "", `Кэш входа: ${tokenCount(usage.cached_input_tokens)}`));
      if (Number.isInteger(usage.reasoning_tokens)) card.append(node("p", "", `Рассуждение: ${tokenCount(usage.reasoning_tokens)}`));
      card.append(node("p", "", `Стоимость: ${costAmount(call.cost_estimated_usd)}`));
      if (call.tariff_source || call.tariff_checked_on) {
        card.append(node("small", "", [call.tariff_source, call.tariff_checked_on].filter(Boolean).join(" · ")));
      }
      body.append(card);
    }
  } catch (error) {
    if (currentSession(generation) && state.drawer === "usage" && state.chat?.id === requestedChat) loading.textContent = error.message;
  }
}
async function initialize() {
  const generation = state.authGeneration;
  try { const csrf = await api("/api/csrf");
    if (!currentSession(generation)) return;
    state.csrf = csrf.csrf_token;
    const bootstrap = await api("/api/bootstrap");
    if (!currentSession(generation)) return;
    state.csrf = bootstrap.csrf_token;
    state.profile = bootstrap.profile; state.memories = bootstrap.memories;
    state.explicitRefreshing = false;
    state.chats = bootstrap.chats; showApp(); renderChats();
    const saved = remembered();
    if (state.chats.length) await selectChat(state.chats.find((item) => item.id === saved?.chatId) || state.chats[0]);
    else { $("conversation").hidden = true; $("empty-state").hidden = false; notice(""); }
  } catch (error) { if (currentSession(generation)) showLogin(); }
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("login-error").textContent = "";
  const generation = resetSession(state.csrf);
  const submit = $("login-form").querySelector("button"); submit.disabled = true;
  try { const login = await api("/api/login", { method: "POST", data: {
    username: $("username").value, password: $("password").value } });
    if (!currentSession(generation)) return;
    state.csrf = login.csrf_token; $("password").value = ""; await initialize();
  } catch (error) { if (currentSession(generation)) $("login-error").textContent = error.message; }
  finally { if (currentSession(generation)) submit.disabled = false; }
});
$("logout").addEventListener("click", async () => {
  const csrf = state.csrf;
  const generation = resetSession();
  $("app-view").hidden = true;
  $("login-view").hidden = true;
  try { await api("/api/logout", { method: "POST", csrfToken: csrf });
    if (!currentSession(generation)) return;
    await initialize();
  } catch (error) {
    if (currentSession(generation)) { showLogin(); $("login-error").textContent = error.message; }
  }
});
$("new-chat").addEventListener("click", createChat);
$("composer").addEventListener("submit", sendQuestion);
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); $("composer").requestSubmit();
  }
});
for (const prompt of document.querySelectorAll("[data-prompt]")) prompt.addEventListener("click", () => {
  $("question").value = prompt.dataset.prompt; $("question").focus();
});
for (const action of document.querySelectorAll("[data-drawer]")) action.addEventListener("click", () => openDrawer(action.dataset.drawer));
$("compress").addEventListener("click", async () => {
  if (!state.branch || state.busy || state.navigating) return;
  const branchId = state.branch.id;
  const generation = state.authGeneration;
  setBusy(true); notice("Сжимаем старую часть истории…");
  try { const result = await api(`/api/branches/${encodeURIComponent(branchId)}/compress`, { method: "POST" });
    if (!currentSession(generation)) return;
    if (await selectBranch(branchId, { allowBusy: true }) && currentSession(generation)) notice(result.message || "Сводка обновлена.");
  } catch (error) { if (currentSession(generation)) notice(error.message, true); }
  finally { if (currentSession(generation)) setBusy(false); }
});
$("drawer-close").addEventListener("click", closeDrawer);
$("drawer-scrim").addEventListener("click", closeDrawer);
$("menu-toggle").addEventListener("click", () => {
  $("sidebar").classList.add("open"); $("mobile-scrim").hidden = false;
  $("menu-toggle").setAttribute("aria-expanded", "true");
  syncSidebarMode();
  $("new-chat").focus();
});
$("mobile-scrim").addEventListener("click", hideSidebar);
mobileSidebar.addEventListener("change", syncSidebarMode);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { if (state.drawer) closeDrawer(); else hideSidebar(); }
  if (event.key === "Tab" && state.drawer) {
    const focusable = [...$("drawer").querySelectorAll("button,input,textarea,select,a[href]")].filter((el) => !el.disabled);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
syncSidebarMode();
initialize();
