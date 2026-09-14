'use strict';

const form = document.querySelector('#ask-form');
const promptInput = document.querySelector('#prompt');
const sendButton = document.querySelector('#send');
const sendLabel = document.querySelector('#send-label');
const modeSelect = document.querySelector('#context-mode');
const panel = document.querySelector('#answer-panel');
const conversation = document.querySelector('#conversation');
const notice = document.querySelector('#notice');
const badge = document.querySelector('#badge');
const waiting = document.querySelector('#waiting');
const restoreButton = document.querySelector('#restore');
const branchPanel = document.querySelector('#branch-panel');
const checkpointForm = document.querySelector('#checkpoint-form');
const branchForm = document.querySelector('#branch-form');
const switchForm = document.querySelector('#switch-form');
const checkpointSelect = document.querySelector('#checkpoint-select');
const branchSelect = document.querySelector('#branch-select');
const suggestions = [...document.querySelectorAll('.suggestion')];
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const integerFormat = new Intl.NumberFormat('ru-RU');
const decimalFormat = new Intl.NumberFormat('en-US', {minimumFractionDigits: 6, maximumFractionDigits: 8});

const modeCopy = {
  sliding: {
    title: 'Окно из последних 6 реплик',
    description: 'Агент отправляет модели только последние шесть отдельных сообщений, включая текущий вопрос. Старые сообщения остаются в архиве, но не входят в новый запрос.',
  },
  facts: {
    title: 'Закреплённые факты + окно',
    description: 'Перед ответом отдельный вызов обновляет словарь фактов. Затем агент отправляет актуальные факты и последние шесть реплик.',
  },
  branching: {
    title: 'Полный путь активной ветки',
    description: 'Каждая ветка получает общий префикс checkpoint и своё продолжение. При переключении восстанавливается полный путь выбранной ветки.',
  },
};

const statusCopy = {
  ok: 'Готово',
  rejected: 'Отклонено',
  error: 'Ошибка',
  pending: 'Выполняется',
  interrupted: 'Прервано',
  accepted: 'пройдена',
  not_checked: 'не выполнялась',
};

let busy = false;
let ready = false;
let currentState = null;
let previewTimer = null;
let previewVersion = 0;
let previewController = null;

