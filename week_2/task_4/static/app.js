const form = document.querySelector('#ask-form');
const prompt = document.querySelector('#prompt');
const send = document.querySelector('#send');
const label = document.querySelector('#send-label');
const panel = document.querySelector('#answer-panel');
const notice = document.querySelector('#notice');
const badge = document.querySelector('#badge');
const waiting = document.querySelector('#waiting');
const conversation = document.querySelector('#conversation');
const restore = document.querySelector('#restore');
const examples = [...document.querySelectorAll('.suggestion')];
const money = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', minimumFractionDigits: 6, maximumFractionDigits: 8});
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const modeSelect = document.querySelector('#context-mode');
let busy = false;
let ready = false;
let bootId = null;
let previewTimer;
let previewVersion = 0;
let inputVersion = 0;
const integer = value => value === null || value === undefined ? 'неизвестно' : new Intl.NumberFormat('ru-RU').format(value);

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function amount(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value)) ? money.format(Number(value)) : 'неизвестна';
}
function usageCard(result) {
  const card = element('section', 'request-usage');
  card.setAttribute('aria-label', 'Статистика запроса');
  const noCall = result.usage_status === 'not_requested';
  const usage = result.usage;
  const grid = element('dl', 'usage-grid');
  for (const [title, value] of [
    ['Вход · input', noCall ? '—' : usage?.input_tokens ?? 'неизвестно'],
    ['Выход · output', noCall ? '—' : usage?.output_tokens ?? 'неизвестно'],
    ['Всего токенов', noCall ? '—' : usage?.total_tokens ?? 'неизвестно'],
    ['Стоимость · USD', noCall ? 'нет вызова' : amount(result.cost_usd)],
  ]) {
    const item = element('div', '');
    item.append(element('dt', '', title), element('dd', '', String(value)));
    grid.append(item);
  }
  card.append(grid);
  const metrics = result.metadata?.token_metrics;
  if (metrics) {
    card.append(element('p', 'usage-details token-breakdown',
      'Полный архив до сжатия (локальная оценка, не расход): новое сообщение ' + integer(metrics.new_message_tokens_estimate) +
      ' · вся история ' + integer(metrics.history_tokens_estimate) +
      ' · инструкции ' + integer(metrics.instructions_tokens_estimate) +
      ' · сумма текста ' + integer(metrics.input_text_tokens_estimate) + ' токенов.'));
  }
  if (result.metadata?.provider_error?.code) card.append(element('p','usage-details',
    'API: ' + result.metadata.provider_error.code + ' · HTTP ' + result.metadata.provider_error.http_status));
  const details = noCall ? 'LLM не вызывалась.' :
    'Кеш входа: ' + (usage?.cached_input_tokens ?? 'неизвестно') +
    ' · Запись кеша: ' + (usage?.cache_write_input_tokens ?? 'неизвестно') +
    ' · Reasoning в выходе: ' + (usage?.reasoning_tokens ?? 'неизвестно');
  card.append(element('p', 'usage-details', details));
  const statuses = {accepted: 'пройдена', rejected: 'отклонено', not_checked: 'не выполнялась'};
  if (result.input_policy && result.output_policy) {
    card.append(element('p', 'usage-details policy-details',
      'Вход: ' + (statuses[result.input_policy.status] ?? result.input_policy.status) +
      ' · Выход: ' + (statuses[result.output_policy.status] ?? result.output_policy.status)));
  }
  return card;
}
function renderState(state, animateId = null) {
  if (!Array.isArray(state.messages) || !Array.isArray(state.requests) || !state.summary) throw new Error('Invalid state');
  const fragment = document.createDocumentFragment();
  const byRequest = new Map();
  for (const message of state.messages) {
    const list = byRequest.get(message.request_id) ?? [];
    list.push(message);
    byRequest.set(message.request_id, list);
  }
  for (const result of state.requests) {
    const turn = element('article', 'turn');
    turn.dataset.requestId = result.id;
    for (const message of byRequest.get(result.id) ?? []) {
      const item = element('div', 'message ' + (message.role === 'assistant' ? 'assistant' : 'user'));
      if (result.id === animateId && message.role === 'assistant') item.classList.add('new-message');
      item.append(element('p', 'message-role', message.role === 'assistant' ? 'АГЕНТ' : 'ВЫ'));
      if (message.content.length > 900) {
        item.append(element('div', 'message-text', message.content.slice(0, 450) + '…'));
        const details = element('details', 'full-message');
        details.append(element('summary', '', 'Весь текст · ' + integer(Array.from(message.content).length) + ' символов'));
        details.addEventListener('toggle', () => {
          if (details.open && details.children.length === 1) details.append(element('div', 'message-text', message.content));
        });
        item.append(details);
      } else item.append(element('div', 'message-text', message.content));
      turn.append(item);
    }
    if (result.status !== 'ok') {
      turn.append(element('div', 'request-error', result.text || (result.status === 'pending' ? 'Запрос выполняется…' : 'Ответ не получен.')));
    }
    if (result.metadata?.kind === 'summary') turn.append(element('p', 'message-role', 'СЖАТИЕ ИСТОРИИ · ' + (result.status === 'ok' ? 'SUMMARY СОХРАНЕНО' : 'ОШИБКА')));
    turn.append(usageCard(result));
    fragment.append(turn);
  }
  conversation.replaceChildren(fragment);
  document.querySelector('#empty').hidden = state.requests.length > 0;
  document.querySelector('#chat-cost').textContent = amount(state.summary.known_cost_usd) + ' USD';
  document.querySelector('#cost-note').textContent = state.summary.cost_complete
    ? 'Всего за чат · вызовов API: ' + state.summary.api_requests + '. Оценка по тарифу.'
    : 'Известная часть суммы. Стоимость ' + state.summary.unknown_cost_requests + ' запросов неизвестна; итог неполный.';
  if (state.context) {
    document.querySelector('#history-count').textContent = 'Вся история сейчас: ≈ ' + integer(state.context.history_tokens_estimate) + ' токенов текста';
    document.querySelector('#context-limits').textContent = 'Лимиты ' + state.context.model + ': окно ' + integer(state.context.context_window) + ', вход ' + integer(state.context.max_input_tokens) + '. Автообрезка выключена.';
  }
  if (state.token_accounting) {
    const t = state.token_accounting;
    document.querySelector('#chat-tokens').textContent = 'Израсходовано: ' + integer(t.known_total_tokens) + (t.complete ? '' : ' + ?') + ' токенов · вход ' + integer(t.known_input_tokens) + ' / выход ' + integer(t.known_output_tokens);
  }
  renderMemory(state);
  renderGrowth(state.requests);
  schedulePreview();
  const restarted = bootId !== null && bootId !== state.boot_id;
  bootId = state.boot_id;
  document.querySelector('#memory-status').textContent = restarted
    ? 'Новый запуск · история восстановлена'
    : state.messages.length ? 'История загружена · сообщений: ' + state.messages.length : 'История пока пуста';
  document.querySelector('#session-info').textContent = 'Чат ' + String(state.chat_id).slice(0, 8) + ' · Запуск ' + state.boot_id + ' · Luna';
  if (animateId !== null) {
    requestAnimationFrame(() => conversation.scrollTo({top: conversation.scrollHeight, behavior: reducedMotion.matches ? 'instant' : 'smooth'}));
  }
}
function controls() {
  modeSelect.disabled = busy;
  send.disabled = busy || !ready;
  restore.disabled = busy;
  prompt.readOnly = busy;
  examples.forEach(button => { button.disabled = busy; });
  document.querySelector('#brief-file').disabled = busy;
  label.textContent = busy ? 'Агент отвечает…' : 'Отправить агенту';
}
function fail(text) {
  panel.classList.add('error');
  badge.textContent = 'Нет соединения';
  notice.textContent = text;
  document.querySelector('#memory-status').textContent = 'Сервер недоступен';
}
async function loadState() {
  if (busy) return;
  busy = true;
  inputVersion++;
  controls();
  try {
    const response = await fetch('/api/state?mode=' + encodeURIComponent(modeSelect.value), {cache: 'no-store'});
    const body = await response.json();
    if (!response.ok) throw new Error(body.text || 'Не удалось прочитать историю.');
    renderState(body);
    ready = true;
    notice.textContent = '';
    panel.classList.remove('error');
    badge.textContent = 'Можно продолжать';
  } catch (error) {
    ready = false;
    fail(error instanceof TypeError ? 'Сервер недоступен. Запустите приложение и нажмите «Обновить». Сохранённая история останется в базе.' : error.message);
  } finally {
    busy = false;
    controls();
  }
}
function updateCount() {
  inputVersion++;
  document.querySelector('#count').textContent = integer(Array.from(prompt.value).length) + ' символов';
  schedulePreview();
}
prompt.addEventListener('input', updateCount);
examples.forEach(button => button.addEventListener('click', () => {
  prompt.value = button.dataset.prompt;
  updateCount();
  prompt.focus();
}));
restore.addEventListener('click', loadState);
modeSelect.addEventListener('change', () => { ready = false; loadState(); });
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !ready) return;
  busy = true;
  inputVersion++;
  clearTimeout(previewTimer); previewVersion++;
  controls();
  notice.textContent = '';
  panel.classList.remove('error');
  panel.setAttribute('aria-busy', 'true');
  waiting.hidden = false;
  badge.textContent = 'В работе';
  try {
    const response = await fetch('/api/ask', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: prompt.value, mode: modeSelect.value}),
    });
    const result = await response.json();
    if (typeof result.text !== 'string' || !['ok', 'rejected', 'error'].includes(result.status)) throw new Error('Invalid response');
    if (result.state) renderState(result.state, result.request_id);
    if (response.ok && result.status === 'ok') {
      prompt.value = '';
      updateCount();
      badge.textContent = 'Ответ сохранён';
      notice.textContent = 'Можно продолжить разговор.';
    } else {
      panel.classList.add('error');
      notice.textContent = result.text;
      badge.textContent = result.status === 'rejected' ? 'Проверьте сообщение' : 'Ответ не получен';
    }
  } catch {
    ready = false;
    fail('Соединение прервалось. Ответ мог быть сохранён. Нажмите «Обновить», прежде чем отправлять сообщение повторно.');
  } finally {
    waiting.hidden = true;
    panel.setAttribute('aria-busy', 'false');
    busy = false;
    controls();
  }
});
loadState();


