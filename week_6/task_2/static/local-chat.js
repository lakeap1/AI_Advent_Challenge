'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const view = $('local-chat-view');
  const input = $('local-prompt');
  const send = $('local-send');
  let state = null;
  let busy = false;
  let generation = 0;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const value = item => item == null ? 'неизвестно' : String(item);
  function notice(message, error = false) {
    $('local-notice').textContent = message;
    $('local-notice').classList.toggle('error', error);
  }
  function controls() {
    send.disabled = busy || Boolean(state?.blocked) || !input.value.trim();
    input.disabled = busy || Boolean(state?.blocked);
  }
  function renderAccounting(data) {
    const target = $('local-accounting');
    const ledger = data.ledger;
    const total = ledger.complete ? '$' + value(ledger.total_cost_usd) + ' USD' : 'неизвестна; известная часть $' + value(ledger.known_total_usd) + ' USD';
    const title = node('p', 'Запросов к модели: ' + ledger.requests + ' · стоимость чата: ' + total);
    const pricing = data.pricing;
    const tariff = node('p', 'Тариф API: вход $' + value(pricing.input_usd_per_million) + ' / 1 млн, выход $' + value(pricing.output_usd_per_million) + ' / 1 млн · ' + value(pricing.model) + ' · ' + value(pricing.service_mode));
    const source = node('p', 'Источник тарифа: ' + value(pricing.source) + ' · проверен ' + value(pricing.checked_on) + '. Электричество и оборудование: ' + value(pricing.infrastructure_cost) + '.');
    target.replaceChildren(title, tariff, source);
    if (!ledger.complete) target.append(node('p', 'Общий расход неполный: usage предыдущего запроса неизвестен.', 'local-warning'));
    if (data.blocked) target.append(node('p', data.block_reason || 'Новые вопросы остановлены до проверки расхода.', 'local-blocked'));
    if (data.receipts.length) {
      const details = node('details');
      details.append(node('summary', 'Запросы, токены и проверки (' + data.receipts.length + ')'));
      for (const receipt of [...data.receipts].reverse()) {
        const block = node('div', undefined, 'local-receipt');
        block.append(node('strong', 'Запрос №' + receipt.id + ' · ' + receipt.status));
        const usage = receipt.usage;
        block.append(node('p', usage ? 'Вход: ' + value(usage.input_tokens) + ' · выход: ' + value(usage.output_tokens) + ' · всего: ' + value(usage.total_tokens) : 'Токены: ' + (receipt.usage_status === 'not_requested' ? 'API не вызывался' : 'неизвестно')));
        if (usage) block.append(node('p', 'Кэш входа: ' + value(usage.cached_input_tokens) + ' · reasoning: ' + value(usage.reasoning_tokens)));
        block.append(node('p', 'Стоимость: ' + (receipt.cost_usd == null ? 'неизвестна' : '$' + receipt.cost_usd + ' USD') + ' · вход: ' + value(receipt.input_policy) + ' · выход: ' + value(receipt.output_policy)));
        if (receipt.error) block.append(node('p', receipt.error, 'local-blocked'));
        details.append(block);
      }
      target.append(details);
    }
  }
  function reveal(element, content) {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return Promise.resolve();
    element.textContent = '';
    return new Promise(resolve => {
      let index = 0;
      const step = () => {
        index = Math.min(content.length, index + 12);
        element.textContent = content.slice(0, index);
        if (index < content.length) requestAnimationFrame(step);
        else resolve();
      };
      requestAnimationFrame(step);
    });
  }
  async function render(data, animate = false) {
    state = data;
    const feed = $('local-feed');
    const inner = node('div', undefined, 'local-feed-inner');
    if (!data.messages.length) inner.append(node('p', 'История пуста. Начните с вопроса о графике или художественной работе.', 'local-empty'));
    let lastAnswer = null;
    for (const message of data.messages) {
      const article = node('article', undefined, 'local-message ' + (message.role === 'assistant' ? 'assistant' : 'user'));
      article.append(node('small', message.role === 'assistant' ? 'Локальный помощник' : 'Вы'));
      const content = node('p', message.content);
      article.append(content);
      inner.append(article);
      if (message.role === 'assistant') lastAnswer = {element: content, content: message.content};
    }
    feed.replaceChildren(inner);
    renderAccounting(data);
    controls();
    if (animate && lastAnswer) await reveal(lastAnswer.element, lastAnswer.content);
    feed.scrollTop = feed.scrollHeight;
  }
  async function refresh() {
    const epoch = ++generation;
    notice('Восстанавливаем локальную историю…');
    try {
      const response = await fetch('/api/local/state', {cache: 'no-store'});
      if (!response.ok) throw new Error('Не удалось загрузить локальную историю.');
      const data = await response.json();
      if (epoch === generation) {await render(data); notice(data.blocked ? data.block_reason : 'История сохранена на этом компьютере.', data.blocked);}
    } catch (error) {
      if (epoch === generation) notice('Локальная история недоступна: ' + error.message, true);
    }
  }
  async function runtime() {
    try {
      const response = await fetch('/api/local/runtime', {cache: 'no-store'});
      if (!response.ok) throw new Error('Не удалось проверить модель.');
      const data = await response.json();
      $('local-runtime').textContent = data.status === 'ready' ? 'Готова: ' + data.model + ' · локальная Ollama' : 'Недоступна: ' + (data.error || data.model);
    } catch (error) {
      $('local-runtime').textContent = 'Локальная модель недоступна: ' + error.message;
    }
  }
  function show() {
    $('regular-workspace').hidden = true;
    $('digest-chat-view').hidden = true;
    view.hidden = false;
    $('local-chat-open').setAttribute('aria-pressed', 'true');
    $('digest-chat-open').setAttribute('aria-pressed', 'false');
    document.querySelector('.skip-link').setAttribute('href', '#local-prompt');
    document.querySelector('.app-shell').classList.remove('chats-mobile-open');
    document.querySelectorAll('[data-shell-toggle="chats"]').forEach(button => button.setAttribute('aria-expanded', 'false'));
    localStorage.setItem('graphics-view', 'local');
    $('local-chat-title').setAttribute('tabindex', '-1');
    $('local-chat-title').focus();
    refresh();
    runtime();
  }
  $('local-chat-open').addEventListener('click', show);
  $('local-back').addEventListener('click', () => {window.cloudChat.show();$('conversation-title').setAttribute('tabindex', '-1');$('conversation-title').focus();});
  document.addEventListener('click', event => {
    if (!view.hidden && event.target.closest('.dialogue-item, [data-panel="create"], .profile-launch')) window.cloudChat.show();
  }, true);
  input.addEventListener('input', controls);
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!send.disabled) $('local-ask-form').requestSubmit();
    }
  });
  $('local-ask-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || state?.blocked || !input.value.trim()) return;
    generation++;
    busy = true; controls();
    const prompt = input.value;
    notice('Локальная модель готовит ответ…');
    try {
      const response = await fetch('/api/local/ask', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prompt})});
      const result = await response.json();
      if (result.state) await render(result.state, response.ok && result.status === 'ok');
      if (response.ok && result.status === 'ok') {input.value = ''; notice('Ответ сохранён.');}
      else notice(result.error || 'Ответ не получен. Проверьте локальный runtime и сохранённый расход.', true);
    } catch (error) {
      notice('Связь прервалась. Результат мог сохраниться; обновите страницу перед повтором. ' + error.message, true);
    } finally {busy = false; controls();}
  });
  controls();
  if (!['regular', 'digest'].includes(localStorage.getItem('graphics-view'))) show();
})();
