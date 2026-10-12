(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const ui = {
    form: $('rag-form'), prompt: $('rag-prompt'), mode: $('rag-mode'), submit: $('rag-submit'),
    build: $('rag-build'), refresh: $('rag-refresh'), waiting: $('rag-waiting'),
    status: $('rag-status'), results: $('rag-results'), sources: $('rag-sources'),
    history: $('rag-history'), ledger: $('rag-ledger'), notice: $('rag-notice'),
    resultTitle: $('rag-result-title'), resultMeta: $('rag-result-meta'), count: $('rag-count')
  };
  let busy = false;
  let blocked = false;
  let cloudAvailable = false;
  let stateEpoch = 0;
  let history = [];
  let globalReceipts = [];
  let latestLedger = null;

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
  }
  function appendText(parent, tag, className, value) {
    const node = element(tag, className, value);
    parent.append(node);
    return node;
  }
  function text(value, fallback = 'неизвестно') {
    return value === null || value === undefined || value === '' ? fallback : String(value);
  }
  function usd(value) {
    return typeof value === 'number' && Number.isFinite(value)
      ? `$${value === 0 ? '0' : value.toFixed(8).replace(/0+$/, '').replace(/\.$/, '')}`
      : 'неизвестно';
  }
  function seconds(value) {
    return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(3)} с` : 'неизвестно';
  }
  function setNotice(message) {
    ui.notice.textContent = message || '';
    ui.notice.hidden = !message;
  }
  function updateControls() {
    const cloudChoice = ui.mode.querySelector('option[value="cloud"]');
    const compareChoice = ui.mode.querySelector('option[value="compare"]');
    cloudChoice.disabled = !cloudAvailable;
    compareChoice.disabled = !cloudAvailable;
    if (!cloudAvailable && ui.mode.value !== 'local') ui.mode.value = 'local';
    ui.submit.disabled = busy || blocked;
    ui.build.disabled = busy || blocked;
    ui.refresh.disabled = busy;
    ui.mode.disabled = busy;
    ui.prompt.disabled = busy;
    ui.waiting.hidden = !busy;
  }
  function setBusy(value) {
    busy = value;
    if (value) stateEpoch++;
    updateControls();
  }
  function addPill(parent, value, problem = false) {
    const node = appendText(parent, 'span', 'rag-status-pill' + (problem ? ' problem' : ''), value);
    return node;
  }
  function renderStatus(state) {
    ui.status.replaceChildren();
    const row = element('div', 'rag-status-grid');
    const index = state.index || {};
    const indexStatus = index.status || 'неизвестно';
    addPill(row, `Индекс: ${indexStatus === 'ready' ? 'готов' : indexStatus === 'stale' ? 'устарел' : indexStatus === 'missing' ? 'отсутствует' : indexStatus}`, indexStatus !== 'ready');
    if (index.chunks !== undefined) addPill(row, `Фрагментов: ${index.chunks}`);
    addPill(row, `Облако: ${state.cloud_available ? 'доступно по выбору' : 'ключ не настроен'}`);
    if (state.blocked) addPill(row, 'Новые вызовы заблокированы: неполный receipt', true);
    ui.status.append(row);
    const runtime = state.runtime || {};
    appendText(ui.status, 'p', 'rag-muted', `Embedding: ${text(runtime.embedding_model || index.model)} · Генератор: ${text(runtime.generation_model || index.generation_model)}.`);
    appendText(ui.status, 'p', 'rag-muted', 'Проверка доступности Ollama не выполнялась при чтении состояния.');
    if (indexStatus === 'stale') appendText(ui.status, 'p', 'rag-muted', 'Корпус или модель изменились. Постройте индекс вручную.');
  }
  function policyText(value) {
    if (!value || typeof value !== 'object') return 'неизвестно';
    return [text(value.status), value.code ? `(${value.code})` : ''].filter(Boolean).join(' ');
  }
  function receiptNode(receipt) {
    const details = element('details', 'rag-receipt');
    appendText(details, 'summary', '', `${text(receipt.stage)} · ${text(receipt.provider)} · ${text(receipt.model)} · ${text(receipt.status)} · ${usd(receipt.cost_usd)}`);
    const body = element('div', 'rag-receipt-body');
    const usage = receipt.usage || {};
    appendText(body, 'div', '', `Токены: вход ${text(usage.input_tokens)}, выход ${text(usage.output_tokens)}, всего ${text(usage.total_tokens)}.`);
    appendText(body, 'div', '', `Кэшированный вход: ${text(usage.cached_input_tokens)}; запись кэша: ${text(usage.cache_write_input_tokens)}; reasoning в выходе: ${text(usage.reasoning_output_tokens)}.`);
    appendText(body, 'div', '', `Учёт: ${text(receipt.usage_status)}; стоимость API: ${usd(receipt.cost_usd)}; время вызова: ${seconds(receipt.elapsed_seconds)}.`);
    appendText(body, 'div', '', `Input policy: ${policyText(receipt.input_policy)}; output policy: ${policyText(receipt.output_policy)}.`);
    const tariff = receipt.tariff || {};
    if (tariff.model_mode === 'local') {
      appendText(body, 'div', '', `${text(tariff.basis)} Стоимость инфраструктуры не измерялась.`);
    } else if (tariff.model_mode === 'cloud') {
      appendText(body, 'div', '', `Тариф: ${text(tariff.service_tier)}; проверен ${text(tariff.price_checked_date)}; источник: ${text(tariff.price_source)}.`);
      appendText(body, 'div', '', `USD / млн: вход ${text(tariff.input_usd_per_million)}, кэш ${text(tariff.cached_input_usd_per_million)}, запись кэша ${text(tariff.cache_write_usd_per_million)}, выход ${text(tariff.output_usd_per_million)}.`);
    }
    if (receipt.error_code) appendText(body, 'div', '', `Код отказа: ${receipt.error_code}.`);
    details.append(body);
    return details;
  }
  function renderLedger(value) {
    latestLedger = value || latestLedger;
    ui.ledger.replaceChildren();
    const ledger = latestLedger;
    if (!ledger) {
      appendText(ui.ledger, 'p', 'rag-muted', 'Учёт пока недоступен.');
      return;
    }
    appendText(ui.ledger, 'p', 'rag-ledger-total', ledger.incomplete ? 'Общая стоимость: неизвестна' : `Всего API: ${usd(ledger.total_usd)}`);
    appendText(ui.ledger, 'p', 'rag-muted', `Известная часть: ${usd(ledger.known_usd)} · записей: ${text(ledger.receipt_count)}.${ledger.incomplete ? ' Учёт неполный; новые вызовы могут быть заблокированы.' : ''}`);
    appendText(ui.ledger, 'p', 'rag-muted', 'Последние вызовы по вопросам');
    const list = element('div', 'rag-receipts');
    const recent = globalReceipts.filter(receipt => receipt.metered !== false &&
      (receipt.stage === 'query_embed' || receipt.stage === 'generation')).slice(0, 4);
    for (const receipt of recent) list.append(receiptNode(receipt));
    if (!recent.length) appendText(list, 'p', 'rag-muted', 'Вызовов по вопросам пока нет.');
    ui.ledger.append(list);
    if (globalReceipts.length) {
      const journal = element('details', 'rag-journal');
      journal.id = 'rag-receipt-journal';
      appendText(journal, 'summary', '', `Журнал доступных записей: ${globalReceipts.length} из ${text(ledger.receipt_count)}`);
      const journalList = element('div', 'rag-journal-list');
      for (const receipt of globalReceipts) journalList.append(receiptNode(receipt));
      journal.append(journalList);
      ui.ledger.append(journal);
    }
  }
  function safeSourceLink(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
      return url.href;
    } catch (_) { return null; }
  }
  function renderSources(retrieval) {
    ui.sources.replaceChildren();
    if (!retrieval || !Array.isArray(retrieval.sources) || !retrieval.sources.length) {
      appendText(ui.sources, 'p', 'rag-muted', 'Подходящие фрагменты не найдены.');
      return;
    }
    for (const source of retrieval.sources) {
      const details = element('details', 'source-card');
      appendText(details, 'summary', '', `${text(source.label)} · ${text(source.title)}`);
      appendText(details, 'div', 'rag-source-meta', `${text(source.file)}:${text(source.line_start)}–${text(source.line_end)} · ${Array.isArray(source.section) ? source.section.join(' / ') : text(source.section)}`);
      appendText(details, 'div', 'rag-source-meta', `Chunk ID: ${text(source.chunk_id)} · score: ${text(source.score)}`);
      const href = safeSourceLink(source.source);
      if (href) {
        const link = appendText(details, 'a', 'rag-source-link', `Открыть исходный материал: ${source.source}`);
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
      } else appendText(details, 'div', 'rag-source-meta', `Источник: ${text(source.source)}`);
      appendText(details, 'div', 'rag-source-text', text(source.text, 'Фрагмент пуст.'));
      ui.sources.append(details);
    }
    appendText(ui.sources, 'p', 'rag-muted', `Ранжирование фрагментов: ${seconds(retrieval.elapsed_seconds)}. Это время только математического поиска, без embedding и полного запроса.`);
    appendText(ui.sources, 'p', 'rag-hash', `Index ID: ${text(retrieval.index_id)}`);
    appendText(ui.sources, 'p', 'rag-hash', `Context SHA-256: ${text(retrieval.context_sha256)}`);
    if (retrieval.request_sha256) appendText(ui.sources, 'p', 'rag-hash', `Request SHA-256: ${retrieval.request_sha256}`);
  }
  function renderAnswers(item) {
    ui.results.replaceChildren();
    appendText(ui.results, 'p', 'rag-question', `Вопрос: ${text(item.question)}`);
    const answers = element('div', 'rag-answer-grid');
    if ((item.results || []).length > 1) answers.classList.add('compare');
    for (const result of item.results || []) {
      const card = element('article', 'answer-card');
      card.dataset.provider = result.provider || 'unknown';
      appendText(card, 'h3', '', `${result.provider === 'local' ? 'Локальная модель' : result.provider === 'cloud' ? 'Облачная модель' : text(result.provider)} · ${text(result.model)}`);
      if (result.status === 'ok' && result.answer !== null) appendText(card, 'div', 'answer-text', result.answer);
      else appendText(card, 'p', 'answer-error', `${text(result.error?.message, 'Ответ не получен.')} (${text(result.error?.code, text(result.status))})`);
      const meta = element('div', 'rag-answer-meta');
      appendText(meta, 'div', '', `Статус: ${text(result.status)} · Источники ответа: ${Array.isArray(result.source_labels) && result.source_labels.length ? result.source_labels.join(', ') : 'не указаны'}`);
      appendText(meta, 'div', '', `Генерация: ${seconds(result.elapsed_seconds)} · Стоимость API: ${usd(result.receipt?.cost_usd)}`);
      if (result.request_sha256) appendText(meta, 'div', 'rag-hash', `Request SHA-256: ${result.request_sha256}`);
      if (result.receipt) meta.append(receiptNode(result.receipt));
      card.append(meta);
      answers.append(card);
    }
    if (!answers.childElementCount) appendText(ui.results, 'p', 'rag-empty', 'Ответ не был получен. Смотрите причину выше и состояние источников.');
    else ui.results.append(answers);
    ui.resultMeta.textContent = item.status === 'ok' ? 'Готово' : `Статус: ${text(item.status)}`;
    renderSources(item.retrieval);
    setNotice(item.error ? `${text(item.error.message)} (${text(item.error.code)})` : '');
    ui.resultTitle.focus({ preventScroll: true });
  }
  function renderHistory() {
    ui.history.replaceChildren();
    if (!history.length) {
      appendText(ui.history, 'p', 'rag-muted', 'Сохранённых вопросов пока нет.');
      return;
    }
    for (const item of history) {
      const button = element('button', 'rag-history-item');
      button.type = 'button';
      appendText(button, 'span', '', text(item.question));
      appendText(button, 'small', '', `${text(item.created_at, 'Только что')} · ${text(item.status)}`);
      button.addEventListener('click', () => {
        renderAnswers(item);
      });
      ui.history.append(button);
    }
  }
  function applyState(state) {
    cloudAvailable = Boolean(state.cloud_available);
    blocked = Boolean(state.blocked);
    renderStatus(state);
    history = Array.isArray(state.history) ? state.history : [];
    renderHistory();
    globalReceipts = Array.isArray(state.receipts) ? state.receipts : [];
    renderLedger(state.ledger);
    updateControls();
  }
  async function request(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!body || typeof body !== 'object') throw new Error('Сервер вернул неверный ответ.');
    return body;
  }
  async function refreshState() {
    if (busy) return;
    const epoch = ++stateEpoch;
    ui.refresh.disabled = true;
    try {
      const state = await request('/api/local-rag/state');
      if (epoch === stateEpoch && !busy) applyState(state);
    } catch (_) {
      if (epoch === stateEpoch) setNotice('Не удалось получить состояние. Проверьте соединение и обновите вручную.');
    } finally {
      if (epoch === stateEpoch) ui.refresh.disabled = busy;
    }
  }
  async function post(url, body) {
    return request(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }
  async function submit(event) {
    event.preventDefault();
    if (busy || blocked) return;
    const prompt = ui.prompt.value.trim();
    if (!prompt || prompt.length > 2000) {
      setNotice('Введите вопрос длиной от 1 до 2000 символов.');
      ui.prompt.focus();
      return;
    }
    setNotice('');
    setBusy(true);
    try {
      const item = await post('/api/local-rag/ask', { prompt, mode: ui.mode.value });
      renderAnswers(item);
      if (item.ledger) {
        blocked = Boolean(item.ledger.incomplete);
        for (const receipt of (item.results || []).map(result => result.receipt).filter(Boolean)) {
          if (!globalReceipts.some(saved => saved.id === receipt.id)) globalReceipts.unshift(receipt);
        }
        renderLedger(item.ledger);
      }
      if (item.retrieval) {
        history.unshift(item);
        renderHistory();
      }
      if (blocked) setNotice(`${item.error ? item.error.message + ' ' : ''}Учёт неполный; новые вызовы заблокированы. Обновите состояние для подробностей.`);
    } catch (_) {
      blocked = true;
      setNotice('Не удалось получить ответ сервера. Состояние вызова неизвестно; новые вызовы остановлены. Обновите состояние вручную.');
    }
    finally { setBusy(false); }
  }
  async function buildIndex() {
    if (busy || blocked) return;
    setNotice('');
    setBusy(true);
    try {
      const outcome = await post('/api/local-rag/build', {});
      if (outcome.ledger) {
        blocked = Boolean(outcome.ledger.incomplete);
        renderLedger(outcome.ledger);
      }
      if (outcome.index) renderStatus({ index: outcome.index, runtime: {}, cloud_available: cloudAvailable, blocked });
      setNotice(outcome.error ? `${text(outcome.error.message)} (${text(outcome.error.code)})` : 'Индекс построен. Обновите состояние для списка вызовов.');
    } catch (_) {
      blocked = true;
      setNotice('Не удалось получить результат построения индекса. Новые вызовы остановлены до ручного обновления состояния.');
    }
    finally { setBusy(false); }
  }
  ui.form.addEventListener('submit', submit);
  ui.build.addEventListener('click', buildIndex);
  ui.refresh.addEventListener('click', refreshState);
  ui.prompt.addEventListener('input', () => { ui.count.textContent = `${ui.prompt.value.length} / 2000`; });
  updateControls();
  refreshState();
})();