function renderGrowth(requests) {
  const rows = document.createDocumentFragment();
  let cost = 0, tokens = 0, unknownCost = false, unknownTokens = false;
  for (const [index, request] of requests.entries()) {
    const called = request.usage_status !== 'not_requested';
    if (called) {
      if (request.cost_usd == null) unknownCost = true; else cost += Number(request.cost_usd);
      if (request.usage == null) unknownTokens = true; else tokens += request.usage.total_tokens;
    }
    const row = element('tr', request.code === 'context_length_exceeded' ? 'overflow-row' : '');
    row.dataset.requestId = request.id;
    const operation = request.metadata?.kind === 'summary' ? 'Сжатие истории' : 'Ответ пользователю';
    const status = request.status === 'ok' ? 'Готово' : request.code || request.status;
    for (const value of [index + 1, operation,
      called ? integer(request.usage?.input_tokens) : 'нет вызова',
      called ? integer(request.usage?.output_tokens) : '—',
      called ? integer(request.usage?.total_tokens) : '—', integer(tokens) + (unknownTokens ? ' + ?' : ''),
      called ? amount(request.cost_usd) : 'нет вызова', amount(cost) + (unknownCost ? ' + ?' : ''), status]) {
      row.append(element('td', '', String(value)));
    }
    rows.append(row);
  }
  document.querySelector('#growth-rows').replaceChildren(rows);
}
function schedulePreview() {
  clearTimeout(previewTimer);
  const version = ++previewVersion;
  const target = document.querySelector('#token-preview');
  if (!prompt.value.trim()) { target.textContent = 'Введите сообщение для оценки токенов.'; return; }
  target.textContent = 'Считаем токены текста…';
  previewTimer = setTimeout(async () => {
    if (busy) return;
    try {
      const response = await fetch('/api/preview', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt:prompt.value,mode:modeSelect.value})});
      const body = await response.json();
      if (version !== previewVersion) return;
      const m = body.token_metrics;
      target.textContent = m ? '≈ ' + integer(m.new_message_tokens_estimate) + ' новый · ' + integer(m.history_tokens_estimate) + ' история · ' + integer(m.instructions_tokens_estimate) + ' инструкции = ' + integer(m.input_text_tokens_estimate) + ' токенов текста полного архива. Это не фактический расход: отправленный контекст зависит от summary и интервала. Точные токены — из API.' : body.text;
    } catch { if (version === previewVersion) target.textContent = 'Оценка недоступна. Проверьте соединение.'; }
  }, 450);
}
document.querySelector('#brief-file').addEventListener('change', async event => {
  const version = ++inputVersion;
  const file = event.target.files[0];
  if (!file || busy) return;
  if (file.size > 32_000_000) { notice.textContent = 'Бриф превышает допустимый размер файла 32 МБ.'; return; }
  let text;
  try { text = await file.text(); } catch {
    if (version === inputVersion) notice.textContent = 'Не удалось прочитать файл.';
    return;
  }
  if (busy || version !== inputVersion) return;
  prompt.value = text;
  updateCount();
  notice.textContent = 'Бриф загружен в поле. Проверьте оценку и отправьте его агенту.';
  event.target.value = '';
});

