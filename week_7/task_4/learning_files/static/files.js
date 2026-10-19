"use strict";

/* The base app.js executes first. Its api, authGeneration and branch state are shared. */
(() => {
  const byId = (id) => document.getElementById(id);
  const make = (tag, className = "", value) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = String(value);
    return item;
  };
  const action = (label, callback, className = "") => {
    const item = make("button", className, label);
    item.type = "button";
    item.addEventListener("click", callback);
    return item;
  };
  const current = (generation, branchId, opening) =>
    generation === state.authGeneration && branchId === state.branch?.id &&
    opening === panelGeneration;

  const launch = action("Материалы", openPanel, "files-launch");
  launch.id = "files-launch";
  byId("compress").before(launch);

  const shade = make("div", "files-shade");
  shade.id = "files-shade";
  shade.hidden = true;
  const panel = make("aside", "files-panel");
  panel.id = "files-panel";
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "files-title");
  const header = make("header", "files-header");
  const heading = make("div");
  heading.append(make("span", "eyebrow", "Личное пространство"));
  const title = make("h2", "", "Материалы");
  title.id = "files-title";
  heading.append(title);
  const close = action("×", closePanel, "files-close");
  close.id = "files-close";
  close.setAttribute("aria-label", "Закрыть материалы");
  header.append(heading, close);
  const body = make("div", "files-body");
  const intro = make("p", "files-intro", "Ассистент читает несколько ваших учебных файлов, предлагает изменения и сохраняет их после подтверждения.");
  const seed = action("Добавить учебные примеры", seedExamples);
  seed.id = "files-seed";
  const exercise = make("p", "files-exercise", "Примеры созданы для упражнения. В двух заметках о normal map есть намеренное противоречие.");
  const inventoryTitle = make("h3", "", "Мои файлы");
  const inventory = make("div", "files-list");
  inventory.id = "files-inventory";
  const readTitle = make("h3", "", "Открытый файл");
  const readback = make("pre", "files-readback");
  readback.id = "files-readback";
  readback.tabIndex = -1;
  const form = make("form", "files-form");
  form.id = "files-form";
  const label = make("label", "", "Учебная цель");
  label.htmlFor = "files-goal";
  const goal = make("textarea");
  goal.id = "files-goal";
  goal.rows = 4;
  goal.maxLength = 4000;
  goal.required = true;
  goal.placeholder = "Сделай памятку по материалу, шейдеру и текстуре…";
  const prepare = make("button", "primary", "Подготовить предложение");
  prepare.id = "files-prepare";
  prepare.type = "submit";
  form.append(label, goal, prepare);
  const status = make("p", "files-status");
  status.id = "files-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const result = make("div", "files-result");
  result.id = "files-result";
  const usage = make("p", "files-usage");
  usage.id = "files-usage";
  body.append(intro, seed, exercise, inventoryTitle, inventory, readTitle, readback,
              form, status, result, usage);
  panel.append(header, body);
  document.body.append(shade, panel);

  let priorFocus = null;
  let activeBranch = null;
  let pending = false;
  let proposal = null;
  let panelGeneration = 0;
  const RECOVERY_KEY = "grani.learningFiles.recovery";
  let recoveryMarker = null;
  let references = new Map();
  function savedReferences() {
    const marker = state.csrf.slice(0, 16);
    if (recoveryMarker !== marker) {
      recoveryMarker = marker;
      references = new Map();
      try {
        const stored = JSON.parse(sessionStorage.getItem(RECOVERY_KEY) || "null");
        if (stored?.marker === marker && Array.isArray(stored.refs))
          for (const ref of stored.refs) {
            if (ref && typeof ref.branch_id === "string" && typeof ref.request_id === "string" &&
                typeof ref.goal === "string" && typeof ref.phase === "string")
              references.set(ref.branch_id, ref);
          }
      } catch (_) { /* Private browsing may disable session storage. */ }
    }
    return references;
  }
  function saveReference(ref) {
    savedReferences().set(ref.branch_id, ref);
    try { sessionStorage.setItem(RECOVERY_KEY, JSON.stringify({marker: recoveryMarker,
      refs: [...references.values()]})); } catch (_) { /* In-memory recovery still works. */ }
  }
  function resetRecovery() {
    references.clear();
    recoveryMarker = null;
    try { sessionStorage.removeItem(RECOVERY_KEY); } catch (_) { /* Storage may be unavailable. */ }
  }
  function rememberRun(run) {
    if (!run?.id || !run?.request_id || !run?.branch_id) return;
    saveReference({branch_id: run.branch_id, request_id: run.request_id,
      goal: run.goal, run_id: run.id,
      phase: ["working", "pending_publish"].includes(run.status) ? "prepare" : "run"});
  }
  function clearPrivate() {
    panelGeneration++;
    proposal = null;
    activeBranch = null;
    goal.value = "";
    inventory.replaceChildren();
    readback.textContent = "";
    result.replaceChildren();
    usage.textContent = "";
    status.textContent = "";
    pending = false;
    prepare.disabled = false;
    seed.disabled = false;
  }
  function setStatus(message, error = false) {
    status.textContent = message || "";
    status.classList.toggle("error", error);
  }
  function closePanel() {
    panel.hidden = true;
    shade.hidden = true;
    clearPrivate();
    if (priorFocus?.isConnected && !byId("app-view").hidden) priorFocus.focus();
    priorFocus = null;
  }
  async function openPanel() {
    if (byId("app-view").hidden || !state.branch) return;
    clearPrivate();
    activeBranch = state.branch.id;
    priorFocus = document.activeElement;
    panel.hidden = false;
    shade.hidden = false;
    close.focus();
    const generation = state.authGeneration, branchId = activeBranch, opening = panelGeneration;
    await refreshInventory();
    if (current(generation, branchId, opening) && !panel.hidden)
      await recoverPanel();
  }
  async function refreshInventory() {
    if (!activeBranch) return;
    const generation = state.authGeneration, branchId = activeBranch, opening = panelGeneration;
    try {
      const response = await api("/api/learning-files/inventory");
      if (!current(generation, branchId, opening) || panel.hidden) return;
      inventory.replaceChildren();
      if (!response.files.length) inventory.append(make("p", "", "Пока нет файлов. Добавьте примеры."));
      for (const file of response.files) {
        inventory.append(action(`${file.path} · ${file.bytes} Б`, () => openFile(file.path)));
      }
    } catch (error) {
      if (current(generation, branchId, opening) && !panel.hidden) setStatus(error.message, true);
    }
  }
  function showUncertain(ref) {
    if (!goal.value.trim()) goal.value = ref.goal;
    proposal = null;
    result.replaceChildren();
    setStatus("Исход прежней подготовки неизвестен. Продолжите тот же запрос; новый платный запуск не создаётся.", true);
    result.append(action("Продолжить тот же запрос", () => sendPrepare(ref)));
  }
  async function recoverPanel(allowPending = false) {
    const generation = state.authGeneration, branchId = activeBranch, opening = panelGeneration;
    const ref = savedReferences().get(branchId);
    const query = new URLSearchParams({branch_id: branchId});
    if (ref) query.set("request_id", ref.request_id);
    try {
      const response = await api(`/api/learning-files/recovery?${query}`);
      if (!current(generation, branchId, opening) || panel.hidden || (pending && !allowPending) ||
          savedReferences().get(branchId)?.request_id !== ref?.request_id) return;
      if (response.run) {
        if (!goal.value.trim()) goal.value = response.run.goal;
        renderRun(response.run);
        if (response.run.status === "applied" && response.run.saved?.length)
          await openFile(response.run.saved[0].path);
      } else if (ref?.phase === "prepare") showUncertain(ref);
    } catch (error) {
      if (current(generation, branchId, opening) && !panel.hidden && ref?.phase === "prepare")
        showUncertain(ref);
      else if (current(generation, branchId, opening) && !panel.hidden && ref?.phase === "apply") {
        setStatus("Исход сохранения неизвестен. Проверьте состояние того же предложения.", true);
        result.replaceChildren(action("Проверить сохранение", () => refreshRun(ref.run_id)));
      }
    }
  }
  async function openFile(path) {
    const generation = state.authGeneration, branchId = activeBranch, opening = panelGeneration;
    try {
      const file = await api(`/api/learning-files/file?path=${encodeURIComponent(path)}`);
      if (!current(generation, branchId, opening) || panel.hidden) return;
      readback.textContent = `${file.path}\n\n${file.content}`;
      readback.focus();
    } catch (error) {
      if (current(generation, branchId, opening) && !panel.hidden) setStatus(error.message, true);
    }
  }
  async function seedExamples() {
    if (pending || !activeBranch) return;
    const generation = state.authGeneration, branchId = activeBranch, opening = panelGeneration;
    pending = true; seed.disabled = true;
    setStatus("Добавляем примеры…");
    try {
      const response = await api("/api/learning-files/examples", {method: "POST"});
      if (!current(generation, branchId, opening) || panel.hidden) return;
      setStatus(response.added.length ? `Добавлено файлов: ${response.added.length}.` : "Примеры уже есть; ваши файлы сохранены.");
      await refreshInventory();
    } catch (error) {
      if (current(generation, branchId, opening) && !panel.hidden) setStatus(error.message, true);
    } finally {
      if (current(generation, branchId, opening)) { pending = false; seed.disabled = false; }
    }
  }
  function renderRun(run) {
    rememberRun(run);
    proposal = run.status === "validated" ? run : null;
    result.replaceChildren();
    const statusText = {
      validated: "Предложение готово. Изучите diff перед сохранением.",
      applied: "Файлы сохранены и перечитаны.",
      pending_publish: "Подтверждение предложения ещё не завершено. Файлы не изменены.",
      conflict: "Файлы изменились после подготовки. Создайте новое предложение.",
      partial: "Сохранена только часть файлов. Проверьте список ниже.",
      rejected: "Предложение отклонено проверкой.",
      error: "Не удалось подготовить предложение.",
      working: "Подготовка ещё идёт. Проверьте состояние позже."
    };
    if (run.message) setStatus(run.message, run.status !== "validated" && run.status !== "applied");
    else setStatus(statusText[run.status] || "Состояние предложения пока неизвестно.");
    if (run.summary) result.append(make("p", "files-summary", run.summary));
    if (run.sources?.length) {
      result.append(make("h3", "", "Прочитанные источники"));
      for (const source of run.sources) result.append(action(source.path, () => openFile(source.path)));
    }
    if (run.changes?.length) {
      result.append(make("h3", "", "Предложенные изменения"));
      for (const change of run.changes) {
        const section = make("section", "files-change");
        section.append(make("h4", "", change.path));
        section.append(make("pre", "files-diff", change.diff));
        const details = make("details");
        details.append(make("summary", "", "Полный новый текст"));
        details.append(make("pre", "files-preview", change.content));
        section.append(details);
        result.append(section);
      }
    }
    if (run.status === "validated") {
      const confirm = action("Сохранить эти изменения", applyProposal, "primary files-apply");
      confirm.id = "files-apply";
      result.append(confirm);
    }
    if (["working", "pending_publish"].includes(run.status))
      result.append(action("Проверить состояние", () => refreshRun(run.id)));
    if (run.saved?.length) {
      result.append(make("h3", "", "Сохранённые файлы"));
      for (const item of run.saved) result.append(action(item.path, () => openFile(item.path)));
    }
  }
  async function refreshRun(runId) {
    const generation = state.authGeneration, branchId = activeBranch, opening = panelGeneration;
    try {
      const run = await api(`/api/learning-files/runs/${encodeURIComponent(runId)}`);
      if (!current(generation, branchId, opening) || panel.hidden || run.branch_id !== branchId) return null;
      renderRun(run);
      if (run.status === "applied" && run.saved?.length) await openFile(run.saved[0].path);
      return run;
    } catch (error) {
      if (current(generation, branchId, opening) && !panel.hidden) setStatus(error.message, true);
      return null;
    }
  }
  async function refreshUsage(generation, branchId, opening, chatId) {
    try {
      const ledger = await api(`/api/ledger?chat_id=${encodeURIComponent(chatId)}`);
      if (!current(generation, branchId, opening) || panel.hidden) return;
      const cost = ledger.complete ? `${ledger.cost_estimated_usd} USD` :
        `известная часть ${ledger.known_cost_estimated_usd} USD; общий расход неизвестен`;
      usage.textContent = `Общий учёт чата: ${ledger.input_tokens ?? "?"} входных, ${ledger.output_tokens ?? "?"} выходных, ${ledger.total_tokens ?? "?"} всего; ${cost}.`;
    } catch (_) { /* The proposal status already reports the outcome. */ }
  }
  async function submitGoal(event) {
    event.preventDefault();
    if (pending || !activeBranch || !goal.value.trim()) return;
    const previous = savedReferences().get(activeBranch);
    if (previous?.phase === "prepare") {
      if (goal.value.trim() !== previous.goal) {
        setStatus("Сначала проверьте прежний запрос; новая цель пока не отправлена.", true);
        return;
      }
      await sendPrepare(previous);
      return;
    }
    if (previous?.phase === "apply") {
      await refreshRun(previous.run_id);
      return;
    }
    const ref = {branch_id: activeBranch, goal: goal.value.trim(),
      request_id: crypto.randomUUID(), phase: "prepare"};
    saveReference(ref);
    await sendPrepare(ref);
  }
  async function sendPrepare(ref) {
    if (pending || !activeBranch || ref.branch_id !== activeBranch) return;
    const generation = state.authGeneration, branchId = activeBranch, opening = panelGeneration;
    const chatId = state.branch.chat_id;
    pending = true; prepare.disabled = true;
    proposal = null; result.replaceChildren(); setStatus("Подбираем и проверяем файлы…");
    try {
      const run = await api("/api/learning-files/prepare", {method: "POST", data: {
        branch_id: branchId, goal: ref.goal, request_id: ref.request_id }});
      if (!current(generation, branchId, opening) || panel.hidden) return;
      renderRun(run);
      await refreshUsage(generation, branchId, opening, chatId);
      if (current(generation, branchId, opening) && !state.busy && !state.navigating)
        await selectBranch(branchId, {allowBusy: true});
    } catch (error) {
      if (current(generation, branchId, opening) && !panel.hidden) {
        setStatus(error.message, true);
        await recoverPanel(true);
      }
    } finally {
      if (current(generation, branchId, opening)) { pending = false; prepare.disabled = false; }
    }
  }
  async function applyProposal() {
    if (pending || !proposal || !activeBranch || proposal.branch_id !== activeBranch) return;
    const generation = state.authGeneration, branchId = activeBranch, opening = panelGeneration;
    const runId = proposal.id;
    saveReference({...savedReferences().get(branchId), branch_id: branchId,
      request_id: proposal.request_id, goal: proposal.goal, run_id: runId, phase: "apply"});
    pending = true;
    const confirm = byId("files-apply");
    if (confirm) confirm.disabled = true;
    setStatus("Сохраняем и перечитываем файлы…");
    try {
      const run = await api(`/api/learning-files/runs/${encodeURIComponent(runId)}/apply`, {
        method: "POST", data: {branch_id: branchId}});
      if (!current(generation, branchId, opening) || panel.hidden) return;
      renderRun(run);
      await refreshInventory();
      if (current(generation, branchId, opening) && !panel.hidden &&
          run.status === "applied" && run.saved?.length) await openFile(run.saved[0].path);
    } catch (error) {
      if (current(generation, branchId, opening) && !panel.hidden) {
        const recovered = await refreshRun(runId);
        if (!recovered && current(generation, branchId, opening) && !panel.hidden) {
          setStatus("Исход сохранения неизвестен. Проверьте состояние того же предложения.", true);
          result.replaceChildren(action("Проверить сохранение", () => refreshRun(runId)));
        }
      }
    } finally {
      if (current(generation, branchId, opening)) pending = false;
    }
  }

  form.addEventListener("submit", submitGoal);
  shade.addEventListener("click", closePanel);
  byId("logout").addEventListener("click", () => { closePanel(); resetRecovery(); }, true);
  byId("login-form").addEventListener("submit", () => { closePanel(); resetRecovery(); }, true);
  new MutationObserver(() => {
    if (byId("app-view").hidden) resetRecovery();
    if (panel.hidden) return;
    if (byId("app-view").hidden || state.branch?.id !== activeBranch) closePanel();
  }).observe(byId("app-view"), {attributes: true, attributeFilter: ["hidden"], subtree: true, childList: true});
  document.addEventListener("keydown", (event) => {
    if (panel.hidden) return;
    if (event.key === "Escape") { event.preventDefault(); closePanel(); return; }
    if (event.key !== "Tab") return;
    const focusable = [...panel.querySelectorAll("button:not([disabled]),textarea:not([disabled])")]
      .filter((item) => item.getClientRects().length);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }, true);
})();
