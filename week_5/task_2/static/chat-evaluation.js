'use strict';

// The backend owns the frozen questions, receipts and assessment. This module only
// presents its current owner-scoped snapshot and sends an explicitly typed prompt.
(() => {
  const $ = selector => document.querySelector(selector);
  const node = (tag, className = '', value = null) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== null && value !== undefined) result.textContent = String(value);
    return result;
  };
  const list = value => Array.isArray(value) ? value : [];
  const text = value => value === null || value === undefined ? '' : String(value);
  const json = value => {
    try { return JSON.stringify(value, null, 2); } catch { return text(value); }
  };
  const money = value => value === null || value === undefined || !Number.isFinite(Number(value))
    ? 'неизвестно' : '$' + Number(value).toPrecision(9).replace(/\.?0+$/, '');
  const number = value => value === null || value === undefined || !Number.isFinite(Number(value))
    ? 'неизвестно' : new Intl.NumberFormat('ru-RU').format(Number(value));
  const status = value => ({ok:'готово', failed:'ошибка', error:'ошибка', rejected:'отклонено',
    interrupted:'прервано', pending:'выполняется', running:'выполняется',
    complete:'завершено', assessed:'оценено'}[value] || text(value) || 'ожидает');
  const slug = value => encodeURIComponent(text(value));
  const safeStorage = {
    get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* private mode */ } },
  };

  let hooks = null;
  let state = null;
  let activeOwner = null;
  let active = false;
  let selectedRunId = null;
  let pendingSubmit = false;
  let ownerRevision = 0;
  let renderedRunKey = null;
  const view = $('#comparison-view');

  function ownerKey(source = state) {
    const identity = hooks?.identity(source) || {};
    const parts = [identity.profile_id, identity.task_id, identity.dialogue_id, identity.branch_id];
    return parts.every(part => part !== null && part !== undefined && part !== '')
      ? parts.map(text).join(':') : null;
  }

  function storageKey(suffix) { return 'chat-evaluation:' + activeOwner + ':' + suffix; }

  function belongs(run) {
    if (!activeOwner) return false;
    const owner = run?.owner;
    if (!owner || typeof owner !== 'object') return true; // The snapshot is server scoped.
    const identity = hooks.identity(state);
    return ['profile_id', 'task_id', 'dialogue_id', 'branch_id'].every(key =>
      owner[key] === undefined || text(owner[key]) === text(identity[key]));
  }

  function runs() { return list(state?.chat_evaluations).filter(belongs); }
  function currentRun() {
    const owned = runs();
    return owned.find(run => text(run.id) === text(selectedRunId)) || owned[owned.length - 1] || null;
  }

  function setRun(id) {
    selectedRunId = id === null || id === undefined ? null : text(id);
    if (activeOwner && selectedRunId) safeStorage.set(storageKey('run'), selectedRunId);
  }

  function questionKey(runId) { return storageKey('question:' + text(runId)); }
  function selectedQuestion(run) {
    const questions = list(run?.questions);
    const remembered = safeStorage.get(questionKey(run?.id));
    return questions.find(question => text(question.id) === remembered) ||
      questions.find(question => question.status === 'not_started') || questions[questions.length - 1] || null;
  }
  function setQuestion(runId, questionId) {
    if (activeOwner && runId && questionId) safeStorage.set(questionKey(runId), text(questionId));
  }

  function notice(message, error = false) {
    const target = $('#comparison-notice');
    if (target) { target.textContent = message; target.classList.toggle('chat-evaluation-error', error); }
  }

  function rememberDetails() {
    if (!activeOwner) return;
    document.querySelectorAll('#comparison-view details[data-eval-detail]').forEach(detail => {
      if (detail.dataset.ownerKey !== activeOwner) return;
      safeStorage.set(storageKey('detail:' + detail.dataset.evalDetail), detail.open ? '1' : '0');
    });
  }

  function detail(label, key, content, className = '') {
    const wrapper = node('details', className);
    wrapper.dataset.evalDetail = key;
    wrapper.dataset.ownerKey = activeOwner || '';
    wrapper.open = safeStorage.get(storageKey('detail:' + key)) === '1';
    wrapper.append(node('summary', '', label), content);
    wrapper.addEventListener('toggle', () => {
      if (wrapper.isConnected) safeStorage.set(storageKey('detail:' + key), wrapper.open ? '1' : '0');
    });
    return wrapper;
  }

  function labeled(label, value, className = '') {
    const item = node('div', className);
    item.append(node('dt', '', label), node('dd', '', value));
    return item;
  }

  function sourceNode(source, key, index) {
    const label = text(source?.label || source?.file || 'Фрагмент ' + (index + 1));
    const locator = ['Метка: ' + label, 'файл: ' + text(source?.file || 'не указан'),
      source?.line_start == null ? 'строки: не указаны' : 'строки ' + source.line_start
        + (source.line_end != null && source.line_end !== source.line_start ? '–' + source.line_end : '')]
      .filter(Boolean).join(' · ');
    const body = node('div', 'chat-evaluation-source-body');
    if (source?.score !== undefined && source.score !== null) body.append(node('p', 'field-note', 'Сходство: ' + source.score));
    body.append(node('pre', 'chat-evaluation-verbatim', source?.text ?? source?.excerpt ?? 'Текст фрагмента отсутствует.'));
    return detail(locator, key + ':source:' + index, body, 'chat-evaluation-source');
  }

  function usageLine(stage) {
    const tokenAccounting = stage?.token_accounting || {};
    const costSummary = stage?.summary || {};
    const token = tokenAccounting?.usage || tokenAccounting;
    const pieces = [];
    if (token?.input_tokens !== undefined || token?.known_input_tokens !== undefined)
      pieces.push('input ' + number(token.input_tokens ?? token.known_input_tokens));
    if (token?.output_tokens !== undefined || token?.known_output_tokens !== undefined)
      pieces.push('output ' + number(token.output_tokens ?? token.known_output_tokens));
    if (token?.total_tokens !== undefined || token?.known_total_tokens !== undefined)
      pieces.push('всего ' + number(token.total_tokens ?? token.known_total_tokens));
    const cost = costSummary.cost_usd ?? costSummary.known_cost_usd ?? stage?.cost_usd;
    const costIncomplete = costSummary.cost_complete === false || costSummary.unknown_cost_requests > 0;
    if (cost !== undefined) pieces.push((costIncomplete ? 'известная часть USD ' : 'USD ') + money(cost));
    if (costIncomplete) pieces.push('сумма неполная');
    if (tokenAccounting.complete === false || tokenAccounting.unknown_requests > 0) pieces.push('учёт токенов неполный');
    if (!pieces.length) pieces.push('данные расходов пока не сохранены');
    return pieces.join(' · ');
  }

  function receiptIds(stage) {
    return list(stage?.requests).map(item => typeof item === 'object' ? item?.id : item).filter(value => value !== null && value !== undefined).map(text);
  }

  function answerNode(question, mode, runId) {
    const stage = question?.pair?.[mode];
    const card = node('section', 'chat-evaluation-answer');
    card.dataset.mode = mode;
    card.dataset.questionId = text(question.id);
    const title = mode === 'rag' ? 'С RAG' : 'Без RAG';
    card.append(node('h4', '', title));
    if (!stage) {
      const waiting = ['pending', 'running'].includes(question?.status);
      const reason = question?.status === 'not_started' ? 'Ещё не запущен.'
        : waiting ? (mode === 'rag' ? 'Ожидает запуска после версии без RAG.' : 'Ожидает запуска.')
          : mode === 'rag' && question?.status === 'failed' && question?.pair?.plain?.status !== 'ok'
            ? 'Пара остановлена после ошибки версии без RAG.'
            : question?.status === 'interrupted' ? 'Прогон прерван до запуска этой версии.'
              : 'Эта версия не была запущена.';
      card.append(node('p', 'chat-evaluation-status', reason + ' API-вызов этой версии '
        + (waiting ? 'ещё не запускался.' : 'не запускался.')));
      if (question?.error) card.append(node('p', 'chat-evaluation-error', text(question.error)));
      return card;
    }
    card.append(node('p', 'chat-evaluation-status', 'Статус: ' + status(stage.status)
      + (stage.code ? ' · ' + text(stage.code) : '')
      + (stage.duration_ms == null ? '' : ' · ' + number(stage.duration_ms) + ' мс')));
    const fullText = text(stage.text);
    card.append(node('div', 'chat-evaluation-answer-text', fullText || (stage.status === 'ok'
      ? 'Принятый текст ответа отсутствует.' : 'Ответ пока не сохранён.')));
    if (stage.status !== 'ok' && !['pending', 'running'].includes(stage.status)) card.append(node('p', 'chat-evaluation-error',
      text(stage.error || stage.reason || stage.message || 'Версия не завершилась успешным ответом. Следующий этап не запускается автоматически.')));
    card.append(node('p', 'chat-evaluation-usage', 'Расходы этой версии: ' + usageLine(stage)));
    const technical = node('div', 'chat-evaluation-technical-content');
    const ids = receiptIds(stage);
    if (ids.length) {
      const buttons = node('div', 'chat-evaluation-receipts');
      for (const id of ids) {
        const button = node('button', 'small-button', 'Трасса вызова ' + id);
        button.type = 'button';
        button.dataset.chatEvaluationTrace = id;
        buttons.append(button);
      }
      technical.append(buttons);
    }
    if (mode === 'rag') {
      const sources = node('section', 'chat-evaluation-sources');
      sources.append(node('h5', '', 'Фактически переданные источники · ' + number(list(stage.sources).length)));
      if (!list(stage.sources).length) sources.append(node('p', 'field-note', 'Сохранённых фрагментов нет.'));
      list(stage.sources).forEach((source, index) => sources.append(sourceNode(source, runId + ':' + question.id + ':' + mode, index)));
      card.append(sources);
    }
    if (stage.context !== undefined && stage.context !== null) technical.append(detail('Сохранённый контекст',
      runId + ':' + question.id + ':' + mode + ':context', node('pre', 'chat-evaluation-json', json(stage.context))));
    if (stage.payloads !== undefined && stage.payloads !== null) technical.append(detail('Фактические payload модели',
      runId + ':' + question.id + ':' + mode + ':payloads', node('pre', 'chat-evaluation-json', json(stage.payloads))));
    if (stage.initial_context_sha256) technical.append(node('p', 'field-note', 'SHA-256 начального контекста: ' + stage.initial_context_sha256));
    card.append(detail('Технические данные', runId + ':' + question.id + ':' + mode + ':technical',
      technical, 'chat-evaluation-technical'));
    return card;
  }

  function assessmentNode(question) {
    const section = node('section', 'chat-evaluation-assessment');
    section.append(node('h4', '', 'Независимая оценка'));
    const assessment = question?.assessment;
    if (!assessment || typeof assessment !== 'object') {
      section.append(node('p', 'chat-evaluation-pending', 'Ожидает независимой оценки сохранённых ответов. Вердикт не присвоен.'));
      return section;
    }
    const facts = list(assessment.facts);
    if (facts.length) {
      const heading = node('h5', '', 'Проверка каждого факта');
      section.append(heading);
      facts.forEach((fact, index) => {
        const review = node('article', 'chat-evaluation-fact');
        const factText = list(question.expected_facts)[fact.fact_index] ?? list(question.expected_facts)[index] ?? 'Факт ' + (index + 1);
        review.append(node('h6', '', 'Факт ' + number((fact.fact_index ?? index) + 1) + ': ' + factText));
        review.append(node('p', '', 'Без RAG: ' + (fact.plain_correct === true ? 'верно' : fact.plain_correct === false ? 'неверно' : 'не оценено')
          + ' · ' + text(fact.plain_reason || 'Причина не указана.')));
        review.append(node('p', '', 'С RAG: ' + (fact.rag_correct === true ? 'верно' : fact.rag_correct === false ? 'неверно' : 'не оценено')
          + ' · поддержка: ' + (fact.rag_supported === true ? 'да' : fact.rag_supported === false ? 'нет' : 'не оценено')
          + ' · ' + text(fact.rag_reason || 'Причина не указана.')));
        if (list(fact.rag_source_labels).length) review.append(node('p', 'field-note',
          'Метки опоры: ' + fact.rag_source_labels.join(', ')));
        section.append(review);
      });
    }
    for (const mode of ['plain', 'rag']) {
      const result = assessment[mode];
      if (!result) continue;
      const card = node('article', 'chat-evaluation-verdict');
      card.append(node('h5', '', mode === 'plain' ? 'Вывод без RAG' : 'Вывод с RAG'));
      card.append(node('p', '', 'Полный ответ: ' + (result.full_answer === true ? 'да' : result.full_answer === false ? 'нет' : 'не оценено')));
      card.append(node('p', '', 'Отказ: ' + (result.abstained === true ? 'да' : result.abstained === false ? 'нет' : 'не оценено')));
      if (list(result.unsupported_claims).length) card.append(node('p', '', 'Неподтверждённые тезисы: ' + result.unsupported_claims.join('; ')));
      card.append(node('p', '', text(result.reason || 'Причина не указана.')));
      section.append(card);
    }
    section.append(node('p', '', 'Достаточность контекста: ' + (assessment.context_sufficient === true ? 'да' : assessment.context_sufficient === false ? 'нет' : 'не оценено')
      + ' · ' + text(assessment.context_reason || 'Причина не указана.')));
    section.append(node('p', 'chat-evaluation-conclusion', text(assessment.conclusion || 'Вывод не указан.')));
    return section;
  }

  function expectedNode(question) {
    const expected = node('section', 'chat-evaluation-expected');
    expected.append(node('p', 'chat-evaluation-disclaimer', 'Эталон для проверки. Эти сведения не передаются модели.'));
    if (question.unanswerable) expected.append(node('p', 'field-note', 'Контроль корректного отказа: ответа нет в базе.'));
    const facts = list(question.expected_facts);
    expected.append(node('h4', '', 'Ожидаемые факты'));
    const factList = node('ol');
    facts.forEach(fact => factList.append(node('li', '', fact)));
    expected.append(factList);
    expected.append(node('h4', '', 'Кандидаты источников'));
    const sourceList = node('ul');
    list(question.expected_sources).forEach(source => sourceList.append(node('li', '',
      text(source.file) + ' · ' + text(source.evidence))));
    expected.append(sourceList);
    return expected;
  }

  function questionNode(question, runId, mode) {
    const card = node('article', 'chat-evaluation-exchange');
    card.dataset.questionId = text(question.id);
    const user = node('div', 'chat-evaluation-user');
    user.append(node('small', '', 'Вы · ' + text(question.id).toUpperCase()), node('p', '', text(question.question)));
    card.append(user, answerNode(question, mode, runId));
    if (mode !== 'rag') return card;
    if (question.assessment?.conclusion) card.append(node('p', 'chat-evaluation-conclusion', text(question.assessment.conclusion)));
    card.append(detail('Эталон и кандидаты источников', runId + ':' + question.id + ':expected', expectedNode(question)));
    card.append(detail('Подробная независимая оценка', runId + ':' + question.id + ':assessment', assessmentNode(question)));
    return card;
  }

  function totalsNode(run) {
    const summary = node('section', 'chat-evaluation-summary');
    summary.append(node('h3', '', 'Итог контрольного прогона'));
    const completed = list(run.questions).filter(question => question?.pair?.plain?.status === 'ok' && question?.pair?.rag?.status === 'ok').length;
    summary.append(node('p', '', 'Пары с двумя принятыми ответами: ' + number(completed) + ' из ' + number(list(run.questions).length) + '.'));
    const assessed = list(run.questions).filter(question => question.assessment && typeof question.assessment === 'object').length;
    summary.append(node('p', '', 'Независимо оценено: ' + number(assessed) + ' из ' + number(list(run.questions).length) + '.'));
    summary.append(node('p', 'chat-evaluation-status', 'Прогон: ' + status(run.status) + ' · оценка: '
      + (assessed === list(run.questions).length && assessed > 0 ? 'завершена' : 'pending')));
    if (run.reviewer) summary.append(node('p', 'field-note', 'Оценщик: ' + json(run.reviewer)));
    if (run.totals && typeof run.totals === 'object') {
      const table = node('dl', 'chat-evaluation-totals');
      const names = {assessment:'Оценка',questions_complete:'Завершено вопросов',
        known_cost_usd:'Известная стоимость USD',cost_complete:'Стоимость полная',
        unknown_cost_requests:'Вызовов с неизвестной стоимостью',api_requests:'Вызовов API',
        plain_correct_facts:'Правильных фактов без RAG',rag_correct_facts:'Правильных фактов с RAG',
        rag_supported_facts:'Подтверждённых фактов RAG',plain_full_answers:'Полных ответов без RAG',
        rag_full_answers:'Полных ответов с RAG',plain_q10_abstained:'Отказ q10 без RAG',
        rag_q10_abstained:'Отказ q10 с RAG'};
      Object.entries(run.totals).forEach(([key, value]) => {
        const display = value && typeof value === 'object' ? json(value) : typeof value === 'boolean'
          ? value ? 'да' : 'нет' : key.includes('cost_usd') ? money(value) : text(value ?? 'неизвестно');
        table.append(labeled(names[key] || key, display));
      });
      summary.append(table);
    }
    if (run.gold_sha256 || run.evidence_sha256) summary.append(node('p', 'field-note',
      'Gold SHA-256: ' + text(run.gold_sha256 || '—') + '\nEvidence SHA-256: ' + text(run.evidence_sha256 || 'ещё нет')));
    return summary;
  }

  function render(source = state) {
    if (source) state = source;
    rememberDetails();
    const traceHost = $('#chat-evaluation-trace');
    if (traceHost?.parentElement !== view) { view.append(traceHost); traceHost.hidden = true; }
    const owned = runs();
    const selected = currentRun();
    view.dataset.ownerKey = activeOwner || '';
    view.dataset.runId = selected ? text(selected.id) : '';
    const select = $('#chat-evaluation-run-select');
    select.replaceChildren();
    if (!owned.length) select.append(new Option('Нет сохранённых прогонов', ''));
    owned.forEach((run, index) => select.append(new Option('Прогон ' + (index + 1) + ' · '
      + status(run.status) + ' · ' + text(run.created_at || run.id), text(run.id))));
    if (selected) select.value = text(selected.id);
    select.disabled = !owned.length || hooks?.busy() || !hooks?.ready();
    const questionSelect = $('#chat-evaluation-question-select');
    questionSelect.replaceChildren();
    const selectable = list(selected?.questions).filter(question => question.status !== 'not_started' || question.pair?.plain || question.pair?.rag);
    if (!selectable.length) questionSelect.append(new Option('Пока нет ответов', ''));
    selectable.forEach(question => questionSelect.append(new Option(
      text(question.id).toUpperCase() + ' · ' + text(question.question).slice(0, 65), text(question.id))));
    const rememberedQuestion = selected && selectedQuestion(selected);
    if (rememberedQuestion && selectable.some(question => text(question.id) === text(rememberedQuestion.id)))
      questionSelect.value = text(rememberedQuestion.id);
    questionSelect.disabled = !selectable.length || hooks?.busy() || !hooks?.ready();
    const rag = $('#comparison-rag');
    const plain = $('#comparison-plain');
    const runKey = activeOwner + ':' + text(selected?.id);
    const switchedRun = runKey !== renderedRunKey;
    renderedRunKey = runKey;
    const priorScroll = {rag: rag.scrollTop, plain: plain.scrollTop};
    const focused = document.activeElement;
    const focusedDetail = focused?.closest?.('details[data-eval-detail]')?.dataset.evalDetail;
    const focusedControl = focused?.id && focused.closest?.('#comparison-view') ? focused.id : null;
    rag.replaceChildren(); plain.replaceChildren();
    const report = $('#comparison-report');
    report.dataset.ownerKey = activeOwner || '';
    report.dataset.evalDetail = selected ? text(selected.id) + ':report' : 'empty:report';
    report.open = safeStorage.get(storageKey('detail:' + report.dataset.evalDetail)) === '1';
    if (!selected) {
      $('#comparison-progress').textContent = 'Получено 0/10 · оценено 0/10';
      $('#comparison-next').textContent = 'Прогон ещё не создан. Нажмите «Новый прогон».';
      $('#comparison-totals').replaceChildren(node('p', '', 'Пока нет сохранённого прогона.'));
      rag.append(node('p', 'chat-evaluation-pending', 'Пока нет вопросов с RAG.'));
      plain.append(node('p', 'chat-evaluation-pending', 'Пока нет вопросов без RAG.'));
      sync(state, hooks?.busy(), hooks?.ready());
      return view;
    }
    const questions = list(selected.questions);
    const received = questions.filter(question => question?.pair?.plain?.status === 'ok' && question?.pair?.rag?.status === 'ok').length;
    const assessed = questions.filter(question => question.assessment && typeof question.assessment === 'object').length;
    $('#comparison-progress').textContent = 'Получено ' + number(received) + '/10 · оценено ' + number(assessed) + '/10';
    const visible = questions.filter(question => question.status !== 'not_started' || question.pair?.plain || question.pair?.rag);
    for (const question of visible) {
      rag.append(questionNode(question, text(selected.id), 'rag'));
      plain.append(questionNode(question, text(selected.id), 'plain'));
    }
    if (!visible.length) {
      rag.append(node('p', 'chat-evaluation-pending', 'Отправьте первый контрольный вопрос.'));
      plain.append(node('p', 'chat-evaluation-pending', 'Здесь появится ответ без RAG.'));
    }
    const next = questions.find(question => question.status === 'not_started');
    $('#comparison-next').textContent = next ? 'Следующий: ' + text(next.id).toUpperCase() + ' · ' + text(next.question)
      : 'Все вопросы зарегистрированы. Неполные пары не повторяются автоматически.';
    const reportBody = node('div');
    reportBody.append(totalsNode(selected));
    questions.forEach(question => {
      const entry = node('section', 'chat-evaluation-question chat-evaluation-report-entry');
      entry.dataset.questionId = text(question.id);
      entry.append(node('h3', '', text(question.id).toUpperCase() + ' · ' + text(question.question)),
        expectedNode(question), assessmentNode(question));
      reportBody.append(entry);
    });
    $('#comparison-totals').replaceChildren(reportBody);
    rag.scrollTop = switchedRun ? 0 : priorScroll.rag;
    plain.scrollTop = switchedRun ? 0 : priorScroll.plain;
    if (switchedRun && rememberedQuestion && selectable.some(question => text(question.id) === text(rememberedQuestion.id)))
      requestAnimationFrame(() => jumpToQuestion(rememberedQuestion.id));
    if (focusedDetail) [...view.querySelectorAll('details[data-eval-detail]')]
      .find(item => item.dataset.evalDetail === focusedDetail)?.querySelector('summary')?.focus({preventScroll:true});
    else if (focusedControl) $('#' + focusedControl)?.focus({preventScroll:true});
    sync(state, hooks?.busy(), hooks?.ready());
    return view;
  }

  function sync(source, isBusy, isReady) {
    if (source) state = source;
    const nextOwner = ownerKey(state);
    const changedOwner = nextOwner !== activeOwner;
    if (changedOwner) {
      ownerRevision += 1;
      activeOwner = nextOwner;
      selectedRunId = nextOwner ? safeStorage.get(storageKey('run')) : null;
      $('#comparison-prompt').value = '';
      notice('');
      if ($('#trace-status')?.textContent?.startsWith('Контрольный вызов ')) {
        $('#trace-status').textContent = 'Выберите вызов';
        $('#trace-content')?.replaceChildren(node('p', 'empty-copy', 'Выберите вызов текущего диалога.'));
      }
    }
    const openButton = $('#chat-evaluation-open');
    const blocked = state?.task_state?.paused === true || state?.task_state?.stage === 'done';
    if (openButton) {
      openButton.disabled = Boolean(isBusy) || !isReady;
      openButton.setAttribute('aria-pressed', String(!view.hidden));
    }
    const controlsBlocked = Boolean(isBusy) || !isReady || !activeOwner || blocked;
    $('#chat-evaluation-run-select').disabled = Boolean(isBusy) || !isReady || !runs().length;
    $('#chat-evaluation-question-select').disabled = Boolean(isBusy) || !isReady ||
      !list(currentRun()?.questions).some(question => question.status !== 'not_started' || question.pair?.plain || question.pair?.rag);
    $('#chat-evaluation-new-run').disabled = controlsBlocked;
    $('#comparison-prompt').disabled = controlsBlocked || !currentRun();
    $('#chat-evaluation-insert-question').disabled = controlsBlocked ||
      !list(currentRun()?.questions).some(question => question.status === 'not_started');
    $('#comparison-send').disabled = controlsBlocked || !currentRun() || !$('#comparison-prompt').value.trim();
  }

  async function start() {
    if (!activeOwner || hooks.busy() || !hooks.ready()) return null;
    notice('Создаём новый прогон…');
    const response = await hooks.mutation('/api/rag/chat/start', hooks.identity(state),
      'Создаём контрольный прогон', '', {
        afterSuccess(body) {
          if (body.run?.id !== undefined) setRun(body.run.id);
          hooks.redraw();
        },
      });
    notice(response ? 'Прогон создан. Вставьте первый вопрос.' : ($('#notice')?.textContent || 'Не удалось создать прогон.'), !response);
    if (response && window.matchMedia('(max-width: 700px)').matches) $('#comparison-controls').open = false;
    return response?.run || null;
  }

  function show(open, persist = true, focus = true) {
    if (open) window.digestChat?.show(false, false, false);
    active = open;
    view.hidden = !open;
    $('#regular-workspace').hidden = open;
    $('#chat-evaluation-open').setAttribute('aria-pressed', String(open));
    document.querySelector('.skip-link').setAttribute('href', open ? '#comparison-prompt' : '#prompt');
    document.querySelector('.app-shell').classList.remove('chats-mobile-open');
    document.querySelectorAll('[data-shell-toggle="chats"]').forEach(item => item.setAttribute('aria-expanded', 'false'));
    if (persist) localStorage.setItem('graphics-view', open ? 'comparison' : 'regular');
    if (open && focus) { $('#comparison-title').setAttribute('tabindex', '-1'); $('#comparison-title').focus(); }
    sync(state, hooks?.busy(), hooks?.ready());
  }

  async function submit(prompt) {
    if (!active || pendingSubmit || hooks.busy() || !hooks.ready()) return false;
    const exact = text(prompt).trim();
    if (!exact) return false;
    const run = currentRun();
    if (!run) { notice('Сначала создайте новый прогон.', true); return false; }
    const next = list(run.questions).find(question => question.status === 'not_started');
    if (!next) {
      notice('В этом прогоне все вопросы зарегистрированы. Создайте новый прогон для нового набора.', true);
      return false;
    }
    if (exact !== next.question) {
      notice('Контрольный вопрос должен буквально совпадать с ' + text(next.id).toUpperCase() + '. Вставьте следующий вопрос кнопкой.', true);
      return false;
    }
    pendingSubmit = true;
    const submittedOwner = activeOwner;
    const submittedRevision = ownerRevision;
    notice('Агенты готовят два независимых ответа…');
    try {
      const body = await hooks.mutation('/api/rag/chat/question', {
        ...hooks.identity(state), run_id: run.id, question_id: next.id, prompt: exact,
      }, 'Агент выполняет plain и RAG', '', {scroll:false});
      if (!body) { notice($('#notice')?.textContent || 'Не удалось получить пару ответов. Обновите состояние перед повтором.', true); return false; }
      if (submittedOwner !== activeOwner || submittedRevision !== ownerRevision) return false;
      setQuestion(run.id, next.id);
      hooks.redraw();
      $('#comparison-prompt').value = '';
      sync(state, hooks.busy(), hooks.ready());
      notice('Контрольная пара сохранена. Расходы включены в диалог.');
      requestAnimationFrame(() => jumpToQuestion(next.id));
      return true;
    } finally { pendingSubmit = false; }
  }

  function jumpToQuestion(questionId) {
    for (const lane of ['rag', 'plain']) {
      const feed = $('#comparison-' + lane);
      const target = [...feed.querySelectorAll('.chat-evaluation-exchange[data-question-id]')]
        .find(item => item.dataset.questionId === text(questionId));
      if (target) feed.scrollTop += target.getBoundingClientRect().top - feed.getBoundingClientRect().top;
    }
  }

  async function trace(receiptId, trigger = null) {
    if (!activeOwner || hooks.busy() || !hooks.ready()) return;
    const startedOwner = activeOwner;
    const startedRevision = ownerRevision;
    const startedRunId = text(currentRun()?.id);
    const receipt = list(state?.chat_evaluation_requests).find(item => text(item.id) === text(receiptId));
    if (!receipt || receipt.metadata?.redacted === true) {
      hooks.notice('Вызов не принадлежит текущему диалогу. Обновите состояние.', 'error');
      return;
    }
    const button = trigger?.dataset?.chatEvaluationTrace === text(receiptId)
      ? trigger
      : [...document.querySelectorAll('[data-chat-evaluation-trace]')]
        .find(item => item.dataset.chatEvaluationTrace === text(receiptId));
    if (button) button.disabled = true;
    try {
      const response = await fetch('/api/rag/chat/trace/' + slug(receiptId), {cache:'no-store'});
      const body = await response.json();
      if (startedOwner !== activeOwner || startedRevision !== ownerRevision || hooks.busy()
          || (button && !button.isConnected) ||
          (button?.closest('#comparison-view') && startedRunId !== text(currentRun()?.id))
          || !list(state?.chat_evaluation_requests).some(item => text(item.id) === text(receiptId)
            && item.metadata?.redacted !== true)) return;
      if (!response.ok || body.status !== 'ok') throw new Error(text(body.error || body.text || 'Трасса недоступна.'));
      const inExpense = button?.closest('#request-list');
      const host = inExpense ? $('#trace-content') : $('#chat-evaluation-trace');
      if (!host) throw new Error('Панель трассы недоступна.');
      if (!inExpense) button?.closest('.chat-evaluation-technical-content')?.append(host);
      host.replaceChildren(node('h3', '', 'Контекст контрольного вызова ' + receiptId),
        node('pre', 'chat-evaluation-json', json(body.trace ?? body)));
      host.hidden = false;
      if (inExpense) {
        $('#trace-status').textContent = 'Контрольный вызов ' + receiptId;
        window.openWorkspacePanel?.('usage');
      }
      host.scrollIntoView({block:'nearest'});
    } catch (error) {
      if (startedOwner === activeOwner && startedRevision === ownerRevision && !hooks.busy())
        hooks.notice(error instanceof TypeError ? 'Не удалось загрузить трассу.' : error.message, 'error');
    } finally { if (button?.isConnected) button.disabled = false; }
  }

  function configure(value) {
    hooks = value;
    document.addEventListener('focusin', event => {
      const key = event.target.closest?.('#comparison-view details[data-eval-detail]')?.dataset.evalDetail;
      if (key && activeOwner) safeStorage.set(storageKey('focus'), key);
    });
    view.addEventListener('toggle', event => {
      const detail = event.target.closest?.('details[data-eval-detail]');
      if (detail && activeOwner) safeStorage.set(storageKey('detail:' + detail.dataset.evalDetail), detail.open ? '1' : '0');
    }, true);
    document.addEventListener('click', event => {
      if (event.target.closest('#chat-evaluation-open')) { show(true); return; }
      if (event.target.closest('#comparison-refresh')) { $('#state-refresh')?.click(); return; }
      if (event.target.closest('#chat-evaluation-new-run')) { start(); return; }
      if (event.target.closest('#chat-evaluation-close, #comparison-back')) {
        show(false); return;
      }
      if (event.target.closest('#chat-evaluation-insert-question')) {
        const run = currentRun();
        const next = list(run?.questions).find(question => question.status === 'not_started');
        const prompt = $('#comparison-prompt');
        if (!next || !prompt || hooks.busy() || !hooks.ready()) return;
        setQuestion(run.id, next.id);
        prompt.value = text(next.question);
        sync(state, hooks.busy(), hooks.ready());
        prompt.focus({preventScroll:true});
        return;
      }
      const traceButton = event.target.closest('[data-chat-evaluation-trace]');
      if (traceButton) trace(traceButton.dataset.chatEvaluationTrace, traceButton);
      if (event.target.closest('.dialogue-item, [data-panel="create"]')) show(false);
    });
    document.addEventListener('change', event => {
      if (event.target.id === 'chat-evaluation-run-select') {
        setRun(event.target.value); render();
        if (window.matchMedia('(max-width: 700px)').matches) $('#comparison-controls').open = false;
      }
      if (event.target.id === 'chat-evaluation-question-select') {
        const run = currentRun();
        if (run) {
          setQuestion(run.id, event.target.value); jumpToQuestion(event.target.value);
          if (window.matchMedia('(max-width: 700px)').matches) $('#comparison-controls').open = false;
        }
      }
      if (event.target.id === 'comparison-mobile-mode') view.dataset.mobileMode = event.target.value;
    });
    $('#comparison-prompt').addEventListener('input', () => sync(state, hooks.busy(), hooks.ready()));
    $('#comparison-prompt').addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!$('#comparison-send').disabled) $('#comparison-form').requestSubmit();
      }
    });
    $('#comparison-form').addEventListener('submit', event => { event.preventDefault(); submit($('#comparison-prompt').value); });
    view.dataset.mobileMode = $('#comparison-mobile-mode').value;
    const mobile = window.matchMedia('(max-width: 700px)');
    const syncDisclosures = () => {
      $('#comparison-controls').open = !mobile.matches;
      $('#comparison-context').open = !mobile.matches;
    };
    mobile.addEventListener('change', syncDisclosures);
    syncDisclosures();
    if (localStorage.getItem('graphics-view') === 'comparison') show(true, false, false);
  }

  window.chatEvaluation = {configure, render, sync, start, submit, trace, show,
    isActive: () => !view.hidden};
})();