function element(tag, className = '', text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function integer(value) {
  return value === null || value === undefined || !Number.isFinite(Number(value))
    ? 'неизвестно'
    : integerFormat.format(Number(value));
}

function cost(value) {
  return value === null || value === undefined || !Number.isFinite(Number(value))
    ? 'неизвестна'
    : decimalFormat.format(Number(value)) + ' USD';
}

function modeName(mode) {
  return modeCopy[mode]?.title ?? String(mode || 'Неизвестный режим');
}

function policyStatus(policy) {
  if (!policy || typeof policy !== 'object') return 'нет данных';
  const status = policy.status ?? 'нет данных';
  return statusCopy[status] ?? String(status);
}

function requestKind(request) {
  return request?.metadata?.kind === 'facts' ? 'Обновление facts' : 'Ответ пользователю';
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const raw = await response.text();
  let body;
  try {
    body = raw ? JSON.parse(raw) : {};
  } catch {
    throw new Error('Сервер вернул ответ в неожиданном формате.');
  }
  return {response, body};
}

function errorText(body, fallback) {
  if (body && typeof body.text === 'string' && body.text) return body.text;
  if (body && typeof body.error === 'string' && body.error) return body.error;
  if (body && typeof body.message === 'string' && body.message) return body.message;
  return fallback;
}

function syncControls() {
  modeSelect.disabled = busy;
  restoreButton.disabled = busy;
  promptInput.disabled = busy;
  suggestions.forEach(button => { button.disabled = busy; });
  sendButton.disabled = busy || !ready || !promptInput.value.trim();

  const branching = modeSelect.value === 'branching';
  for (const control of branchPanel.querySelectorAll('input, select, button')) {
    control.disabled = busy || !ready || !branching;
  }
  document.querySelector('#create-checkpoint').disabled = busy || !ready || !branching || !document.querySelector('#checkpoint-name').value.trim();
  document.querySelector('#create-branch').disabled = busy || !ready || !branching || !checkpointSelect.value || !document.querySelector('#branch-name').value.trim();
  document.querySelector('#switch-branch').disabled = busy || !ready || !branching || !branchSelect.value || String(currentState?.active_branch ?? '') === branchSelect.value;
  sendLabel.textContent = busy ? 'Операция выполняется…' : 'Отправить агенту';
}

function setBusy(value, label = '') {
  busy = value;
  panel.setAttribute('aria-busy', String(value));
  waiting.hidden = !value;
  if (value) {
    clearTimeout(previewTimer);
    previewController?.abort();
    badge.textContent = label || 'В работе';
  }
  syncControls();
}

function setNotice(text, kind = 'normal') {
  notice.textContent = text;
  panel.classList.toggle('error', kind === 'error');
}

function usageCard(request) {
  const card = element('section', 'request-usage');
  card.setAttribute('aria-label', 'Статистика: ' + requestKind(request));
  const heading = element('div', 'usage-heading');
  heading.append(
    element('span', 'operation-label', requestKind(request)),
    element('span', 'request-status status-' + String(request.status || 'error'), statusCopy[request.status] ?? request.status ?? 'Неизвестно'),
  );
  card.append(heading);

  const noCall = request.usage_status === 'not_requested';
  const usage = request.usage;
  const grid = element('dl', 'usage-grid');
  const values = [
    ['Input', noCall ? 'нет вызова' : integer(usage?.input_tokens)],
    ['Output', noCall ? '—' : integer(usage?.output_tokens)],
    ['Всего', noCall ? '—' : integer(usage?.total_tokens)],
    ['Стоимость', noCall ? 'нет вызова' : cost(request.cost_usd)],
  ];
  for (const [title, value] of values) {
    const item = element('div');
    item.append(element('dt', '', title), element('dd', '', value));
    grid.append(item);
  }
  card.append(grid);

  const details = noCall
    ? 'LLM не вызывалась.'
    : 'Cached input: ' + integer(usage?.cached_input_tokens) +
      ' · Reasoning в output: ' + integer(usage?.reasoning_tokens) +
      ' · usage: ' + String(request.usage_status ?? 'неизвестно');
  card.append(element('p', 'usage-details', details));
  card.append(element('p', 'usage-details', 'Input policy: ' + policyStatus(request.input_policy) + ' · Output policy: ' + policyStatus(request.output_policy)));

  const context = request.metadata?.context;
  if (request.metadata?.kind === 'answer' && context && typeof context === 'object') {
    const ids = Array.isArray(context.sent_message_ids) ? context.sent_message_ids.join(', ') : 'нет данных';
    const count = context.actual_messages ?? context.sent_messages;
    card.append(element('p', 'usage-details context-line', 'В контекст ответа вошло сообщений: ' + integer(count) + ' · ID: ' + ids + '.'));
  }
  return card;
}

function renderConversation(state, animateRequestId = null) {
  const fragment = document.createDocumentFragment();
  const requests = Array.isArray(state.requests) ? state.requests : [];
  const requestMap = new Map(requests.map(request => [String(request.id), request]));
  const childrenMap = new Map();
  for (const request of requests) {
    const parentId = request.metadata?.parent_request_id;
    if (parentId === null || parentId === undefined) continue;
    const key = String(parentId);
    const list = childrenMap.get(key) ?? [];
    list.push(request);
    childrenMap.set(key, list);
  }

  const turns = [];
  for (const message of state.messages) {
    const key = String(message.request_id);
    let turn = turns.at(-1);
    if (!turn || turn.key !== key) {
      turn = {key, messages: []};
      turns.push(turn);
    }
    turn.messages.push(message);
  }

  for (const turnData of turns) {
    const turn = element('article', 'turn');
    turn.dataset.requestId = turnData.key;
    for (const message of turnData.messages) {
      const role = message.role === 'assistant' ? 'assistant' : 'user';
      const item = element('div', 'message ' + role);
      if (String(animateRequestId) === turnData.key && role === 'assistant') item.classList.add('new-message');
      item.append(
        element('p', 'message-role', role === 'assistant' ? 'АГЕНТ' : 'ВЫ'),
        element('div', 'message-text', message.content ?? ''),
      );
      turn.append(item);
    }

    const related = [...(childrenMap.get(turnData.key) ?? [])];
    const answerRequest = requestMap.get(turnData.key);
    if (answerRequest) related.push(answerRequest);
    related.sort((a, b) => (a.metadata?.kind === 'facts' ? -1 : 1) - (b.metadata?.kind === 'facts' ? -1 : 1));
    for (const request of related) {
      if (request.status !== 'ok' && request.text) turn.append(element('div', 'request-error', request.text));
      turn.append(usageCard(request));
    }
    fragment.append(turn);
  }

  conversation.replaceChildren(fragment);
  document.querySelector('#empty').hidden = turns.length > 0;
  if (animateRequestId !== null) {
    requestAnimationFrame(() => conversation.scrollTo({
      top: conversation.scrollHeight,
      behavior: reducedMotion.matches ? 'auto' : 'smooth',
    }));
  }
}

function renderAccounting(state) {
  const summary = state.summary;
  const tokens = state.token_accounting;
  document.querySelector('#chat-cost').textContent = cost(summary.known_cost_usd);
  document.querySelector('#api-requests').textContent = integer(summary.api_requests);
  document.querySelector('#chat-tokens').textContent = integer(tokens.known_total_tokens) + (tokens.complete ? '' : ' + ?');
  document.querySelector('#cost-note').textContent = summary.cost_complete
    ? 'Стоимость полная по сохранённым вызовам.'
    : 'Показана известная часть: стоимость ' + integer(summary.unknown_cost_requests) + ' вызовов неизвестна.';
  document.querySelector('#accounting-note').textContent = 'Input: ' + integer(tokens.known_input_tokens) +
    ' · Output: ' + integer(tokens.known_output_tokens) +
    (tokens.complete ? '.' : ' · неизвестных usage: ' + integer(tokens.unknown_requests) + '.');
}

function renderMemory(state) {
  const mode = state.memory?.mode ?? modeSelect.value;
  const copy = modeCopy[mode] ?? {title: modeName(mode), description: ''};
  document.querySelector('#memory-mode').textContent = copy.title;
  document.querySelector('#memory-explanation').textContent = copy.description;
  document.querySelector('#mode-description').textContent = copy.description;
  document.querySelector('#path-message-count').textContent = integer(state.messages.length);
  document.querySelector('#history-tokens').textContent = '≈ ' + integer(state.context?.history_tokens_estimate);
  document.querySelector('#input-limit').textContent = integer(state.context?.max_input_tokens);

  const answerRequests = state.requests.filter(request => request.metadata?.kind === 'answer');
  const lastContext = answerRequests.at(-1)?.metadata?.context;
  const sentIds = Array.isArray(lastContext?.sent_message_ids) ? lastContext.sent_message_ids.join(', ') : '';
  const sentCount = lastContext?.actual_messages ?? lastContext?.sent_messages;
  document.querySelector('#sent-context').textContent = lastContext
    ? 'Последний ответ: отправлено сообщений ' + integer(sentCount) + (sentIds ? ' · ID ' + sentIds : '') + '. Оценка текста контекста: ≈ ' + integer(lastContext.input_text_tokens_estimate ?? lastContext.tokens_estimate) + ' токенов.'
    : 'После первого ответа здесь появится фактический состав отправленного контекста.';

  const facts = state.memory?.facts && typeof state.memory.facts === 'object' ? state.memory.facts : {};
  const factEntries = Object.entries(facts);
  const factsList = document.querySelector('#facts-list');
  factsList.replaceChildren(...factEntries.map(([key, value]) => {
    const row = element('div');
    row.append(element('dt', '', key), element('dd', '', value));
    return row;
  }));
  document.querySelector('#facts-empty').hidden = factEntries.length > 0;
  document.querySelector('#facts-revisions').textContent = integer(state.memory?.revisions ?? 0) + ' обновлений';
}

function option(value, text) {
  const node = element('option', '', text);
  node.value = String(value);
  return node;
}

function renderBranches(state) {
  const branching = modeSelect.value === 'branching';
  branchPanel.hidden = !branching;
  const checkpoints = Array.isArray(state.checkpoints) ? state.checkpoints : [];
  const branches = Array.isArray(state.branches) ? state.branches : [];

  const previousCheckpoint = checkpointSelect.value;
  checkpointSelect.replaceChildren(...checkpoints.map(item => option(item.id, item.name + ' · #' + item.id)));
  if (checkpoints.some(item => String(item.id) === previousCheckpoint)) checkpointSelect.value = previousCheckpoint;

  branchSelect.replaceChildren(...branches.map(item => option(item.id, item.name + (String(item.id) === String(state.active_branch) ? ' · активна' : ''))));
  if (branches.some(item => String(item.id) === String(state.active_branch))) branchSelect.value = String(state.active_branch);

  const checkpointList = document.querySelector('#checkpoint-list');
  checkpointList.replaceChildren(...checkpoints.map(item => element('li', '', item.name + ' · #' + item.id)));
  if (!checkpoints.length) checkpointList.append(element('li', 'empty-copy', 'Checkpoint пока нет.'));

  const branchList = document.querySelector('#branch-list');
  branchList.replaceChildren(...branches.map(item => element('li', String(item.id) === String(state.active_branch) ? 'active-item' : '', item.name + (String(item.id) === String(state.active_branch) ? ' · активна' : ''))));
  if (!branches.length) branchList.append(element('li', 'empty-copy', 'Ветки пока не созданы.'));
  const active = branches.find(item => String(item.id) === String(state.active_branch));
  document.querySelector('#active-branch').textContent = active ? 'Сейчас открыт путь «' + active.name + '».' : 'Активная ветка не определена.';
}

function renderRequests(requests) {
  const rows = document.createDocumentFragment();
  for (const request of requests) {
    const noCall = request.usage_status === 'not_requested';
    const usage = request.usage;
    const row = element('tr', request.status === 'ok' ? '' : 'problem-row');
    const values = [
      request.id,
      requestKind(request),
      statusCopy[request.status] ?? request.status,
      noCall ? 'нет вызова' : integer(usage?.input_tokens),
      noCall ? '—' : integer(usage?.output_tokens),
      noCall ? '—' : integer(usage?.total_tokens),
      noCall ? '—' : integer(usage?.cached_input_tokens),
      noCall ? '—' : integer(usage?.reasoning_tokens),
      noCall ? 'нет вызова' : cost(request.cost_usd),
      'вход: ' + policyStatus(request.input_policy) + ' · выход: ' + policyStatus(request.output_policy),
    ];
    for (const value of values) row.append(element('td', '', value ?? '—'));
    rows.append(row);
  }
  document.querySelector('#request-rows').replaceChildren(rows);
  document.querySelector('#requests-empty').hidden = requests.length > 0;
}

function renderState(state, animateRequestId = null) {
  if (!state || !Array.isArray(state.messages) || !Array.isArray(state.requests) ||
      !state.summary || !state.token_accounting || !state.memory || !state.context) {
    throw new Error('Сервер вернул неполное состояние чата.');
  }
  currentState = state;
  if (modeCopy[state.memory.mode]) modeSelect.value = state.memory.mode;
  renderConversation(state, animateRequestId);
  renderAccounting(state);
  renderMemory(state);
  renderBranches(state);
  renderRequests(state.requests);
  document.querySelector('#memory-status').textContent = state.messages.length
    ? 'История восстановлена · сообщений в пути: ' + integer(state.messages.length)
    : 'В этом режиме история пока пуста';
  document.querySelector('#session-info').textContent = 'Чат ' + String(state.chat_id).slice(0, 10) + ' · ' + (state.context.model ?? 'модель не указана') + ' · порт 5010';
  syncControls();
}

async function loadState() {
  if (busy) return;
  setBusy(true, 'Загрузка');
  setNotice('');
  ready = false;
  try {
    const {response, body} = await fetchJson('/api/state?mode=' + encodeURIComponent(modeSelect.value), {cache: 'no-store'});
    if (!response.ok) throw new Error(errorText(body, 'Не удалось загрузить сохранённый чат.'));
    renderState(body);
    ready = true;
    badge.textContent = 'Можно продолжать';
  } catch (error) {
    currentState = null;
    badge.textContent = 'Нет соединения';
    document.querySelector('#memory-status').textContent = 'Состояние не загружено';
    setNotice(error instanceof TypeError
      ? 'Сервер недоступен. Запустите приложение на порту 5010 и нажмите «Обновить».'
      : error.message, 'error');
  } finally {
    setBusy(false);
    syncControls();
    schedulePreview();
  }
}

async function mutation(url, payload, workingLabel, successText, afterSuccess) {
  if (busy || !ready) return;
  setBusy(true, workingLabel);
  setNotice('');
  try {
    const {response, body} = await fetchJson(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (body.state) renderState(body.state, body.request_id ?? null);
    if (!response.ok || body.status !== 'ok') {
      throw new Error(errorText(body, 'Операция не выполнена.'));
    }
    if (typeof afterSuccess === 'function') afterSuccess(body);
    badge.textContent = 'Сохранено';
    setNotice(successText);
  } catch (error) {
    badge.textContent = 'Операция не выполнена';
    setNotice(error instanceof TypeError
      ? 'Соединение прервалось. Результат мог сохраниться: нажмите «Обновить» перед повтором.'
      : error.message, 'error');
    if (error instanceof TypeError) ready = false;
  } finally {
    setBusy(false);
    syncControls();
    schedulePreview();
  }
}

function updateCount() {
  document.querySelector('#count').textContent = integer(Array.from(promptInput.value).length) + ' символов';
  syncControls();
  schedulePreview();
}

function previewText(metrics) {
  const labels = {
    new_message_tokens_estimate: 'новый вопрос',
    history_tokens_estimate: 'история',
    facts_tokens_estimate: 'facts',
    instructions_tokens_estimate: 'инструкции',
    input_text_tokens_estimate: 'весь текст входа',
    context_tokens_estimate: 'контекст',
  };
  const parts = [];
  for (const [key, label] of Object.entries(labels)) {
    if (metrics[key] !== null && metrics[key] !== undefined) parts.push(label + ': ≈ ' + integer(metrics[key]));
  }
  return parts.length ? parts.join(' · ') + '. Точный расход появится после ответа API.' : 'Оценка получена, но сервер не передал числовые метрики.';
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewController?.abort();
  const target = document.querySelector('#token-preview');
  const text = promptInput.value.trim();
  if (!text) {
    target.textContent = 'Введите сообщение для оценки контекста.';
    return;
  }
  if (!ready || busy) return;
  const version = ++previewVersion;
  target.textContent = 'Оцениваем текст контекста…';
  previewTimer = setTimeout(async () => {
    previewController = new AbortController();
    try {
      const {response, body} = await fetchJson('/api/preview', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode: modeSelect.value, prompt: promptInput.value}),
        signal: previewController.signal,
      });
      if (version !== previewVersion) return;
      target.textContent = response.ok && body.status === 'ok' && body.token_metrics
        ? previewText(body.token_metrics)
        : errorText(body, 'Оценка недоступна.');
    } catch (error) {
      if (error.name !== 'AbortError' && version === previewVersion) target.textContent = 'Оценка сейчас недоступна.';
    }
  }, 400);
}

