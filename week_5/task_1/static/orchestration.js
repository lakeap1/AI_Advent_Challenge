'use strict';
// Render the persisted run. Model selection and execution remain on the server.
(() => {
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    node.className = cls || '';
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const statuses = {running:'Выполняется', success:'Готово', done:'Готово', error:'Остановлено', rejected:'Отклонено', interrupted:'Прервано'};
  const labels = {research:'Материалы', processing:'Сравнение и разбор', library:'Библиотека'};
  const json = value => JSON.stringify(value, null, 2);
  function disclosure(title, value, cls = '') {
    const details = el('details', cls);
    details.append(el('summary', '', title), el('pre', 'orchestration-json', typeof value === 'string' ? value : json(value)));
    return details;
  }
  function resultSummary(result) {
    if (!result || typeof result !== 'object') return 'Результат ещё не получен';
    const parts = [];
    if (result.material_id) parts.push('Материал: ' + result.material_id);
    if (result.material_ids) parts.push('Материалы: ' + result.material_ids.join(', '));
    if (result.report_id) parts.push('Отчёт: ' + result.report_id);
    if (result.sources) for (const source of result.sources) parts.push(source.title + ' · ' + source.url);
    if (result.filename) parts.push('Файл: ' + result.filename);
    if (result.sha256) parts.push('SHA-256: ' + result.sha256);
    if (result.verified !== undefined) parts.push(result.verified ? 'Чтение подтверждено' : 'Чтение не подтверждено');
    if (!parts.length) parts.push(json(result).slice(0, 350));
    return parts.join('\n');
  }
  function steps(run) {
    const block = el('section', 'orchestration-run');
    block.dataset.runId = String(run.id);
    block.dataset.status = run.status;
    block.dataset.verified = String(run.verified === true);
    block.setAttribute('aria-label', 'Исследование с несколькими MCP-серверами');
    const head = el('div', 'orchestration-heading');
    head.append(el('h3', '', 'От материалов к проверенному отчёту'),
      el('span', 'orchestration-state', run.status === 'success' && run.verified ? 'Сохранение проверено ✓' : statuses[run.status] || run.status));
    block.append(head, el('p', 'orchestration-caption', 'Каждое действие выбирает модель. Порядок и результат сохраняются в диалоге.'));
    const catalog = el('section', 'orchestration-catalog');
    catalog.dataset.component = 'orchestration-catalog';
    catalog.setAttribute('aria-label', 'Обнаруженные MCP-серверы');
    for (const server of run.catalog || []) {
      const card = el('div', 'orchestration-server');
      card.dataset.server = server.server;
      card.append(el('strong', '', labels[server.server] || server.server), el('span', 'orchestration-server-name', server.server_info?.name || server.server));
      const tools = el('ul', 'orchestration-tool-list');
      for (const tool of server.tools || []) {
        const item = el('li', '', tool.qualified_name || server.server + '__' + tool.name);
        item.title = tool.description || '';
        tools.append(item);
      }
      card.append(tools, disclosure('Сессия и схемы инструментов', {session_id:server.session_id, server_info:server.server_info, tools:server.tools}));
      catalog.append(card);
    }
    if (!catalog.childElementCount) catalog.append(el('p', 'field-note', 'Открываем MCP-сессии и обнаруживаем инструменты…'));
    block.append(catalog);
    const list = el('ol', 'orchestration-calls');
    list.setAttribute('aria-label', 'Порядок выбранных вызовов');
    for (const [index, call] of (run.calls || []).entries()) {
      const item = el('li', 'orchestration-call');
      item.dataset.server = call.server;
      item.dataset.tool = call.tool;
      item.dataset.status = call.status;
      item.dataset.callId = call.call_id || '';
      const line = el('div', 'orchestration-call-heading');
      line.append(el('span', 'orchestration-number', call.sequence || index + 1),
        el('strong', '', call.qualified_name || call.server + '__' + call.tool),
        el('span', 'orchestration-call-status', statuses[call.status] || call.status));
      item.append(line, el('pre', 'orchestration-arguments', json(call.arguments || {})));
      if (call.result) item.append(el('p', 'orchestration-result-summary', resultSummary(call.result)), disclosure('Полный результат инструмента', call.result));
      if (call.error) item.append(el('p', 'orchestration-error', call.error));
      list.append(item);
    }
    if (list.childElementCount) block.append(list);
    if (run.error) block.append(el('p', 'orchestration-error', run.error));
    if (run.status === 'success' && run.verified && typeof run.saved?.content === 'string') {
      const report = el('section', 'orchestration-saved');
      report.append(el('h4', '', 'Сохранённый разбор'), el('pre', 'orchestration-report', run.saved.content));
      report.lastElementChild.dataset.component = 'orchestration-report';
      const link = el('a', 'text-button', 'Скачать проверенный TXT ↓');
      link.href = '/api/orchestration/' + encodeURIComponent(run.id) + '/file';
      link.dataset.orchestrationDownload = String(run.id);
      report.append(link, el('p', 'field-note', (run.saved.bytes_written ?? '—') + ' байт · SHA-256: ' + (run.saved.sha256 || 'неизвестно')));
      block.append(report);
    }
    if (run.model_steps?.length) {
      block.append(el('p', 'field-note', run.model_steps.length + ' шагов модели. Токены, стоимость и результаты политик каждого API-вызова — в панели «Расходы».'));
    }
    return block;
  }
  function linkedRun(state, message) {
    return (state?.orchestration_runs || []).find(run => String(run.parent_request_id) === String(message.request_id) && message.request_id != null);
  }
  function appendToMessage(article, run) { article.append(steps(run)); article.dataset.orchestrationRunId = String(run.id); }
  function pending(state) {
    const represented = new Set((state?.messages || []).map(message => linkedRun(state, message)?.id).filter(Boolean));
    return (state?.orchestration_runs || []).filter(run => String(run.branch_id) === String(state.active_branch) && !represented.has(run.id));
  }
  function detached(run, role) {
    const article = el('article', role === 'user' ? 'message user' : 'orchestration-event');
    article.append(el('div', 'message-meta', role === 'user' ? 'ВЫ' : 'ХОД РАБОТЫ'));
    if (role === 'user') article.append(el('div', 'message-text', run.question));
    else appendToMessage(article, run);
    return article;
  }
  function startPending(prompt) {
    const feed = document.getElementById('conversation');
    const user = el('article', 'message user composition-pending');
    const article = el('article', 'message assistant composition-pending');
    user.append(el('div', 'message-meta', 'ВЫ'), el('div', 'message-text', prompt));
    article.setAttribute('aria-live', 'polite');
    article.append(el('div', 'message-meta', 'ХОД РАБОТЫ'), el('p', 'composition-state', 'Помощник выбирает действие…'));
    feed.append(user, article);
    feed.scrollTop = feed.scrollHeight;
    let lastSignature = '';
    function update(run, renderer) {
      const signature = run.id + ':' + run.status + ':' + (run.calls?.length || 0) + ':' + (run.calls?.at(-1)?.status || '') + ':' + (run.events?.length || 0);
      if (signature === lastSignature) return;
      lastSignature = signature;
      article.replaceChildren(el('div', 'message-meta', 'ХОД РАБОТЫ'), renderer(run));
      feed.scrollTop = feed.scrollHeight;
    }
    return {update:run=>update(run, window.chatComposition.steps), updateOrchestration:run=>update(run, steps), clear(){user.remove();article.remove();}};
  }
  window.chatOrchestration = {steps, linkedRun, appendToMessage, pending, detached, startPending};
})();
