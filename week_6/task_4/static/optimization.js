(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const ui = {
    form: $('optimization-form'), prompt: $('optimization-prompt'), preset: $('optimization-preset'),
    submit: $('optimization-submit'), refresh: $('optimization-refresh'), build: $('optimization-build'),
    waiting: $('optimization-waiting'), status: $('optimization-status'), presets: $('optimization-presets'),
    results: $('optimization-results'), sources: $('optimization-sources'), history: $('optimization-history'),
    ledger: $('optimization-ledger'), notice: $('optimization-notice'), benchmark: $('optimization-benchmark'),
    title: $('optimization-result-title'), meta: $('optimization-result-meta'), count: $('optimization-count')
  };
  const labels = { baseline: 'Исходный', optimized: 'Оптимизированный', quantized: 'Квантованный Q8' };
  let busy = false, blocked = false, stateEpoch = 0, history = [], receipts = [], ledger = null;
  function node(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined && value !== null) item.textContent = String(value);
    return item;
  }
  function put(parent, tag, className, value) {
    const item = node(tag, className, value);
    parent.append(item);
    return item;
  }
  function value(item, fallback = 'неизвестно') {
    return item === null || item === undefined || item === '' ? fallback : String(item);
  }
  function usd(item) {
    return typeof item === 'number' && Number.isFinite(item)
      ? `$${item === 0 ? '0' : item.toFixed(8).replace(/0+$/, '').replace(/\.$/, '')}` : 'неизвестно';
  }
  function number(item, digits = 1) {
    return typeof item === 'number' && Number.isFinite(item) ? item.toFixed(digits) : 'неизвестно';
  }
  function notice(message) {
    ui.notice.textContent = message || '';
    ui.notice.hidden = !message;
  }
  function controls() {
    ui.submit.disabled = busy || blocked;
    ui.build.disabled = busy || blocked;
    ui.refresh.disabled = busy;
    ui.prompt.disabled = busy;
    ui.preset.disabled = busy;
    ui.waiting.hidden = !busy;
  }
  function setBusy(on) { busy = on; if (on) stateEpoch++; controls(); }
  function renderStatus(state) {
    ui.status.replaceChildren();
    const index = state.index || {}, status = index.status || 'неизвестно';
    const row = node('div', 'rag-status-grid');
    const pill = (text, problem = false) => put(row, 'span', `rag-status-pill${problem ? ' problem' : ''}`, text);
    pill(`Индекс: ${status === 'ready' ? 'готов' : status === 'missing' ? 'отсутствует' : status === 'stale' ? 'устарел' : status}`, status !== 'ready');
    if (index.chunks !== undefined) pill(`Фрагментов: ${index.chunks}`);
    pill(`Выбран: ${labels[state.selected_preset] || value(state.selected_preset)}`);
    if (state.blocked) pill('Новые вызовы заблокированы: неполный receipt', true);
    ui.status.append(row);
    if (status === 'stale') put(ui.status, 'p', 'rag-muted', 'Корпус или модель изменились. Постройте индекс вручную.');
  }
  function renderPresets(presets) {
    ui.presets.replaceChildren();
    for (const name of ['baseline', 'optimized', 'quantized']) {
      const preset = presets?.[name];
      if (!preset) continue;
      const card = node('div', 'optimization-preset-card');
      put(card, 'strong', '', labels[name]);
      put(card, 'span', '', value(preset.model));
      put(card, 'small', '', `T ${value(preset.temperature)} · контекст ${value(preset.context_window)} · ответ до ${value(preset.max_output_tokens)} токенов`);
      ui.presets.append(card);
    }
  }
  function policyText(policy) {
    return policy && typeof policy === 'object' ? `${value(policy.status)}${policy.code ? ` (${policy.code})` : ''}` : 'неизвестно';
  }
  function receiptNode(receipt) {
    const details = node('details', 'rag-receipt');
    const title = receipt.stage === 'input_policy' ? 'Проверка входа' : labels[receipt.preset] || value(receipt.preset, 'Вызов');
    put(details, 'summary', '', `${title} · ${value(receipt.model)} · ${value(receipt.status)} · ${usd(receipt.cost_usd)}`);
    const body = node('div', 'rag-receipt-body'), usage = receipt.usage || {}, tariff = receipt.tariff || {};
    put(body, 'div', '', `Токены: вход ${value(usage.input_tokens)}, выход ${value(usage.output_tokens)}, всего ${value(usage.total_tokens)}.`);
    put(body, 'div', '', `Кэшированный вход: ${value(usage.cached_input_tokens)}; reasoning в выходе: ${value(usage.reasoning_output_tokens)}.`);
    put(body, 'div', '', `Учёт: ${value(receipt.usage_status)} · стоимость API: ${usd(receipt.cost_usd)} · время: ${number(receipt.elapsed_seconds, 3)} с.`);
    put(body, 'div', '', `Input policy: ${policyText(receipt.input_policy)}; output policy: ${policyText(receipt.output_policy)}.`);
    put(body, 'div', '', `Тариф: ${value(tariff.basis || tariff.model_mode)}; источник: ${value(tariff.price_source || tariff.source)}; проверен: ${value(tariff.price_checked_date || tariff.checked_at)}.`);
    put(body, 'div', '', `Стоимость инфраструктуры: ${usd(tariff.infrastructure_cost_usd)}.`);
    if (receipt.error_code) put(body, 'div', '', `Код отказа: ${receipt.error_code}.`);
    details.append(body);
    return details;
  }
  function renderLedger(next) {
    if (next) ledger = next;
    ui.ledger.replaceChildren();
    if (!ledger) return put(ui.ledger, 'p', 'rag-muted', 'Учёт пока недоступен.');
    put(ui.ledger, 'p', 'rag-ledger-total', ledger.incomplete ? 'Общая стоимость: неизвестна' : `Всего API: ${usd(ledger.total_usd)}`);
    put(ui.ledger, 'p', 'rag-muted', `Известная часть: ${usd(ledger.known_usd)} · записей: ${value(ledger.receipt_count)}.${ledger.incomplete ? ' Учёт неполный.' : ''}`);
    put(ui.ledger, 'p', 'rag-muted', 'Стоимость локального API и расход на оборудование показаны отдельно.');
    const list = node('div', 'rag-receipts');
    const recent = receipts.filter(item => item.stage === 'input_policy' ||
      (item.metered !== false && (item.stage === 'generation' || item.stage === 'query_embed'))).slice(0, 4);
    for (const item of recent) list.append(receiptNode(item));
    if (!recent.length) put(list, 'p', 'rag-muted', 'Вызовов по вопросам пока нет.');
    ui.ledger.append(list);
    if (receipts.length > recent.length) {
      const journal = node('details', 'rag-journal');
      put(journal, 'summary', '', `Журнал записей: ${receipts.length}`);
      const all = node('div', 'rag-journal-list');
      for (const item of receipts) all.append(receiptNode(item));
      journal.append(all);
      ui.ledger.append(journal);
    }
  }
  function safeHref(raw) {
    try { const url = new URL(raw); return ['https:', 'http:'].includes(url.protocol) ? url.href : null; }
    catch (_) { return null; }
  }
  function renderSources(retrieval) {
    ui.sources.replaceChildren();
    if (!Array.isArray(retrieval?.sources) || !retrieval.sources.length) {
      put(ui.sources, 'p', 'rag-muted', 'Подходящие фрагменты не найдены.');
      return;
    }
    for (const source of retrieval.sources) {
      const details = node('details', 'source-card');
      put(details, 'summary', '', `${value(source.label)} · ${value(source.title)}`);
      put(details, 'div', 'rag-source-meta', `${value(source.file)}:${value(source.line_start)}–${value(source.line_end)} · ${Array.isArray(source.section) ? source.section.join(' / ') : value(source.section)}`);
      put(details, 'div', 'rag-source-meta', `Chunk ID: ${value(source.chunk_id)} · score: ${value(source.score)}`);
      const href = safeHref(source.source);
      if (href) {
        const link = put(details, 'a', 'rag-source-link', `Открыть исходный материал: ${source.source}`);
        link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      } else put(details, 'div', 'rag-source-meta', `Источник: ${value(source.source)}`);
      put(details, 'div', 'rag-source-text', value(source.text, 'Фрагмент пуст.'));
      ui.sources.append(details);
    }
    put(ui.sources, 'p', 'rag-hash', `Context SHA-256: ${value(retrieval.context_sha256)}`);
    if (retrieval.index_id) put(ui.sources, 'p', 'rag-hash', `Index ID: ${retrieval.index_id}`);
  }
  function renderAnswers(item) {
    ui.results.replaceChildren();
    put(ui.results, 'p', 'rag-question', `Вопрос: ${value(item.question)}`);
    const grid = node('div', 'rag-answer-grid');
    if ((item.results || []).length > 1) grid.classList.add('compare');
    for (const result of item.results || []) {
      const card = node('article', 'answer-card');
      card.dataset.preset = result.preset || 'unknown';
      put(card, 'h3', '', `${labels[result.preset] || value(result.preset)} · ${value(result.model)}`);
      if (result.status === 'ok' && result.answer !== null) put(card, 'div', 'answer-text', result.answer);
      else put(card, 'p', 'answer-error', `${value(result.error?.message, 'Ответ не получен.')} (${value(result.error?.code, result.status)})`);
      const meta = node('div', 'rag-answer-meta'), params = result.parameters || {};
      put(meta, 'div', '', `Статус: ${value(result.status)} · Источники ответа: ${Array.isArray(result.source_labels) && result.source_labels.length ? result.source_labels.join(', ') : 'не указаны'}`);
      put(meta, 'div', '', `Запрос генерации: ${number(result.elapsed_seconds, 3)} с · Стоимость API: ${usd(result.receipt?.cost_usd)}`);
      if (typeof result.metrics?.eval_seconds === 'number') put(meta, 'div', '', `Генерация токенов: ${number(result.metrics.eval_seconds, 3)} с (Ollama eval).`);
      if (Object.keys(params).length) put(meta, 'div', '', `Параметры: T ${value(params.temperature)} · контекст ${value(params.context_window || params.num_ctx)} · ответ до ${value(params.max_output_tokens || params.num_predict)} токенов`);
      if (result.request_sha256) put(meta, 'div', 'rag-hash', `Request SHA-256: ${result.request_sha256}`);
      if (result.receipt) meta.append(receiptNode(result.receipt));
      card.append(meta); grid.append(card);
    }
    if (grid.childElementCount) ui.results.append(grid);
    else put(ui.results, 'p', 'rag-empty', 'Ответ не получен. Проверьте статус и состояние источников.');
    ui.meta.textContent = item.status === 'ok' ? 'Готово' : `Статус: ${value(item.status)}`;
    renderSources(item.retrieval);
    notice(item.error ? `${value(item.error.message)} (${value(item.error.code)})` : '');
    ui.title.focus({ preventScroll: true });
  }
  function renderHistory() {
    ui.history.replaceChildren();
    if (!history.length) return put(ui.history, 'p', 'rag-muted', 'Сохранённых вопросов пока нет.');
    for (const item of history) {
      const button = node('button', 'rag-history-item'); button.type = 'button';
      put(button, 'span', '', value(item.question));
      put(button, 'small', '', `${value(item.created_at, 'Только что')} · ${value(item.status)}`);
      button.addEventListener('click', () => renderAnswers(item)); ui.history.append(button);
    }
  }
  function metric(data, key, digits = 1) { return number(data?.[key]?.median, digits); }
  function renderBenchmark(benchmark) {
    ui.benchmark.replaceChildren();
    if (benchmark?.kind !== 'saved' || !benchmark.report) {
      put(ui.benchmark, 'p', 'rag-muted', 'Сохранённого прогона пока нет. Сравнение выше запускается по вашему вопросу.'); return;
    }
    const report = benchmark.report;
    put(ui.benchmark, 'p', 'rag-muted', `Сохранённый прогон: ${value(report.created_at)} · выбран ${labels[report.selected_preset] || value(report.selected_preset)}. Эти числа не относятся к текущему вопросу.`);
    const wrap = node('div', 'optimization-table-scroll'), table = node('table', 'optimization-table');
    const head = node('thead'), row = node('tr');
    for (const title of ['Режим', 'Качество', 'Запрос, медиана', 'Токенов/с', 'VRAM модели', 'GPU пик, медиана']) put(row, 'th', '', title);
    head.append(row); table.append(head);
    const body = node('tbody');
    for (const [name, data] of Object.entries(report.presets || {})) {
      const tr = node('tr'), quality = report.quality?.[name];
      put(tr, 'th', '', labels[name] || name);
      put(tr, 'td', '', typeof quality?.correct_facts === 'number' && typeof quality.total_facts === 'number' ? `${quality.correct_facts} / ${quality.total_facts} фактов` : 'неизвестно');
      put(tr, 'td', '', `${metric(data, 'wall_seconds')} с`);
      put(tr, 'td', '', metric(data, 'tokens_per_second'));
      put(tr, 'td', '', `${number(typeof data?.resident_vram_bytes?.median === 'number' ? data.resident_vram_bytes.median / 1048576 : null, 0)} МиБ`);
      put(tr, 'td', '', `${metric(data, 'sampled_gpu_peak_mib', 0)} МиБ`);
      body.append(tr);
    }
    table.append(body); wrap.append(table); ui.benchmark.append(wrap);
    put(ui.benchmark, 'p', 'rag-muted', 'GPU пик — медиана наибольших наблюдений каждого запроса при выборочном мониторинге, не точный максимум или энергопотребление.');
    for (const item of report.limitations || []) put(ui.benchmark, 'p', 'rag-muted', item);
  }
  function applyState(state) {
    blocked = Boolean(state.blocked || state.ledger?.incomplete);
    renderStatus(state); renderPresets(state.presets);
    if (state.selected_preset && ['baseline', 'optimized', 'quantized'].includes(state.selected_preset)) ui.preset.value = state.selected_preset;
    history = Array.isArray(state.history) ? state.history : [];
    receipts = Array.isArray(state.receipts) ? state.receipts : [];
    renderHistory(); renderLedger(state.ledger); renderBenchmark(state.benchmark); controls();
  }
  async function request(url, options) {
    const response = await fetch(url, options), body = await response.json();
    if (!body || typeof body !== 'object') throw new Error('Сервер вернул неверный ответ.');
    return body;
  }
  async function post(url, body) {
    return request(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }
  async function refresh() {
    if (busy) return;
    const epoch = ++stateEpoch; ui.refresh.disabled = true;
    try { const state = await request('/api/optimization/state'); if (epoch === stateEpoch && !busy) applyState(state); }
    catch (_) { if (epoch === stateEpoch) notice('Не удалось получить состояние. Проверьте соединение и обновите вручную.'); }
    finally { if (epoch === stateEpoch) ui.refresh.disabled = busy; }
  }
  async function synchronizeAfterAsk(epoch) {
    const state = await request('/api/optimization/state');
    if (epoch !== stateEpoch || !busy) return;
    blocked = Boolean(blocked || state.blocked || state.ledger?.incomplete);
    receipts = Array.isArray(state.receipts) ? state.receipts : receipts;
    history = Array.isArray(state.history) ? state.history : history;
    renderLedger(state.ledger);
    renderHistory();
    renderStatus({ ...state, blocked });
    controls();
  }
  async function submit(event) {
    event.preventDefault(); if (busy || blocked) return;
    const prompt = ui.prompt.value.trim();
    if (!prompt || prompt.length > 2000) { notice('Введите вопрос длиной от 1 до 2000 символов.'); ui.prompt.focus(); return; }
    notice(''); setBusy(true);
    const askEpoch = stateEpoch;
    try {
      const item = await post('/api/optimization/ask', { prompt, preset: ui.preset.value });
      renderAnswers(item);
      if (item.ledger) {
        blocked = Boolean(item.ledger.incomplete);
        for (const receipt of (item.results || []).map(result => result.receipt).filter(Boolean)) {
          if (!receipts.some(saved => saved.id === receipt.id)) receipts.unshift(receipt);
        }
        renderLedger(item.ledger);
      }
      if (item.retrieval) { history.unshift(item); renderHistory(); }
      if (blocked) notice(`${item.error ? item.error.message + ' ' : ''}Учёт неполный; новые вызовы заблокированы.`);
      try { await synchronizeAfterAsk(askEpoch); }
      catch (_) {
        blocked = true;
        notice(`${item.error ? item.error.message + ' ' : ''}Журнал вызовов не удалось обновить. Новые вызовы остановлены до ручного обновления состояния.`);
      }
    } catch (_) {
      blocked = true;
      notice('Не удалось получить ответ сервера. Состояние вызова неизвестно; новые вызовы остановлены. Обновите состояние вручную.');
    } finally { setBusy(false); }
  }
  async function build() {
    if (busy || blocked) return;
    notice(''); setBusy(true);
    try {
      const outcome = await post('/api/optimization/build', {});
      if (outcome.ledger) { blocked = Boolean(outcome.ledger.incomplete); renderLedger(outcome.ledger); }
      if (outcome.index) renderStatus({ index: outcome.index, selected_preset: ui.preset.value, blocked });
      notice(outcome.error ? `${value(outcome.error.message)} (${value(outcome.error.code)})` : 'Индекс построен. Обновите состояние для полного журнала.');
    } catch (_) {
      blocked = true;
      notice('Не удалось получить результат построения индекса. Новые вызовы остановлены до ручного обновления состояния.');
    } finally { setBusy(false); }
  }
  ui.form.addEventListener('submit', submit);
  ui.build.addEventListener('click', build);
  ui.refresh.addEventListener('click', refresh);
  ui.prompt.addEventListener('input', () => { ui.count.textContent = `${ui.prompt.value.length} / 2000`; });
  controls(); refresh();
})();