form.addEventListener('submit', event => {
  event.preventDefault();
  const text = promptInput.value.trim();
  if (!text) return;
  mutation('/api/ask', {mode: modeSelect.value, prompt: text}, 'Агент отвечает', 'Ответ и расход сохранены.', body => {
    if (body.status === 'ok') {
      promptInput.value = '';
      updateCount();
    }
  });
});

promptInput.addEventListener('input', updateCount);
suggestions.forEach(button => button.addEventListener('click', () => {
  if (busy) return;
  promptInput.value = button.dataset.prompt ?? '';
  updateCount();
  promptInput.focus();
}));

modeSelect.addEventListener('change', () => {
  if (busy) return;
  ready = false;
  branchPanel.hidden = modeSelect.value !== 'branching';
  loadState();
});
restoreButton.addEventListener('click', loadState);

document.querySelector('#checkpoint-name').addEventListener('input', syncControls);
document.querySelector('#branch-name').addEventListener('input', syncControls);
checkpointSelect.addEventListener('change', syncControls);
branchSelect.addEventListener('change', syncControls);

checkpointForm.addEventListener('submit', event => {
  event.preventDefault();
  const input = document.querySelector('#checkpoint-name');
  const name = input.value.trim();
  if (!name) return;
  mutation('/api/checkpoint', {mode: 'branching', name}, 'Сохраняем checkpoint', 'Checkpoint «' + name + '» сохранён.', () => {
    input.value = '';
  });
});

