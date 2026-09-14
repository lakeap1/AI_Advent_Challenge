const form = document.querySelector('#ask-form');
const prompt = document.querySelector('#prompt');
const send = document.querySelector('#send');
const label = document.querySelector('#send-label');
const panel = document.querySelector('#answer-panel');
const answer = document.querySelector('#answer');
const notice = document.querySelector('#notice');
const badge = document.querySelector('#badge');
const waiting = document.querySelector('#waiting');
const examples = [...document.querySelectorAll('.suggestion')];
const usagePanel = document.querySelector('#usage-panel');
const money = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', minimumFractionDigits: 6, maximumFractionDigits: 8});
let busy = false;

function renderUsage(result) {
  const unused = result.usage_status === 'not_requested';
  const usage = result.usage;
  for (const [id, field] of [['input-tokens', 'input_tokens'], ['output-tokens', 'output_tokens'], ['total-tokens', 'total_tokens']]) {
    const count = usage?.[field];
    document.getElementById(id).textContent = unused ? '0' : Number.isInteger(count) ? count.toLocaleString('ru-RU') : '—';
  }
  const cost = result.cost_usd === null || result.cost_usd === undefined ? null : Number(result.cost_usd);
  document.querySelector('#cost-usd').textContent = unused ? '$0.00' : cost !== null && Number.isFinite(cost) ? money.format(cost) : '—';
  const details = [];
  if (unused) {
    details.push('LLM не вызывалась: токены не расходовались.');
  } else if (!usage) {
    details.push('API не вернул статистику. Расход и стоимость неизвестны.');
  } else {
    if (usage.cached_input_tokens !== null) details.push(`Из входа: кеш ${usage.cached_input_tokens ?? '—'}.`);
    if (usage.reasoning_tokens !== null) details.push(`Из выхода: reasoning ${usage.reasoning_tokens ?? '—'}.`);
    details.push(cost === null ? 'Стоимость неизвестна: недостаточно данных или тариф не подходит.' : 'Оценка по тарифу из конфига, не счёт провайдера.');
  }
  document.querySelector('#usage-details').textContent = details.join(' ');
  usagePanel.hidden = false;
}

function updateCount() {
  document.querySelector('#count').textContent = `${Array.from(prompt.value).length} символов`;
}
prompt.addEventListener('input', updateCount);
examples.forEach(button => button.addEventListener('click', () => {
  prompt.value = button.dataset.prompt;
  updateCount();
  prompt.focus();
}));

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  send.disabled = true;
  prompt.readOnly = true;
  examples.forEach(button => { button.disabled = true; });
  label.textContent = 'Агент отвечает…';
  document.querySelector('#empty').hidden = true;
  answer.hidden = true;
  answer.textContent = '';
  usagePanel.hidden = true;
  notice.textContent = '';
  panel.classList.remove('error');
  panel.setAttribute('aria-busy', 'true');
  waiting.hidden = false;
  badge.textContent = 'В работе';
  try {
    const response = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: prompt.value }),
    });
    const result = await response.json();
    if (typeof result.text !== 'string' || !['ok', 'rejected', 'error'].includes(result.status)) {
      throw new Error('Unexpected response');
    }
    renderUsage(result);
    if (response.ok && result.status === 'ok') {
      answer.textContent = result.text;
      answer.hidden = false;
      badge.textContent = 'Ответ получен';
      notice.textContent = 'Готово. Ответ агента — ниже.';
    } else {
      panel.classList.add('error');
      notice.textContent = result.text;
      badge.textContent = result.status === 'rejected' ? 'Проверьте запрос' : 'Не удалось ответить';
    }
  } catch {
    panel.classList.add('error');
    badge.textContent = 'Нет соединения';
    notice.textContent = 'Не удалось получить ответ приложения. Проверьте, что сервер запущен, и повторите запрос.';
  } finally {
    waiting.hidden = true;
    panel.setAttribute('aria-busy', 'false');
    send.disabled = false;
    prompt.readOnly = false;
    examples.forEach(button => { button.disabled = false; });
    label.textContent = 'Отправить агенту';
    busy = false;
  }
});
