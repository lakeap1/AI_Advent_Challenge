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
let busy = false;
let ready = false;
let bootId = null;

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
  const details = noCall ? 'LLM не вызывалась.' :
    'Кеш входа: ' + (usage?.cached_input_tokens ?? 'неизвестно') +
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
      item.append(element('div', 'message-text', message.content));
      turn.append(item);
    }
    if (result.status !== 'ok') {
      turn.append(element('div', 'request-error', result.text || (result.status === 'pending' ? 'Запрос выполняется…' : 'Ответ не получен.')));
    }
    turn.append(usageCard(result));
    fragment.append(turn);
  }
  conversation.replaceChildren(fragment);
  document.querySelector('#empty').hidden = state.requests.length > 0;
  document.querySelector('#chat-cost').textContent = amount(state.summary.known_cost_usd) + ' USD';
  document.querySelector('#cost-note').textContent = state.summary.cost_complete
    ? 'Всего за чат · вызовов API: ' + state.summary.api_requests + '. Оценка по тарифу.'
    : 'Известная часть суммы. Стоимость ' + state.summary.unknown_cost_requests + ' запросов неизвестна; итог неполный.';
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
  send.disabled = busy || !ready;
  restore.disabled = busy;
  prompt.readOnly = busy;
  examples.forEach(button => { button.disabled = busy; });
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
  controls();
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
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
  document.querySelector('#count').textContent = Array.from(prompt.value).length + ' символов';
}
prompt.addEventListener('input', updateCount);
examples.forEach(button => button.addEventListener('click', () => {
  prompt.value = button.dataset.prompt;
  updateCount();
  prompt.focus();
}));
restore.addEventListener('click', loadState);
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !ready) return;
  busy = true;
  controls();
  notice.textContent = '';
  panel.classList.remove('error');
  panel.setAttribute('aria-busy', 'true');
  waiting.hidden = false;
  badge.textContent = 'В работе';
  try {
    const response = await fetch('/api/ask', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: prompt.value}),
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
