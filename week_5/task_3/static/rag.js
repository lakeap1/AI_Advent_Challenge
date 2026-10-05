(() => {
  const $ = id => document.getElementById(id);
  const modes = ['rag', 'rewrite', 'filter', 'rewrite_filter'];
  const labels = {plain: 'Без поиска', rag: 'Без rewrite и фильтра', rewrite: 'Только rewrite', filter: 'Только фильтр', rewrite_filter: 'Rewrite + фильтр'};
  const decisions = {selected: 'в контексте', below_threshold: 'ниже порога', overlap: 'перекрытие', context_budget: 'лимит контекста', top_k: 'лимит top-K'};
  const str = value => value === null || value === undefined ? 'неизвестно' : String(value);
  const number = value => value === null || value === undefined ? 'неизвестно' : Number(value).toLocaleString('ru-RU');
  const money = value => value === null || value === undefined ? 'неизвестно' : '$' + Number(value).toFixed(8);
  const rate = value => (typeof value !== 'number' && typeof value !== 'string') || value === '' || !Number.isFinite(Number(value)) || Number(value) < 0 ? 'неизвестно' : `$${String(value)} / 1M токенов`;
  const seconds = value => value === null || value === undefined ? 'неизвестно' : Number(value).toFixed(2);
  const node = (tag, value, className) => { const el = document.createElement(tag); if (value !== undefined) el.textContent = str(value); if (className) el.className = className; return el; };
  const append = (parent, tag, value, className) => { const el = node(tag, value, className); parent.append(el); return el; };
  const cell = (row, value) => append(row, 'td', value);
  const sessionKey = 'day23-rag-session-id';
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
  function setBusy(value) {
    busy = value;
    for (const id of ['ask', 'compare', 'mode', 'question']) $(id).disabled = value;
    $('status').textContent = value ? 'Ожидаем фактические ответы API…' : 'Готово. Сессия сохранена локально.';
  }
  function disclosure(parent, title) { const details = append(parent, 'details'); append(details, 'summary', title); return details; }
  function sourceDetails(parent, source) {
    const details = disclosure(parent, `${str(source.label || source.chunk_id)} · ${str(source.file)} · строки ${str(source.line_start)}–${str(source.line_end)}`);
    const body = append(details, 'div', undefined, 'source-card');
    append(body, 'p', `Файл: ${str(source.file)} · Раздел: ${str(source.section)} · Источник: ${str(source.source)} · Заголовок: ${str(source.title)}`, 'metadata');
    append(body, 'p', `SHA-256: ${str(source.document_hash)} · cosine: ${str(source.score)} · релевантность 0–3: ${str(source.relevance_score)} · chunk_id: ${str(source.chunk_id)}`, 'metadata');
    append(body, 'pre', source.text ?? '', 'exact-text');
  }
  function renderRetrieval(parent, retrieval) {
    if (!retrieval) return;
    append(parent, 'p', `Исходный вопрос: ${str(retrieval.original_query)}`, 'metadata');
    append(parent, 'p', `Поисковый запрос: ${str(retrieval.search_query)} · rewrite: ${retrieval.rewrite_applied ? 'да' : 'нет'} · фильтр: ${retrieval.filter_applied ? 'да' : 'нет'}`, 'metadata');
    const counts = retrieval.counts || {};
    append(parent, 'p', `Кандидатов ${number(counts.candidates)} · прошли порог ${number(counts.passed)} · переданы в контекст ${number(counts.selected)} · top-K ${number(retrieval.top_k_before)} → ${number(retrieval.top_k_after)} · порог ${number(retrieval.relevance_threshold)} / 3`, 'metadata');
    const candidates = Array.isArray(retrieval.candidates) ? retrieval.candidates : [];
    if (candidates.length) {
      const details = disclosure(parent, `Отбор кандидатов: ${candidates.length}`);
      for (const candidate of candidates) {
        const item = disclosure(details, `${str(candidate.rank)}. ${str(candidate.file)} · cosine ${str(candidate.score)} · оценка ${str(candidate.relevance_score)} / 3 · ${decisions[candidate.decision] || str(candidate.decision)}`);
        append(item, 'p', `Причина: ${str(candidate.reason)} · chunk_id: ${str(candidate.chunk_id)}`, 'metadata');
        sourceDetails(item, candidate);
      }
    }
  }
  function renderAnswer(target, result) {
    target.replaceChildren();
    if (!result) { append(target, 'p', 'Этот режим ещё не запускался.', 'muted'); return; }
    append(target, 'p', `Статус: ${str(result.status)} · ${str(result.code || 'без ошибки')} · ${seconds(result.elapsed_seconds)} с`, 'metadata');
    if (result.input_policy || result.output_policy) append(target, 'p', `Input policy: ${str(result.input_policy?.code)} · output policy: ${str(result.output_policy?.code)}`, 'metadata');
    append(target, 'p', result.text || (result.status === 'no_context' ? 'Подходящий контекст не найден; генерация не запускалась.' : 'Ответ не получен.'), 'answer-text');
    const usage = result.usage || result.usage_known || {};
    append(target, 'p', `Токены${result.usage ? '' : result.usage_known ? ' (известная часть)' : ' (неизвестны)'}: input ${number(usage.input_tokens)}, output ${number(usage.output_tokens)}, total ${number(usage.total_tokens)} · запрос ${money(result.cost_usd)}`, 'metadata');
    renderRetrieval(target, result.retrieval);
    const sources = Array.isArray(result.sources) ? result.sources : [];
    if (sources.length) {
      const details = disclosure(target, `Переданные источники: ${sources.length}`);
      for (const source of sources) sourceDetails(details, source);
    }
    if (result.context) {
      const details = disclosure(target, 'Точный контекст генерации');
      append(details, 'pre', result.context, 'exact-text');
      const budget = result.context_budget || {};
      append(details, 'p', `Лимит ${number(budget.limit_tokens)} UTF-8 байт; использовано ${number(budget.used_utf8_bytes)} байт. Это верхняя граница токенов, фактическое число токенов контекста не измерено.`, 'metadata');
    }
  }
  function renderResults(question, results) {
    $('result-question').textContent = question || 'Пока нет запроса.';
    for (const mode of [...modes, 'plain']) renderAnswer($(`${mode}-result`), results[mode]);
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
        cell(row, `${labels[record.mode] || str(record.mode)} / ${str(stage.kind)}`);
        const status = cell(row, `${str(stage.status)} · input ${str(stage.input_policy?.code)} · output ${str(stage.output_policy?.code)}`);
        if (stage.tariff || stage.model) {
          const details = disclosure(status, 'Модель и тариф');
          append(details, 'p', `${str(stage.model)} · ${str(stage.service_tier)} · ${str(stage.tariff?.source)} · проверен ${str(stage.tariff?.verified_on)}`, 'metadata');
          append(details, 'p', `Ввод: ${rate(stage.tariff?.input_usd_per_million)} · cached input: ${rate(stage.tariff?.cached_input_usd_per_million)} · cache write: ${rate(stage.tariff?.cache_write_usd_per_million)} · вывод: ${rate(stage.tariff?.output_usd_per_million)}`, 'metadata');
        }
        cell(row, stage.api_called ? 'да' : 'нет');
        cell(row, `${number(usage.input_tokens)} / ${number(usage.cached_input_tokens)}`);
        cell(row, `${number(usage.output_tokens)} / ${number(usage.reasoning_tokens)}`);
        cell(row, number(usage.total_tokens)); cell(row, money(stage.cost_usd)); cell(row, seconds(stage.elapsed_seconds));
      }
    }
    const latest = requests.at(-1);
    if (latest) {
      const group = latest.comparison_id ? requests.filter(item => item.comparison_id === latest.comparison_id) : [latest];
      renderResults(latest.question, Object.fromEntries(group.map(item => [item.mode, item])));
    }
  }
  async function refreshState() {
    const result = await api(`/api/rag/state?session_id=${encodeURIComponent(sessionId)}`);
    if (!result.ok) throw new Error(result.data.text || 'Не удалось загрузить сессию.');
    renderState(result.data);
  }
  function renderConfig(config) {
    const retrieval = config.retrieval || config;
    $('rag-config').textContent = `Кандидатов до отбора: ${number(retrieval.top_k_before)} · источников после отбора: ${number(retrieval.top_k_after)} · порог: ${number(retrieval.relevance_threshold)} / 3 · контекст: ${number(retrieval.context_budget_utf8_bytes ?? retrieval.context_budget_bytes ?? retrieval.max_context_tokens)} UTF-8 байт. Настройки задаются в config.toml сервера.`;
  }
  function renderQuestions(data) {
    const list = $('questions'); list.replaceChildren();
    for (const item of Array.isArray(data.questions) ? data.questions : []) {
      const box = append(list, 'article', undefined, 'question-item');
      const button = append(box, 'button', `${str(item.id)} · ${str(item.question)}`); button.type = 'button';
      button.addEventListener('click', () => { $('question').value = item.question; $('question').focus(); window.scrollTo({top: 0, behavior: 'smooth'}); });
      const details = disclosure(box, 'Ожидаемые факты и источники');
      append(details, 'p', item.unanswerable ? 'В корпусе нет достаточных сведений: корректно признать это.' : 'Ожидаемые факты:');
      const facts = append(details, 'ul'); for (const fact of item.expected_facts || []) append(facts, 'li', fact);
      const sources = append(details, 'ul'); for (const source of item.expected_sources || []) append(sources, 'li', `${str(source.file)} — ${str(source.evidence)}`);
    }
  }
  function assessmentText(grade) {
    if (!grade || grade === 'pending') return 'Ожидает содержательной оценки';
    return `${number(grade.correct_facts)} / ${number(grade.total_facts)} фактов · полный ответ: ${str(grade.full_answer)} · подтверждено: ${number(grade.supported_facts)} · неподтверждённых тезисов: ${Array.isArray(grade.unsupported_claims) ? grade.unsupported_claims.length : 'неизвестно'} · отказ корректен: ${grade.abstention_correct === null ? 'неприменимо' : str(grade.abstention_correct)}`;
  }
  function reportAnswer(target, result, grade, retrievalGrade) {
    append(target, 'p', assessmentText(grade));
    if (!result) { append(target, 'p', 'Режим не завершён.', 'muted'); return; }
    append(target, 'p', `Статус ${str(result.status)} · ${seconds(result.elapsed_seconds)} с · ${money(result.cost_usd)}`, 'metadata');
    const answer = disclosure(target, 'Полный сохранённый ответ'); append(answer, 'pre', result.text || '', 'exact-text');
    const trace = disclosure(target, 'Источники, запрос и контекст'); renderRetrieval(trace, result.retrieval);
    for (const source of result.sources || []) sourceDetails(trace, source);
    append(trace, 'pre', result.context || '', 'exact-text');
    if (grade && Array.isArray(grade.fact_scores)) {
      const reasons = disclosure(target, 'Причины оценки');
      for (const fact of grade.fact_scores) append(reasons, 'p', `${str(fact.fact)} · ${str(fact.score)} · ${str(fact.reason)}`);
      for (const claim of grade.unsupported_claims || []) append(reasons, 'p', typeof claim === 'string' ? claim : `${str(claim.claim ?? claim.text)} · ${str(claim.reason)}`);
    }
    if (retrievalGrade && retrievalGrade !== 'pending') append(target, 'p', `Достаточность кандидатов: ${str(retrievalGrade.candidate_sufficient)} · контекста: ${str(retrievalGrade.context_sufficient)} · релевантных источников ${number(retrievalGrade.relevant_sources)} / ${number(retrievalGrade.total_sources)}. ${str(retrievalGrade.reason)}`, 'metadata');
  }
  function renderEvaluation(report) {
    const body = $('evaluation-body'); body.replaceChildren();
    const items = report.version === 2 && Array.isArray(report.questions) ? report.questions : [];
    $('evaluation-status').textContent = report.version !== 2 && report.status !== 'pending' ? 'Найден прежний формат отчёта; четырёхрежимная оценка ожидается.' : report.status === 'pending' ? 'Живой прогон ещё не сохранён; оценка ожидается.' : `Прогон: ${str(report.status)} · сохранено вопросов: ${items.length} / 10`;
    const summary = report.summary || {}, grade = summary.assessment;
    $('evaluation-config').textContent = report.config ? `Конфигурация прогона: ${JSON.stringify(report.config)}` : 'Конфигурация прогона пока не сохранена.';
    if (grade && grade !== 'pending' && grade.modes) {
      const parts = modes.map(mode => { const item = grade.modes[mode] || {}; return `${labels[mode]}: ${number(item.correct_facts)} / ${number(item.total_facts)} фактов, полных ответов ${number(item.full_answers)}, подтверждённых фактов ${number(item.supported_facts)}, достаточных контекстов ${number(item.sufficient_contexts)}, корректных отказов ${number(item.correct_abstentions)}, ${seconds(item.mean_seconds)} с, ${money(item.cost_usd)}`; });
      $('evaluation-summary').textContent = `${parts.join(' · ')}. Основа: ${str(grade.scope)}. Известная стоимость набора ${money(summary.known_cost_usd)}, сумма ${summary.complete ? 'полная' : 'неполная'}, неизвестных вызовов ${number(summary.unknown_calls)}.`;
    } else $('evaluation-summary').textContent = `Содержательная оценка ожидается. Известная стоимость набора ${money(summary.known_cost_usd)}; сумма ${summary.complete ? 'полная' : 'неполная'}; неизвестных вызовов ${number(summary.unknown_calls)}.`;
    for (const item of items) {
      const row = append(body, 'tr'); cell(row, `${str(item.id)} · ${str(item.question)}`);
      const assessment = item.assessment && item.assessment !== 'pending' ? item.assessment : {};
      for (const mode of modes) reportAnswer(append(row, 'td'), item.results?.[mode], assessment.modes?.[mode], assessment.retrieval?.[mode]);
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
      if (data.results) renderResults(question, data.results);
      else if (data.mode) renderResults(question, {[data.mode]: data});
      if (!ok) setError(data.text || Object.values(data.results || {}).find(item => item?.status === 'error' || item?.status === 'rejected')?.text || 'Запрос не завершён.');
      await refreshState();
    } catch (error) { setError(error.message || 'Связь с сервером нарушена.'); }
    finally { setBusy(false); }
  }
  $('rag-form').addEventListener('submit', event => { event.preventDefault(); submit(false); });
  $('compare').addEventListener('click', () => submit(true));
  $('refresh-evaluation').addEventListener('click', () => refreshEvaluation().catch(error => setError(error.message)));
  Promise.allSettled([
    refreshState(),
    api('/api/rag/questions').then(result => { if (!result.ok) throw new Error(result.data.text); renderQuestions(result.data); }),
    api('/api/rag/config').then(result => { if (!result.ok) throw new Error(result.data.text); renderConfig(result.data); }),
    refreshEvaluation()
  ]).then(results => { const errors = results.filter(item => item.status === 'rejected'); if (errors.length) setError(errors.map(item => item.reason.message).join(' ')); $('status').textContent = 'Готово. Сессия сохранена локально.'; });
})();