branchForm.addEventListener('submit', event => {
  event.preventDefault();
  const input = document.querySelector('#branch-name');
  const name = input.value.trim();
  if (!name || !checkpointSelect.value) return;
  mutation('/api/branch', {
    mode: 'branching',
    checkpoint_id: Number(checkpointSelect.value),
    name,
  }, 'Создаём ветку', 'Ветка «' + name + '» создана и открыта.', () => {
    input.value = '';
  });
});

switchForm.addEventListener('submit', event => {
  event.preventDefault();
  if (!branchSelect.value) return;
  mutation('/api/switch', {
    mode: 'branching',
    branch_id: Number(branchSelect.value),
  }, 'Открываем ветку', 'Путь ветки восстановлен.');
});

function comparisonMetric(label, value) {
  const item = element('div');
  item.append(element('dt', '', label), element('dd', '', value));
  return item;
}

function correctFieldCount(check) {
  if (!check || typeof check !== 'object') return 0;
  if (Array.isArray(check.correct_fields)) return check.correct_fields.length;
  if (Number.isFinite(Number(check.correct_fields))) return Number(check.correct_fields);
  if (!check.expected || !check.actual || typeof check.expected !== 'object' || typeof check.actual !== 'object') return 0;
  return Object.entries(check.expected).filter(([key, value]) => JSON.stringify(check.actual[key]) === JSON.stringify(value)).length;
}

