(() => {
  const $ = id => document.getElementById(id);
  const str = value => value === null || value === undefined ? 'неизвестно' : String(value);
  const number = value => value === null || value === undefined ? 'неизвестно' : Number(value).toLocaleString('ru-RU');
  const money = value => value === null || value === undefined ? 'неизвестно' : '$' + Number(value).toFixed(8);
  const seconds = value => value === null || value === undefined ? 'неизвестно' : Number(value).toFixed(2);
  const node = (tag, text, className) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = str(text); if (className) el.className = className; return el; };
  const append = (parent, tag, text, className) => { const el = node(tag, text, className); parent.append(el); return el; };
  const cell = (row, value) => append(row, 'td', value);
  const sessionKey = 'day22-rag-session-id';
  let sessionId = localStorage.getItem(sessionKey);
  if (!sessionId || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sessionId)) {
    sessionId = crypto.randomUUID(); localStorage.setItem(sessionKey, sessionId);
  }
  let busy = false;

  async function api(path, options) {
    const response = await fetch(path, {cache: 'no-store', ...options});
    let data;
    try { data = await response.json(); } catch { throw new Error('Сервер вернул ответ без JSON.'); }
    return {ok: response.ok, data};
  }
  function setError(message) { $('error').textContent = message || ''; $('error').hidden = !message; }
  function setBusy(value) { busy = value; $('ask').disabled = value; $('compare').disabled = value; $('mode').disabled = value; $('question').disabled = value; $('status').textContent = value ? 'Ожидаем фактический ответ API…' : 'Готово. Сессия сохранена локально.'; }

  function sourceDetails(parent, source) {
    const details = append(parent, 'details');
    append(details, 'summary', `${str(source.label)} · ${str(source.file)} · строки ${str(source.line_start)}–${str(source.line_end)}`);
    const body = append(details, 'div', undefined, 'source-card');
    append(body, 'p', `Файл: ${str(source.file)} · Раздел: ${str(source.section)} · Источник: ${str(source.source)} · Заголовок: ${str(source.title)}`, 'metadata');
    append(body, 'p', `SHA-256 документа: ${str(source.document_hash)} · cosine score: ${str(source.score)} (не вероятность) · chunk_id: ${str(source.chunk_id)}`, 'metadata');
    append(body, 'pre', source.text ?? '', 'exact-text');
  }
  function renderAnswer(target, result) {
    target.replaceChildren();
    if (!result) { append(target, 'p', 'Этот режим ещё не запускался.', 'muted'); return; }
    append(target, 'p', `Статус: ${str(result.status)} · ${str(result.code || 'без ошибки')} · ${seconds(result.elapsed_seconds)} с`, 'metadata');
    if (result.input_policy || result.output_policy) append(target, 'p', `Input policy: ${str(result.input_policy?.code)} · output policy: ${str(result.output_policy?.code)}`, 'metadata');
    append(target, 'p', result.text || (result.status === 'ok' ? 'Пустой ответ.' : 'Ответ не получен.'), 'answer-text');
    const usage = result.usage || {};
    append(target, 'p', `Токены: input ${number(usage.input_tokens)}, output ${number(usage.output_tokens)}, total ${number(usage.total_tokens)} · запрос ${money(result.cost_usd)}`, 'metadata');
    const sources = Array.isArray(result.sources) ? result.sources : [];
    if (sources.length) {
      const details = append(target, 'details'); append(details, 'summary', `Переданные источники: ${sources.length}`);
      for (const source of sources) sourceDetails(details, source);
    }
    if (result.context) {
      const details = append(target, 'details'); append(details, 'summary', 'Точный контекст генерации');
      append(details, 'pre', result.context, 'exact-text');
      const budget = result.context_budget || {};
      append(details, 'p', `Лимит ${number(budget.limit_tokens)} токенов; использовано ${number(budget.used_utf8_bytes)} UTF-8 байт как консервативная верхняя граница, фактическое число токенов не измерено.`, 'metadata');
    }
  }
  function renderState(state) {
    const cumulative = state.cumulative || {};
    $('cumulative').textContent = `Известная стоимость: ${money(cumulative.known_cost_usd)} · сумма ${cumulative.complete ? 'полная' : 'неполная'} · вызовов с неизвестной стоимостью: ${number(cumulative.unknown_calls)} · сессия ${str(state.session_id)}`;
    const requests = Array.isArray(state.requests) ? state.requests : [];
    const body = $('ledger-body'); body.replaceChildren(); $('ledger-empty').hidden = requests.length > 0;
    for (const record of requests) {
      for (const stage of Array.isArray(record.stages) ? record.stages : []) {
        const usage = stage.usage || {};
        const row = append(body, 'tr');
        cell(row, `${str(record.mode)} / ${str(stage.kind)}`);
        const statusCell = cell(row, `${str(stage.status)} · input ${str(stage.input_policy?.code)} · output ${str(stage.output_policy?.code)}`);
        if (stage.tariff || stage.model) {
          const details = append(statusCell, 'details'); append(details, 'summary', 'Модель и тариф');
          append(details, 'p', `${str(stage.model)} · ${str(stage.service_tier)} · ${str(stage.tariff?.source)} · проверен ${str(stage.tariff?.verified_on)}`, 'metadata');
        }
        cell(row, stage.api_called ? 'да' : 'нет');
        cell(row, `${number(usage.input_tokens)} / ${number(usage.cached_input_tokens)}`);
        cell(row, `${number(usage.output_tokens)} / ${number(usage.reasoning_tokens)}`);
        cell(row, number(usage.total_tokens)); cell(row, money(stage.cost_usd)); cell(row, seconds(stage.elapsed_seconds));
      }
    }
    const latest = requests.at(-1);
    if (latest) {
      const comparison = latest.comparison_id ? requests.filter(item => item.comparison_id === latest.comparison_id) : [latest];
      $('result-question').textContent = latest.question;
      renderAnswer($('plain-result'), comparison.find(item => item.mode === 'plain'));
      renderAnswer($('rag-result'), comparison.find(item => item.mode === 'rag'));
    }
  }
  async function refreshState() {
    const result = await api(`/api/rag/state?session_id=${encodeURIComponent(sessionId)}`);
    if (!result.ok) throw new Error(result.data.text || 'Не удалось загрузить сессию.');
    renderState(result.data);
  }
  function renderQuestions(data) {
    const list = $('questions'); list.replaceChildren();
    const questions = Array.isArray(data.questions) ? data.questions : [];
    for (const item of questions) {
      const box = append(list, 'article', undefined, 'question-item');
      const button = append(box, 'button', `${str(item.id)} · ${str(item.question)}`); button.type = 'button';
      button.addEventListener('click', () => { $('question').value = item.question; $('question').focus(); window.scrollTo({top: 0, behavior: 'smooth'}); });
      const details = append(box, 'details'); append(details, 'summary', 'Ожидание и источники');
      append(details, 'p', item.unanswerable ? 'Вопрос без ответа в корпусе: корректно признать отсутствие точных данных.' : 'Ожидаемые атомарные факты:');
      const facts = append(details, 'ul'); for (const fact of item.expected_facts || []) append(facts, 'li', fact);
      append(details, 'p', 'Ожидаемые кандидаты источников:');
      const sources = append(details, 'ul'); for (const source of item.expected_sources || []) append(sources, 'li', `${str(source.file)} — ${str(source.evidence)}`);
    }
  }
  function assessmentText(value) {
    if (!value || value === 'pending') return 'Ожидает содержательной оценки';
    const facts = `${number(value.correct_facts)} / ${number(value.total_facts)} фактов`;
    const unsupported = Array.isArray(value.unsupported_claims) ? value.unsupported_claims.length : 'неизвестно';
    return `${facts} · полный ответ: ${str(value.full_answer)} · неподтверждённых тезисов: ${unsupported} · отказ корректен: ${str(value.abstention_correct)}`;
  }
  function reportAnswer(cellNode, result, grade) {
    append(cellNode, 'p', assessmentText(grade));
    if (result) {
      append(cellNode, 'p', `Статус ${str(result.status)} · ${seconds(result.elapsed_seconds)} с · ${money(result.cost_usd)}`, 'metadata');
      const details = append(cellNode, 'details'); append(details, 'summary', 'Полный фактический ответ'); append(details, 'pre', result.text || '', 'exact-text');
    }
    if (grade && Array.isArray(grade.fact_scores)) {
      const details = append(cellNode, 'details'); append(details, 'summary', 'Причины оценки');
      for (const fact of grade.fact_scores) append(details, 'p', `${str(fact.fact)} · ${str(fact.score)} · ${str(fact.reason)}`);
    }
    if (grade && Array.isArray(grade.unsupported_claims) && grade.unsupported_claims.length) {
      const details = append(cellNode, 'details'); append(details, 'summary', `Неподтверждённые тезисы: ${grade.unsupported_claims.length}`);
      for (const item of grade.unsupported_claims) {
        const claim = typeof item === 'string' ? item : `${str(item.claim ?? item.text)} · ${str(item.reason)}`;
        append(details, 'p', claim);
      }
    }
  }
  function renderEvaluation(report) {
    const body = $('evaluation-body'); body.replaceChildren();
    const items = Array.isArray(report.questions) ? report.questions : [];
    $('evaluation-status').textContent = report.status === 'pending' ? 'Живой прогон ещё не сохранён; содержательная оценка ожидается.' : `Прогон: ${str(report.status)} · вопросов с сохранёнными ответами: ${items.length} / 10`;
    const summary = report.summary || {}, grade = summary.assessment;
    $('evaluation-summary').textContent = grade && grade !== 'pending' ? `Полных ответов: без поиска ${number(grade.plain_full)}, с поиском ${number(grade.rag_full)}; фактов ${number(grade.plain_correct_facts)} → ${number(grade.rag_correct_facts)} из ${number(grade.total_facts)}; неподтверждённых тезисов RAG ${number(grade.rag_unsupported_claims)}; корректных отказов RAG ${number(grade.rag_abstention_correct)}. Основа оценки: ${str(grade.quality_basis)}. Известная стоимость набора ${money(summary.known_cost_usd)}, сумма ${summary.complete ? 'полная' : 'неполная'}.` : 'Содержательная оценка ожидается.';
    for (const item of items) {
      const row = append(body, 'tr'); cell(row, `${str(item.id)} · ${str(item.question)}`);
      const assessment = item.assessment && item.assessment !== 'pending' ? item.assessment : {};
      reportAnswer(append(row, 'td'), item.plain, assessment.plain);
      reportAnswer(append(row, 'td'), item.rag, assessment.rag);
      const retrieval = append(row, 'td');
      const proxy = assessment.retrieval;
      append(retrieval, 'p', proxy ? `Имя файла, proxy: ${str(proxy.source_recall_proxy)} · контекст достаточен: ${str(proxy.sufficient_context)}. ${str(proxy.reason)}` : 'Ожидает оценки поиска');
      if (item.retrieval) {
        const details = append(retrieval, 'details'); append(details, 'summary', 'Фактические источники и контекст');
        for (const source of item.retrieval.sources || []) sourceDetails(details, source);
        append(details, 'pre', item.retrieval.context || '', 'exact-text');
      }
    }
  }
  async function refreshEvaluation() {
    const result = await api('/api/rag/evaluation');
    if (!result.ok) throw new Error(result.data.text || 'Оценка недоступна.');
    renderEvaluation(result.data);
  }
  async function submit(compare) {
    if (busy) return;
    const question = $('question').value.trim();
    if (!question) { setError('Введите вопрос.'); $('question').focus(); return; }
    setError(''); setBusy(true);
    try {
      const endpoint = compare ? '/api/rag/compare' : '/api/rag/ask';
      const payload = compare ? {question, session_id: sessionId} : {question, mode: $('mode').value, session_id: sessionId};
      const {data, ok} = await api(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      if (data.plain || data.rag) {
        $('result-question').textContent = question;
        renderAnswer($('plain-result'), data.plain); renderAnswer($('rag-result'), data.rag);
      } else if (data.request_id) {
        $('result-question').textContent = question;
        renderAnswer($(data.mode === 'plain' ? 'plain-result' : 'rag-result'), data);
      }
      if (!ok) setError(data.text || data.rag?.text || data.plain?.text || 'Запрос не завершён.');
      await refreshState();
    } catch (error) { setError(error.message || 'Связь с сервером нарушена.'); }
    finally { setBusy(false); }
  }
  $('rag-form').addEventListener('submit', event => { event.preventDefault(); submit(false); });
  $('compare').addEventListener('click', () => submit(true));
  $('refresh-evaluation').addEventListener('click', () => refreshEvaluation().catch(error => setError(error.message)));
  Promise.allSettled([refreshState(), api('/api/rag/questions').then(result => { if (!result.ok) throw new Error(result.data.text); renderQuestions(result.data); }), refreshEvaluation()])
    .then(results => { const failures = results.filter(item => item.status === 'rejected'); if (failures.length) setError(failures.map(item => item.reason.message).join(' ')); $('status').textContent = 'Готово. Сессия сохранена локально.'; });
})();
