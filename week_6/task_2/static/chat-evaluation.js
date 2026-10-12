'use strict';

// The service owns questions, results and assessment. This view presents its saved snapshot.
(() => {
  const $ = selector => document.querySelector(selector);
  const node = (tag, cls = '', value) => {
    const item = document.createElement(tag);
    if (cls) item.className = cls;
    if (value !== undefined) item.textContent = String(value);
    return item;
  };
  const list = value => Array.isArray(value) ? value : [];
  const text = value => value == null ? '' : String(value);
  const modes = {rag:'RAG',filter:'RAG + фильтр',rewrite:'Переформулировка',rewrite_filter:'Переформулировка + фильтрация'};
  const status = value => ({ok:'готово',no_context:'контекст не найден',rejected:'отклонено',
    error:'ошибка',failed:'ошибка',interrupted:'прервано',running:'выполняется',
    complete:'завершено',assessed:'оценено',not_started:'не начато'}[value] || text(value) || 'ожидает');
  const storage = {
    get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* unavailable */ } },
  };
  let hooks;
  let state;
  let owner = null;
  let ownerRevision = 0;
  let active = false;
  let runId = null;
  let questionId = null;
  let mobileMode = 'rag';
  let pendingSubmit = false;

  function ownerKey(source = state) {
    const identity = hooks?.identity(source) || {};
    const values = [identity.profile_id, identity.task_id, identity.dialogue_id, identity.branch_id];
    return values.every(value => value != null && value !== '') ? values.map(text).join(':') : null;
  }
  const key = suffix => 'chat-evaluation:' + owner + ':' + suffix;
  function belongs(run) {
    if (!owner) return false;
    const identity = hooks.identity(state);
    return !run?.owner || ['profile_id','task_id','dialogue_id','branch_id'].every(field =>
      run.owner[field] == null || text(run.owner[field]) === text(identity[field]));
  }
  const runs = () => list(state?.chat_evaluations).filter(belongs);
  const selectedRun = () => runs().find(run => text(run.id) === runId) || runs().at(-1) || null;
  const runModes = run => list(run?.modes).length ? list(run.modes) :
    Number(run?.version) === 2 ? ['rag','rewrite','filter','rewrite_filter'] : ['rag','filter'];
  const legacy = run => Number(run?.version) === 2;
  function setRun(value) {
    runId = text(value) || null;
    if (owner && runId) storage.set(key('run'), runId);
    questionId = null;
  }
  function rememberScroll() {
    if (!owner) return;
    for (const mode of ['rag','filter']) {
      const feed = $('#chat-evaluation-feed-' + mode);
      if (feed) storage.set(key('scroll:' + text(selectedRun()?.id) + ':' + mode), String(feed.scrollTop));
    }
  }
  function restoreScroll(run) {
    if (!run) return;
    for (const mode of ['rag','filter']) {
      const feed = $('#chat-evaluation-feed-' + mode);
      const saved = Number(storage.get(key('scroll:' + text(run.id) + ':' + mode)));
      if (feed && Number.isFinite(saved) && saved >= 0) feed.scrollTop = saved;
    }
  }
  function sourceNode(source, detailKey) {
    const detail = node('details', 'chat-evaluation-source');
    detail.open = storage.get(key('source:' + detailKey)) === '1';
    const locator = [source?.label || 'Источник', source?.file || 'локальный документ',
      source?.line_start == null ? null : 'строки ' + source.line_start
        + (source.line_end != null && source.line_end !== source.line_start ? '–' + source.line_end : '')]
      .filter(Boolean).join(' · ');
    detail.append(node('summary', '', locator),
      node('pre', 'chat-evaluation-verbatim', source?.text ?? source?.excerpt ?? 'Текст фрагмента отсутствует.'));
    detail.addEventListener('toggle', () => {
      if (detail.isConnected) storage.set(key('source:' + detailKey), detail.open ? '1' : '0');
    });
    return detail;
  }
  function answerNode(question, mode, runId) {
    const stage = question?.pair?.[mode];
    const card = node('article', 'chat-evaluation-answer');
    card.dataset.mode = mode;
    card.append(node('p', 'chat-evaluation-question-label', text(question.id).toUpperCase() + ' · ' + text(question.question)));
    if (!stage) {
      card.append(node('p', 'chat-evaluation-status', 'Ответ ещё не получен.'));
      return card;
    }
    if (stage.status !== 'ok') card.append(node('p', 'chat-evaluation-status', 'Состояние: ' + status(stage.status)));
    const answer = stage.status === 'ok' ? text(stage.text) : '';
    card.append(node('div', 'chat-evaluation-answer-text', answer || (stage.status === 'no_context'
      ? 'Релевантный контекст не найден. Генерация не запускалась.'
      : text(stage.error || stage.reason || stage.message || 'Ответ не опубликован.'))));
    const sources = node('section', 'chat-evaluation-sources');
    sources.append(node('h4', '', 'Фактически переданные источники · ' + list(stage.sources).length));
    if (!list(stage.sources).length) sources.append(node('p', 'field-note', 'Сохранённых источников нет.'));
    list(stage.sources).forEach((source,index) => sources.append(sourceNode(source,
      text(runId) + ':' + text(question.id) + ':' + mode + ':' + index)));
    card.append(sources);
    return card;
  }
  function lane(run, mode) {
    const section = node('section', 'chat-evaluation-lane');
    section.dataset.mode = mode;
    section.append(node('h3', '', modes[mode]), node('div', 'chat-evaluation-feed'));
    const feed = section.querySelector('.chat-evaluation-feed');
    feed.id = 'chat-evaluation-feed-' + mode;
    feed.tabIndex = 0;
    feed.setAttribute('aria-label', 'История ' + modes[mode]);
    const visible = list(run.questions).filter(question => question.pair?.[mode] || text(question.id) === questionId);
    if (visible.length) visible.forEach(question => {
      const answer = answerNode(question, mode, run.id);
      answer.dataset.questionId = text(question.id);
      if (text(question.id) === questionId) answer.classList.add('is-selected');
      feed.append(answer);
    });
    else feed.append(node('p', 'field-note', 'Ответов пока нет.'));
    if (mode !== mobileMode) section.classList.add('mobile-inactive');
    return section;
  }
  function report(run) {
    const details = node('details', 'chat-evaluation-report');
    details.id = 'chat-evaluation-report';
    details.open = storage.get(key('report:' + text(run.id))) === '1';
    details.append(node('summary', '', 'Отдельный отчёт: качество и расходы'));
    const body = node('div', 'chat-evaluation-report-body');
    body.append(node('p', '', 'Статус прогона: ' + status(run.status) + '. Независимая оценка: '
      + (run.reviewer ? 'сохранена' : 'ожидается') + '.'));
    body.append(node('p', 'field-note', 'Набор: ' + list(run.questions).length
      + ' фиксированных вопросов учебной выборки. Оценка относится к этим вопросам и сохранённому состоянию базы; она не доказывает улучшение на других вопросах.'));
    body.append(node('p', 'field-note',
      'Полный контроль альтернативных настроек в этом отдельном прогоне не выполнен. Для выбора конфигурации сопоставляйте независимые прогоны с одинаковыми условиями.'));
    const totals = run.totals || {};
    const overall = node('dl', 'chat-evaluation-totals');
    for (const [label, field] of [['Известная стоимость USD','known_cost_usd'],['Стоимость полная','cost_complete'],
      ['Вызовов с неизвестной стоимостью','unknown_cost_requests'],['Вызовов API','api_requests'],
      ['Завершено вопросов','questions_complete']]) {
      const row = node('div');
      row.append(node('dt', '', label), node('dd', '', field === 'cost_complete'
        ? (totals[field] === true ? 'да' : totals[field] === false ? 'нет' : 'неизвестно')
        : text(totals[field] ?? 'неизвестно')));
      overall.append(row);
    }
    body.append(overall);
    for (const mode of runModes(run)) {
      const summary = totals.modes?.[mode] || {};
      const section = node('section', 'chat-evaluation-mode-summary');
      section.append(node('h4', '', modes[mode] || mode));
      const metrics = node('dl', 'chat-evaluation-totals');
      for (const [label, field] of [['Полных ответов','full_answers'],['Верных фактов','correct_facts'],
        ['Фактов с опорой','supported_facts'],['Кандидатов достаточно','candidate_sufficient'],
        ['Контекста достаточно','context_sufficient'],['Отказ на q10','q10_abstained'],
        ['Тезисов без опоры','unsupported_claims'],['Без контекста','no_context'],
        ['Ошибок','error'],['Отклонено','rejected'],['Вызовов API','api_requests'],
        ['Время, мс','duration_ms'],['Известная стоимость USD','known_cost_usd']]) {
        if (summary[field] == null) continue;
        const value = typeof summary[field] === 'boolean' ? (summary[field] ? 'да' : 'нет') : text(summary[field]);
        const row = node('div'); row.append(node('dt', '', label), node('dd', '', value)); metrics.append(row);
      }
      section.append(metrics); body.append(section);
    }
    if (totals.paired && typeof totals.paired === 'object') {
      const paired = node('section', 'chat-evaluation-paired');
      paired.append(node('h4', '', 'Парные изменения относительно RAG'));
      for (const [mode, comparison] of Object.entries(totals.paired)) {
        paired.append(node('p', '', (modes[mode] || mode) + ': Выигрыш — '
          + (list(comparison?.gains).join(', ') || 'нет') + '. Потеря — '
          + (list(comparison?.losses).join(', ') || 'нет') + '.'));
      }
      body.append(paired);
    }
    for (const question of list(run.questions)) {
      const assessment = question.assessment;
      if (!assessment?.modes) continue;
      const item = node('details', 'chat-evaluation-assessment');
      item.append(node('summary', '', text(question.id).toUpperCase() + ' · независимая оценка'));
      for (const mode of runModes(run)) {
        const verdict = assessment.modes[mode];
        if (!verdict) continue;
        const block = node('section', 'chat-evaluation-verdict');
        block.append(node('h5', '', modes[mode] || mode), node('p', '', 'Полный ответ: '
          + (verdict.full_answer === true ? 'да' : verdict.full_answer === false ? 'нет' : 'не оценено')));
        block.append(node('p', '', 'Отказ: '
          + (verdict.abstained === true ? 'да' : verdict.abstained === false ? 'нет' : 'не оценено')));
        block.append(node('p', '', 'Неподтверждённые тезисы: '
          + (list(verdict.unsupported_claims).join('; ') || 'нет')));
        block.append(node('p', '', 'Кандидаты достаточны: '
          + (verdict.candidate_sufficient === true ? 'да' : verdict.candidate_sufficient === false ? 'нет' : 'не оценено')
          + '. ' + text(verdict.candidate_reason || 'Причина не сохранена.')));
        block.append(node('p', '', 'Контекст достаточен: '
          + (verdict.context_sufficient === true ? 'да' : verdict.context_sufficient === false ? 'нет' : 'не оценено')
          + '. ' + text(verdict.context_reason || 'Причина не сохранена.')));
        for (const fact of list(verdict.facts)) block.append(node('p', '', 'Факт ' + (Number(fact.fact_index) + 1)
          + ': ' + text(list(question.expected_facts)[fact.fact_index] || 'формулировка недоступна')
          + ': верность ' + (fact.correct ? 'да' : 'нет') + ', опора ' + (fact.supported ? 'да' : 'нет')
          + '. ' + text(fact.reason)));
        block.append(node('p', '', text(verdict.reason))); item.append(block);
      }
      item.append(node('p', 'chat-evaluation-conclusion', text(assessment.conclusion || 'Вывод оценки не сохранён.')));
      body.append(item);
    }
    if (legacy(run)) {
      const archive = node('details', 'chat-evaluation-archive');
      archive.append(node('summary', '', 'Все четыре архивных результата'));
      for (const question of list(run.questions)) {
        const section = node('section', 'chat-evaluation-archive-question');
        section.append(node('h4', '', text(question.id).toUpperCase() + ' · ' + text(question.question)));
        for (const mode of runModes(run)) section.append(answerNode(question, mode, run.id));
        archive.append(section);
      }
      body.append(archive);
    }
    details.append(body);
    details.addEventListener('toggle', () => {
      if (details.isConnected) storage.set(key('report:' + text(run.id)), details.open ? '1' : '0');
    });
    return details;
  }
  function render(source = state) {
    if (source) state = source;
    const host = $('#chat-evaluation-content');
    if (!host) return;
    const oldRunId = host.querySelector('.chat-evaluation-run')?.dataset.runId;
    const draft = $('#chat-evaluation-prompt')?.value || '';
    if (!owner) { host.replaceChildren(node('p', 'field-note', 'Выберите диалог для сравнения.')); return; }
    if (oldRunId && oldRunId === text(selectedRun()?.id)) rememberScroll();
    const wrapper = node('div', 'chat-evaluation-run');
    wrapper.dataset.ownerKey = owner;
    const selected = selectedRun();
    if (selected) wrapper.dataset.runId = text(selected.id);
    const toolbar = node('div', 'chat-evaluation-toolbar');
    const controls = node('div', 'chat-evaluation-run-controls');
    const selectLabel = node('label', '', 'Сохранённый прогон');
    const select = node('select'); select.id = 'chat-evaluation-run-select';
    if (!runs().length) select.append(new Option('Нет сохранённых прогонов', ''));
    runs().forEach((run, index) => select.append(new Option('Прогон ' + (index + 1)
      + (legacy(run) ? ' · архив, четыре режима' : ' · RAG / RAG + фильтр')
      + ' · ' + status(run.status), text(run.id))));
    if (selected) select.value = text(selected.id);
    select.disabled = !runs().length || hooks.busy() || !hooks.ready();
    selectLabel.append(select);
    const fresh = node('button', 'small-button', 'Новый прогон');
    fresh.type = 'button'; fresh.id = 'chat-evaluation-new-run';
    fresh.disabled = hooks.busy() || !hooks.ready();
    const refresh = node('button', 'small-button', 'Обновить сохранённое');
    refresh.type = 'button'; refresh.id = 'chat-evaluation-refresh';
    refresh.disabled = hooks.busy();
    const expense = node('button', 'small-button', 'Расходы и политики');
    expense.type = 'button'; expense.id = 'chat-evaluation-expense';
    controls.append(selectLabel, fresh, refresh, expense); toolbar.append(controls); wrapper.append(toolbar);
    if (!selected) wrapper.append(node('p', 'chat-evaluation-pending', 'Прогон ещё не создан. Создание не обращается к модели.'));
    else {
      const old = legacy(selected);
      if (old) wrapper.append(node('p', 'chat-evaluation-legacy',
        'Архивный прогон с четырьмя режимами. Два чата доступны для просмотра; продолжить и отправить вопрос нельзя. Полный отчёт сохранён ниже.'));
      const questions = list(selected.questions);
      const next = questions.find(question => question.status === 'not_started');
      const chosen = questions.find(question => text(question.id) === questionId)
        || next || questions.at(-1) || null;
      questionId = chosen ? text(chosen.id) : null;
      const picker = node('div', 'chat-evaluation-pickers');
      const questionLabel = node('label', '', 'Вопрос в двух чатах');
      const questionSelect = node('select'); questionSelect.id = 'chat-evaluation-question-select';
      questions.forEach(question => questionSelect.append(new Option(text(question.id).toUpperCase()
        + ' · ' + text(question.question), text(question.id))));
      if (chosen) questionSelect.value = text(chosen.id);
      questionLabel.append(questionSelect);
      const mobileLabel = node('label', 'chat-evaluation-mobile-picker', 'Мобильный чат');
      const mobile = node('select'); mobile.id = 'chat-evaluation-mobile-mode';
      for (const mode of ['rag','filter']) mobile.append(new Option(modes[mode], mode));
      mobile.value = mobileMode; mobileLabel.append(mobile);
      picker.append(questionLabel, mobileLabel); wrapper.append(picker);
      const lanes = node('div', 'chat-evaluation-lanes');
      lanes.append(lane(selected,'rag'), lane(selected,'filter')); wrapper.append(lanes);
      if (!old) {
        const composer = node('form', 'chat-evaluation-composer'); composer.id = 'chat-evaluation-form';
        const label = node('label', '', next ? 'Следующий фиксированный вопрос · ' + text(next.id).toUpperCase()
          : 'Все вопросы прогона зарегистрированы');
        label.htmlFor = 'chat-evaluation-prompt';
        const prompt = node('textarea'); prompt.id = 'chat-evaluation-prompt'; prompt.maxLength = 12000;
        if (oldRunId === text(selected.id)) prompt.value = draft;
        prompt.placeholder = 'Введите точный следующий вопрос или вставьте его кнопкой';
        prompt.disabled = !next || hooks.busy() || !hooks.ready();
        const insert = node('button', 'small-button', 'Вставить следующий вопрос');
        insert.type = 'button'; insert.id = 'chat-evaluation-insert'; insert.disabled = prompt.disabled;
        const send = node('button', 'send-button', 'Отправить в оба чата ↗');
        send.type = 'submit'; send.id = 'chat-evaluation-send'; send.disabled = !prompt.value.trim() || prompt.disabled;
        composer.append(label, prompt, insert, send,
          node('p', 'field-note', 'Отправка выполняет два режима и расходует API. Сохранённые ответы открываются без повторного вызова.'));
        wrapper.append(composer);
      }
      wrapper.append(report(selected));
    }
    host.replaceChildren(wrapper); restoreScroll(selected);
  }
  function showQuestion() {
    for (const mode of ['rag','filter']) {
      const feed = $('#chat-evaluation-feed-' + mode);
      const card = [...(feed?.querySelectorAll('.chat-evaluation-answer') || [])]
        .find(item => item.dataset.questionId === questionId);
      if (!feed || !card) continue;
      feed.scrollTop += card.getBoundingClientRect().top - feed.getBoundingClientRect().top - 16;
    }
    rememberScroll();
  }
  function sync(source, isBusy, isReady) {
    if (source) state = source;
    const nextOwner = ownerKey(state);
    if (nextOwner !== owner) {
      rememberScroll(); owner = nextOwner; ownerRevision++;
      active = Boolean(owner && storage.get(key('active')) === '1');
      runId = owner ? storage.get(key('run')) : null; questionId = null;
      if ($('#trace-status')?.textContent?.startsWith('Контрольный вызов ')) {
        $('#trace-status').textContent = 'Выберите вызов';
        $('#trace-content')?.replaceChildren(node('p', 'empty-copy', 'Выберите вызов текущего диалога.'));
      }
    }
    $('#chat-evaluation-view').hidden = !active;
    $('#chat-evaluation-open').setAttribute('aria-pressed', String(active));
    $('#chat-evaluation-open').disabled = Boolean(isBusy) || !isReady || !owner;
    $('#regular-workspace').hidden = active || !$('#digest-chat-view').hidden;
    if (active) render();
    const fresh = $('#chat-evaluation-new-run');
    if (fresh) fresh.disabled = Boolean(isBusy) || !isReady;
  }
  async function start() {
    if (!owner || hooks.busy() || !hooks.ready()) return null;
    const body = await hooks.mutation('/api/rag/chat/start', hooks.identity(state),
      'Создаём прогон', 'Прогон создан. Вопросы отправляются только по вашему действию.', {
        afterSuccess(reply) { if (reply.run?.id != null) setRun(reply.run.id); hooks.redraw(); },
      });
    return body?.run || null;
  }
  async function open() {
    if (!owner || hooks.busy() || !hooks.ready()) return;
    window.digestChat?.show(false);
    active = true; storage.set(key('active'), '1');
    $('#regular-workspace').hidden = true; $('#chat-evaluation-view').hidden = false;
    $('.skip-link')?.setAttribute('href', '#chat-evaluation-prompt');
    hooks.redraw(); hooks.controls();
    if (!selectedRun()) await start();
    $('#chat-evaluation-title')?.focus();
  }
  function close() {
    if (!active) return;
    rememberScroll(); active = false; if (owner) storage.set(key('active'), '0');
    $('#chat-evaluation-view').hidden = true;
    $('#chat-evaluation-notice').textContent = '';
    $('#regular-workspace').hidden = !$('#digest-chat-view').hidden;
    $('#chat-evaluation-open').setAttribute('aria-pressed', 'false');
    $('.skip-link')?.setAttribute('href', '#prompt'); hooks.controls();
  }
  async function submit() {
    if (!active || pendingSubmit || hooks.busy() || !hooks.ready()) return false;
    const run = selectedRun();
    if (!run || legacy(run)) return false;
    const next = list(run.questions).find(question => question.status === 'not_started');
    const prompt = $('#chat-evaluation-prompt')?.value.trim();
    if (!next || !prompt) return false;
    if (prompt !== next.question) {
      hooks.notice('Текст должен буквально совпадать со следующим вопросом ' + text(next.id).toUpperCase() + '.', 'error');
      return false;
    }
    pendingSubmit = true;
    try {
      const body = await hooks.mutation('/api/rag/chat/question', {
        ...hooks.identity(state),run_id:run.id,question_id:next.id,prompt,
      }, 'Сравниваем RAG и RAG + фильтр', '', {scroll:false});
      if (!body) return false;
      questionId = text(next.id);
      hooks.notice('Оба результата сохранены. Расходы доступны в общей панели.', 'success');
      hooks.redraw(); return true;
    } finally { pendingSubmit = false; }
  }
  async function trace(receiptId, trigger) {
    if (!owner || hooks.busy() || !hooks.ready()) return;
    const revision = ownerRevision;
    const savedOwner = owner;
    const receipt = list(state?.chat_evaluation_requests).find(item => text(item.id) === text(receiptId));
    if (!receipt || receipt.metadata?.redacted === true) return;
    trigger.disabled = true;
    try {
      const response = await fetch('/api/rag/chat/trace/' + encodeURIComponent(text(receiptId)), {cache:'no-store'});
      const body = await response.json();
      if (revision !== ownerRevision || savedOwner !== owner || hooks.busy()
        || !list(state?.chat_evaluation_requests).some(item => text(item.id) === text(receiptId)
          && item.metadata?.redacted !== true)) return;
      if (!response.ok || body.status !== 'ok') throw new Error(text(body.error || body.text || 'Трасса недоступна.'));
      $('#trace-content').replaceChildren(node('h3', '', 'Контекст контрольного вызова ' + receiptId),
        node('pre', 'chat-evaluation-json', JSON.stringify(body.trace ?? body, null, 2)));
      $('#trace-status').textContent = 'Контрольный вызов ' + receiptId;
      window.openWorkspacePanel?.('usage');
    } catch (error) {
      if (revision === ownerRevision && savedOwner === owner) hooks.notice(error.message, 'error');
    } finally { if (trigger.isConnected) trigger.disabled = false; }
  }
  function configure(value) {
    hooks = value;
    document.addEventListener('workspace-notice', event => {
      const notice = $('#chat-evaluation-notice');
      if (notice) {
        notice.textContent = active ? text(event.detail?.text) : '';
        notice.className = event.detail?.type ? 'notice ' + event.detail.type : 'notice';
      }
    });
    document.addEventListener('click', event => {
      if (event.target.closest('#chat-evaluation-open')) { open(); return; }
      if (event.target.closest('#chat-evaluation-close')) { close(); return; }
      if (event.target.closest('#chat-evaluation-new-run')) { start(); return; }
      if (event.target.closest('#chat-evaluation-refresh')) { hooks.refresh(); return; }
      if (event.target.closest('#chat-evaluation-expense')) { window.openWorkspacePanel?.('usage'); return; }
      if (event.target.closest('#chat-evaluation-insert')) {
        const next = list(selectedRun()?.questions).find(question => question.status === 'not_started');
        const prompt = $('#chat-evaluation-prompt');
        if (next && prompt) { prompt.value = next.question; prompt.dispatchEvent(new Event('input',{bubbles:true})); prompt.focus(); }
        return;
      }
      const traceButton = event.target.closest('#request-list [data-chat-evaluation-trace]');
      if (traceButton) trace(traceButton.dataset.chatEvaluationTrace, traceButton);
      if (event.target.closest('.dialogue-item, [data-panel="create"]')) close();
    });
    document.addEventListener('change', event => {
      if (event.target.id === 'chat-evaluation-run-select') { rememberScroll(); setRun(event.target.value); render(); }
      if (event.target.id === 'chat-evaluation-question-select') { rememberScroll(); questionId = event.target.value; render(); showQuestion(); }
      if (event.target.id === 'chat-evaluation-mobile-mode') { mobileMode = event.target.value; render(); }
    });
    document.addEventListener('input', event => {
      if (event.target.id === 'chat-evaluation-prompt') $('#chat-evaluation-send').disabled =
        !event.target.value.trim() || hooks.busy() || !hooks.ready();
    });
    document.addEventListener('submit', event => {
      if (event.target.id === 'chat-evaluation-form') { event.preventDefault(); submit(); }
    });
    $('#chat-evaluation-content')?.addEventListener('scroll', event => {
      if (event.target.classList?.contains('chat-evaluation-feed')) rememberScroll();
    }, true);
  }
  window.chatEvaluation = {configure,render,sync,start,submit,trace,open,close,isActive:() => active};
})();