function expectedFieldCount(check) {
  if (!check || typeof check !== 'object') return 0;
  if (check.expected && typeof check.expected === 'object') return Object.keys(check.expected).length;
  return 0;
}

function fullTurn(turn) {
  const section = element('details', 'result-turn');
  const status = turn.result?.status ?? 'неизвестно';
  const check = turn.check;
  const checkText = check ? ' · память: ' + (check.passed ? 'пройдена' : 'не пройдена') : '';
  section.append(element('summary', '', integer(turn.number) + '. ' + String(turn.prompt ?? 'Вопрос') + ' · ' + (statusCopy[status] ?? status) + checkText));
  section.append(element('p', 'result-label', 'ВОПРОС'), element('div', 'result-text', turn.prompt ?? ''));
  section.append(element('p', 'result-label', 'ОТВЕТ'), element('div', 'result-text', turn.result?.text ?? 'Ответ не получен.'));
  if (turn.facts && typeof turn.facts === 'object') {
    section.append(element('p', 'result-label', 'FACTS ПОСЛЕ ХОДА'), element('pre', 'turn-data', JSON.stringify(turn.facts, null, 2)));
  }
  if (turn.context && typeof turn.context === 'object') {
    section.append(element('p', 'result-label', 'ОТПРАВЛЕННЫЙ КОНТЕКСТ'), element('pre', 'turn-data', JSON.stringify(turn.context, null, 2)));
  }
  if (check) {
    section.append(element('p', 'result-label', 'ПРОВЕРКА ПАМЯТИ'));
    section.append(element('pre', 'turn-data', JSON.stringify({passed: check.passed, correct_fields: check.correct_fields, expected: check.expected, actual: check.actual}, null, 2)));
  }
  return section;
}

