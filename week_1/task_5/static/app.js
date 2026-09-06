const form = document.querySelector('#compare-form');
const promptInput = document.querySelector('#prompt');
const repeats = document.querySelector('#repeats');
const statusText = document.querySelector('#status');
const errorText = document.querySelector('#error');
const summary = document.querySelector('#summary');
const cards = new Map([...document.querySelectorAll('.result')].map(el => [el.dataset.model, el]));
let results = [], total = 9;
const number = value => value == null ? '—' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits:3}).format(value);
const money = value => value == null ? 'неизвестна' : '$' + value.toFixed(6);

function showResult(result) {
  const card = cards.get(result.requested_model);
  card.querySelector('[data-value="seconds"]').textContent = number(result.seconds) + ' с';
  card.querySelector('[data-value="tokens"]').textContent = number(result.usage?.total_tokens);
  card.querySelector('[data-value="cost"]').textContent = money(result.cost_usd);
  const usage = result.usage;
  card.querySelector('.usage').textContent = usage
    ? `Вход ${number(usage.input_tokens)} · выход ${number(usage.output_tokens)} · из них рассуждение ${number(usage.output_tokens_details?.reasoning_tokens)} · кеш ${number(usage.input_tokens_details?.cached_tokens)}`
    : 'Расход токенов не получен';
  card.querySelector('.card-status').textContent = result.error || `Прогон ${result.attempt} завершён`;
  const answer = card.querySelector('.answer');
  answer.textContent = result.answer || result.error || 'Ответ отсутствует';
  if (result.cost_warning) card.querySelector('.card-status').textContent += ' · Стоимость неизвестна';
  answer.scrollTop = 0;
  answer.classList.remove('reveal');
  void answer.offsetWidth;
  answer.classList.add('reveal');
  card.querySelectorAll('.attempts button').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.attempt) === result.attempt)));
}

function handleEvent(event) {
  if (event.type === 'start') total = event.total;
  if (event.type === 'progress') {
    cards.forEach(card => card.classList.remove('working'));
    const card = cards.get(event.model);
    card.classList.add('working');
    card.querySelector('.card-status').textContent = `Прогон ${event.attempt}: модель отвечает…`;
    statusText.textContent = `${card.querySelector('h2').childNodes[0].textContent} · прогон ${event.attempt} · ожидаем полный ответ`;
  }
  if (event.type === 'result') {
    results.push(event.result);
    const card = cards.get(event.result.requested_model);
    card.classList.remove('working');
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.attempt = event.result.attempt;
    button.textContent = `Прогон ${event.result.attempt}`;
    button.addEventListener('click', () => showResult(event.result));
    card.querySelector('.attempts').append(button);
    showResult(event.result);
    document.querySelector('#counter').textContent = `${String(results.length).padStart(2,'0')} / ${String(total).padStart(2,'0')}`;
  }
  if (event.type === 'done') {
    summary.querySelector('tbody').replaceChildren();
    for (const row of event.run.summary) {
      const tr = document.createElement('tr');
      const name = cards.get(row.model).querySelector('h2').childNodes[0].textContent;
      for (const value of [name, `${row.completed} / ${row.attempts}`, row.median_seconds == null ? '—' : number(row.median_seconds) + ' с', number(row.median_tokens), money(row.total_cost_usd)]) {
        const td = document.createElement('td'); td.textContent = value; tr.append(td);
      }
      summary.querySelector('tbody').append(tr);
    }
    summary.hidden = false;
    const successful = results.filter(r => r.status === 'completed').length;
    statusText.textContent = `Сравнение завершено · получено ${successful} из ${total} ответов`;
    if (event.run.save_warning) { errorText.textContent = event.run.save_warning; errorText.hidden = false; }
  }
}

document.querySelector('#example').addEventListener('click', () => { promptInput.value = promptInput.dataset.example; promptInput.focus(); });
repeats.addEventListener('change', () => {
  total = Number(repeats.value) * 3;
  document.querySelector('#request-hint').textContent = `${total} платных API-вызовов · последовательно, с чередованием порядка · без кеширования`;
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (!promptInput.value.trim()) { promptInput.focus(); return; }
  document.querySelector('#restore').disabled = true;
  results = []; total = Number(repeats.value) * 3;
  summary.hidden = true; errorText.hidden = true;
  form.querySelectorAll('button,textarea,select').forEach(el => el.disabled = true);
  cards.forEach(card => {
    card.querySelector('.attempts').replaceChildren();
    card.querySelector('.answer').textContent = '';
    card.querySelectorAll('[data-value]').forEach(el => el.textContent = '—');
    card.querySelector('.usage').textContent = 'Вход / выход / рассуждение — после ответа';
    card.querySelector('.card-status').textContent = 'В очереди';
  });
  document.querySelector('#counter').textContent = `00 / ${String(total).padStart(2,'0')}`;
  statusText.textContent = 'Начинаем сравнение…';
  let done = false;
  try {
    const response = await fetch('/api/compare', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt:promptInput.value, repeats:Number(repeats.value)})});
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Ошибка сервера ${response.status}`);
    }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, {stream:!chunk.done});
      const lines = buffer.split('\n'); buffer = lines.pop();
      for (const line of lines) if (line.trim()) {
        const message = JSON.parse(line); handleEvent(message);
        if (message.type === 'done') done = true;
      }
      if (chunk.done) break;
    }
    if (!done) throw new Error('Соединение прервалось до завершения сравнения. Полученные ответы сохранены на экране.');
  } catch (error) {
    errorText.textContent = error.message; errorText.hidden = false;
    statusText.textContent = 'Сравнение не завершено';
  } finally {
    cards.forEach(card => { if (card.classList.contains('working')) card.querySelector('.card-status').textContent = 'Соединение завершено'; card.classList.remove('working'); });
    form.querySelectorAll('button,textarea,select').forEach(el => el.disabled = false);
    document.querySelector('#restore').disabled = false;
  }
});


document.querySelector('#restore').addEventListener('click', async () => {
  const button = document.querySelector('#restore');
  button.disabled = true; document.querySelector('#submit').disabled = true;
  errorText.hidden = true;
  try {
    const response = await fetch('/api/latest', {cache:'no-store'});
    const run = await response.json();
    if (!response.ok) throw new Error(run.error || 'Не удалось открыть сравнение.');
    results = []; total = run.repeats * 3;
    cards.forEach(card => card.querySelector('.attempts').replaceChildren());
    promptInput.value = run.request.input; repeats.value = String(run.repeats);
    repeats.dispatchEvent(new Event('change'));
    for (const result of run.results) handleEvent({type:'result', result});
    handleEvent({type:'done', run});
    statusText.textContent = 'Сохранённое сравнение · ' + new Date(run.created_at).toLocaleString('ru-RU') + ' · новых API-вызовов нет';
  } catch (error) {
    errorText.textContent = error.message; errorText.hidden = false;
  } finally {
    button.disabled = false; document.querySelector('#submit').disabled = false;
  }
});
