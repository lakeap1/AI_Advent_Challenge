'use strict';
// The model selects composition; this module presents persisted results.
(() => {
  const el = (tag, cls, value) => {
    const node = document.createElement(tag);
    node.className = cls || '';
    if (value !== undefined) node.textContent = String(value);
    return node;
  };
  const status = {running:'Выполняется…', done:'Готово', success:'Готово', error:'Остановлено', rejected:'Отклонено', interrupted:'Прервано'};
  const stages = [['connection','MCP-соединение'],['search','Найти материалы'],['summarize','Подготовить разбор'],['save','Сохранить TXT']];
  function steps(run) {
    const block = el('section', 'composition-progress');
    block.dataset.runId = String(run.id);
    block.dataset.status = run.status;
    block.setAttribute('aria-label', 'Ход сохранения разбора');
    block.append(el('p', 'composition-state', run.status === 'success' ? 'Разбор сохранён' : status[run.status] || run.status));
    const list = el('ol', 'composition-steps');
    for (const [stage, label] of stages) {
      const event = (run.events || []).filter(item => item.stage === stage).at(-1);
      const item = el('li', '', '');
      item.dataset.stage = stage;
      item.dataset.status = event?.status || '';
      item.append(el('span', '', label), el('small', 'step-state', event ? status[event.status] || event.status : run.status === 'running' ? 'Ожидает предыдущий шаг' : 'Не выполнялся'));
      list.append(item);
    }
    block.append(list);
    if (run.materials) {
      const labels = {blender:'Blender Stack Exchange',computergraphics:'Computer Graphics Stack Exchange',gamedev:'Game Development Stack Exchange',wikipedia_en:'Wikipedia (en)',wikipedia_ru:'Wikipedia (ru)'};
      const source = run.materials.source || run.source || 'blender';
      block.append(el('p', 'field-note', (labels[source] || 'Источник') + ' · ' + (run.materials.sources?.length ?? 0) + ' фрагмента. ' + (run.materials.limitation || '')));
    }
    if (run.error) block.append(el('p', 'composition-error', run.error));
    if (run.call) {
      const call = run.call, usage = call.usage;
      block.append(el('p', 'composition-receipt',
        (usage ? `input ${usage.input_tokens} · output ${usage.output_tokens} · total ${usage.total_tokens}. ` : call.usage_status === 'not_requested' ? 'API не вызывался. ' : 'Usage неизвестен. ') +
        (call.cost_usd == null ? 'Стоимость неизвестна. ' : `$${call.cost_usd} USD. `) +
        `Вход: ${call.input_policy?.status || 'неизвестно'}; выход: ${call.output_policy?.status || 'неизвестно'}.`));
    }
    if (run.status === 'success' && typeof run.saved?.content === 'string') {
      const saved = el('div', 'composition-saved');
      saved.append(el('h3', '', 'Сохранённый разбор'), el('pre', 'composition-content', run.saved.content));
      const file = el('div', 'composition-file');
      const link = el('a', 'text-button', 'Скачать TXT ↓');
      link.href = '/api/composition/' + encodeURIComponent(run.id) + '/file';
      link.dataset.compositionDownload = String(run.id);
      file.append(link);
      if (run.saved_path || run.saved.filename) file.append(el('p', 'composition-path', run.saved_path || 'data/composition/results/' + run.saved.filename));
      if (run.saved.sha256) file.append(el('p', 'field-note', `${run.saved.bytes_written ?? '—'} байт · SHA-256: ${run.saved.sha256}`));
      saved.append(file);
      block.append(saved);
    }
    return block;
  }
  function linkedRun(state, message) {
    const request = (state.requests || []).find(item => String(item.id) === String(message.request_id));
    return (state.composition_runs || []).find(run =>
      (run.parent_request_id != null && String(run.parent_request_id) === String(message.request_id))
      || request?.metadata?.composition_run_id === run.id);
  }
  function appendToMessage(article, run) {
    article.append(steps(run));
    article.dataset.compositionRunId = String(run.id);
  }
  function pending(state) {
    const represented = new Set((state.messages || []).map(message => linkedRun(state, message)?.id).filter(Boolean));
    return (state.composition_runs || []).filter(run =>
      String(run.branch_id) === String(state.active_branch)
      && !represented.has(run.id));
  }
  function detached(run, role) {
    const article = el('article', role === 'user' ? 'message user' : 'composition-event');
    article.append(el('div', 'message-meta', role === 'user' ? 'ВЫ' : 'ХОД РАБОТЫ'));
    if (role === 'user') article.append(el('div', 'message-text', run.question || 'Запрос на сохранение разбора'));
    else { article.id = 'composition-' + run.id; appendToMessage(article, run); }
    return article;
  }
  function startPending(prompt) {
    const feed = document.getElementById('conversation');
    const user = el('article', 'message user composition-pending');
    user.append(el('div', 'message-meta', 'ВЫ'), el('div', 'message-text', prompt));
    const article = el('article', 'message assistant composition-pending');
    article.setAttribute('aria-live', 'polite');
    article.append(el('div', 'message-meta', 'ХОД РАБОТЫ'), el('p', 'composition-state', 'Помощник выбирает действие…'));
    feed.append(user, article);
    feed.scrollTop = feed.scrollHeight;
    return {
      update(run) { article.replaceChildren(el('div', 'message-meta', 'ХОД РАБОТЫ'), steps(run)); feed.scrollTop = feed.scrollHeight; },
      clear() { user.remove(); article.remove(); },
    };
  }
  window.chatComposition = {steps, linkedRun, appendToMessage, pending, detached, startPending};
})();