function renderComparison(data) {
  const target = document.querySelector('#comparison-result');
  const fragment = document.createDocumentFragment();
  const conditions = data.conditions ?? {};
  if (data.provenance?.source === 'recorded_ui_sessions') fragment.append(element('p', 'comparison-provenance', 'Результаты этих трёх чатов. Обновления facts включены; дополнительные ветки показаны отдельно.'));
  fragment.append(element('p', 'comparison-conclusion', data.complete
    ? 'Сценарий завершён: результаты всех трёх стратегий сохранены.'
    : 'Сценарий сохранён частично: выводы по неполным данным ограничены.'));
  fragment.append(element('p', 'panel-lead',
    'Одинаковых вопросов: ' + integer(data.scenario?.length) +
    ' · модель: ' + String(conditions.model ?? 'не указана') +
    ' · reasoning: ' + String(conditions.reasoning_effort ?? 'не указан') +
    ' · окно: ' + integer(conditions.keep_last_messages) + ' реплик.'));
  if (typeof conditions.note === 'string') fragment.append(element('p', 'panel-lead', conditions.note));

  const entries = data.modes && typeof data.modes === 'object'
    ? (Array.isArray(data.modes) ? data.modes.map((item, index) => [item.mode ?? String(index + 1), item]) : Object.entries(data.modes))
    : [];
  entries.sort((a,b) => ['sliding','facts','branching'].indexOf(a[0])-['sliding','facts','branching'].indexOf(b[0]));
  if (entries.length) {
    const grid = element('div', 'comparison-grid');
    for (const [mode, result] of entries) {
      const card = element('article', 'compare-card');
      card.append(element('p', 'panel-kicker', 'СТРАТЕГИЯ'), element('h3', '', modeName(mode)));
      const totals = result.token_accounting ?? {};
      const summary = result.summary ?? {};
      const checks = (result.turns ?? []).map(turn => turn.check).filter(Boolean);
      const passed = checks.filter(check => check.passed).length;
      const correctFields = checks.reduce((sum, check) => sum + correctFieldCount(check), 0);
      const expectedFields = checks.reduce((sum, check) => sum + expectedFieldCount(check), 0) || 12;
      const metrics = element('dl', 'compact-stats compare-stats');
      metrics.append(
        comparisonMetric('Память', integer(passed) + ' / ' + integer(checks.length || 2)),
        comparisonMetric('Верные поля', integer(correctFields) + ' / ' + integer(expectedFields)),
        comparisonMetric('Всего токенов', integer(totals.known_total_tokens)),
        comparisonMetric('Стоимость', cost(summary.known_cost_usd)),
        comparisonMetric('Input / Output', integer(totals.known_input_tokens) + ' / ' + integer(totals.known_output_tokens)),
        comparisonMetric('API-вызовы', integer(summary.api_requests)),
      );
      card.append(metrics);
      card.append(element('p', 'field-note', summary.cost_complete && totals.complete
        ? 'Учёт полный.'
        : 'Показана известная часть; часть usage или стоимости отсутствует.'));
      const turns = element('details', 'mode-turns');
      turns.append(element('summary', '', 'Все вопросы, ответы и память · ' + integer(result.turns?.length) + ' ходов'));
      for (const turn of result.turns ?? []) turns.append(fullTurn(turn));
      card.append(turns);
      grid.append(card);
    }
    fragment.append(grid);
  }

  const experiment = data.branching_experiment;
  if (experiment && typeof experiment === 'object') {
    const branchSection = element('section', 'branch-results');
    branchSection.append(element('h3', '', 'Две независимые ветки'));
    const branchSummary = experiment.summary_including_common_run ?? {};
    const branchTokens = experiment.token_accounting_including_common_run ?? {};
    branchSection.append(element('p', 'panel-lead',
      'Общий прогон и продолжения веток: ' + integer(branchTokens.known_total_tokens) +
      ' токенов · ' + cost(branchSummary.known_cost_usd) +
      ' · API-вызовов: ' + integer(branchSummary.api_requests) + '.'));
    const branchGrid = element('div', 'comparison-grid branch-comparison');
    for (const item of experiment.branches ?? []) {
      const branchCard = element('article', 'compare-card');
      branchCard.append(element('p', 'panel-kicker', 'ВЕТКА'), element('h3', '', item.branch?.name ?? 'Без названия'));
      branchCard.append(element('p', 'result-label', 'ПРОДОЛЖЕНИЕ'), element('div', 'result-text', item.prompt ?? ''));
      branchCard.append(element('p', 'result-label', 'ОТВЕТ'), element('div', 'result-text', item.result?.text ?? 'Ответ не получен.'));
      if (item.probe) {
        branchCard.append(element('p', 'result-label', 'КОНТРОЛЬНЫЙ ВОПРОС'), element('div', 'result-text', item.probe.prompt ?? ''));
        branchCard.append(element('p', 'result-label', 'КОНТРОЛЬНЫЙ ОТВЕТ'), element('div', 'result-text', item.probe.result?.text ?? 'Ответ не получен.'));
      }
      branchGrid.append(branchCard);
    }
    branchSection.append(branchGrid);
    fragment.append(branchSection);
  }

  const quality = element('section', 'comparison-quality');
  quality.append(element('h3', '', 'Качество ответов и ограничения сравнения'));
  if (data.conclusion) quality.append(element('p', 'panel-lead', data.conclusion));
  for (const note of data.quality_notes ?? []) quality.append(element('p', 'panel-lead', note));
  fragment.append(quality);

  const full = element('details', 'full-results');
  full.append(element('summary', '', 'Полные сохранённые результаты'));
  const pre = element('pre', 'json-result', JSON.stringify(data, null, 2));
  pre.tabIndex = 0;
  full.append(pre);
  fragment.append(full);
  target.replaceChildren(fragment);
}

async function loadComparison() {
  const target = document.querySelector('#comparison-result');
  try {
    const {response, body} = await fetchJson('/api/comparison', {cache: 'no-store'});
    if (!response.ok) {
      target.textContent = errorText(body, 'Сохранённое сравнение пока недоступно.');
      return;
    }
    renderComparison(body);
  } catch {
    target.textContent = 'Не удалось загрузить сохранённое сравнение.';
  }
}

updateCount();
loadState();
loadComparison();