function renderMemory(state) {
  const c = state.compression;
  if (!c) return;
  document.querySelector('#compression-stats').textContent = c.mode === 'full'
    ? 'Полная история: все сообщения отправляются модели без summary.'
    : 'Сжатие каждые ' + c.summary_every_messages + ' завершённых сообщений · после сжатия оставляем ' + c.keep_last_messages + ' последних реплик · обновлений summary: ' + c.revisions + ' · сжато до сообщения №' + c.covered_through + ' · summary ≈ ' + c.summary_tokens_estimate + ' токенов.';
  document.querySelector('#summary-text').textContent = c.summary || 'Summary пока нет.';
  const tail = state.messages.filter(m => c.mode === 'full' || m.id > c.covered_through);
  document.querySelector('#context-tail').replaceChildren(...tail.map(m => element('p','', '#' + m.id + ' ' + (m.role === 'user' ? 'Вы: ' : 'Агент: ') + m.content)));
  const t = state.compression_accounting;
  document.querySelector('#compression-cost').textContent = 'На сжатие израсходовано: ' + integer(t.known_total_tokens) + (t.complete ? '' : ' + ?') + ' токенов. Это часть общего расхода чата. После прошлого сжатия накоплено ' + c.messages_since_summary + ' сообщений. Между сжатиями все новые реплики передаются дословно; старые не теряются.';
}
async function loadComparison() {
  const target = document.querySelector('#comparison-result');
  try {
    const response = await fetch('/api/comparison', {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) { target.textContent = data.text; return; }
    target.previousElementSibling.textContent = 'Сохранённый результат сравнения. Объём, источник ответов и условия подсчёта указаны ниже.';
    const grid = element('div','comparison-grid');
    for (const mode of ['full','compressed']) {
      const r = data.modes[mode]; const card = element('div','compare-card');
      card.append(element('h3','',mode === 'full' ? 'Без сжатия' : 'Со сжатием'));
      card.append(element('p','comparison-metrics','Вход: ' + integer(r.totals.known_input_tokens) + ' · выход: ' + integer(r.totals.known_output_tokens) + '\nВсего: ' + integer(r.totals.known_total_tokens) + (r.totals.complete ? '' : ' + ?') + ' токенов'));
      card.append(element('p','', 'USD: ' + amount(r.cost.known_cost_usd) + (r.cost.cost_complete ? '' : ' + ?') + ' · API-вызовов: ' + r.cost.api_requests));
      card.append(element('p','', 'Из них summary: ' + integer(r.summary_totals.known_total_tokens) + ' токенов'));
      card.append(element('p','', 'Проверки фактов: ' + r.passed + ' / ' + r.checks.length)); grid.append(card);
    }
    target.replaceChildren(grid);
    target.append(element('p','comparison-metrics',data.conclusion));
    target.append(element('p','growth-note',data.method));
    target.append(element('p','growth-note','Меньше токенов не означает дешевле: кэш входа и оплачиваемая генерация summary влияют на USD отдельно.'));
    if (data.experiment_type === 'dialogue_from_empty') {
      const trend = element('details', 'comparison-check');
      trend.append(element('summary', '', 'Расход с первого вопроса · динамика по всему диалогу'));
      const wrap = element('div', 'table-scroll'); wrap.tabIndex = 0;
      const table = element('table', '');
      const head = element('thead', ''); const header = element('tr', '');
      for (const title of ['После вопроса', 'Реплик в чате', 'Без сжатия: все токены', 'Со сжатием: все токены', 'Обновлений summary']) header.append(element('th', '', title));
      head.append(header); table.append(head); const body = element('tbody', '');
      data.modes.full.turns.forEach((turn, index) => {
        if (turn.number % 3 !== 0) return;
        const compressed = data.modes.compressed.turns[index]; const row = element('tr', '');
        for (const value of [turn.number, turn.history_messages_after, turn.cumulative_tokens.known_total_tokens, compressed.cumulative_tokens.known_total_tokens, compressed.summary_revision]) row.append(element('td', '', integer(value)));
        body.append(row);
      });
      table.append(body); wrap.append(table); trend.append(wrap); target.append(trend);
      const dialogue = element('details', 'comparison-check');
      dialogue.append(element('summary', '', 'Все 24 теоретических вопроса и реальные ответы обоих режимов'));
      data.scenario.forEach((turn,index) => {
        const section = element('details', 'comparison-check');
        section.append(element('summary', '', turn.number + '. ' + turn.topic), element('p', 'compare-answer', turn.prompt));
        const answers = element('div', 'comparison-grid');
        for (const mode of ['full', 'compressed']) {
          const card = element('div', 'compare-card');
          card.append(element('h3', '', mode === 'full' ? 'Без сжатия' : 'Со сжатием'), element('p', 'compare-answer', data.modes[mode].turns[index].answer));
          answers.append(card);
        }
        section.append(answers); dialogue.append(section);
      });
      target.append(dialogue);
    }
    data.modes.full.checks.forEach((check,index) => {
      const section = element('details','comparison-check');
      section.append(element('summary','',check.label + ' · ожидается: ' + check.expected));
      const answers = element('div','comparison-grid');
      for(const mode of ['full','compressed']) {
        const c=data.modes[mode].checks[index]; const card=element('div','compare-card');
        card.append(element('h3','', (mode === 'full' ? 'Без сжатия' : 'Со сжатием') + ' · ' + (c.passed ? 'пройдено' : 'не пройдено')));
        card.append(element('p','compare-answer', c.answer)); answers.append(card);
      }
      section.append(element('p','',check.prompt),answers); target.append(section);
    });
  } catch { target.textContent = 'Не удалось загрузить сохранённое сравнение.'; }
}
loadComparison();
