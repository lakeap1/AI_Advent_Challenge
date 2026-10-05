// Synthetic browser fixture: no external model API is called.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const { chromium } = require(process.env.PLAYWRIGHT_NODE_MODULES || 'playwright');
const modes = ['rag', 'rewrite', 'filter', 'rewrite_filter'];

function html(name) {
  return fs.readFileSync(path.join(root, 'templates', name), 'utf8')
    .replaceAll("{{ url_for('static', filename='style.css') }}", '/static/style.css')
    .replaceAll("{{ url_for('static', filename='rag.css') }}", '/static/rag.css')
    .replaceAll("{{ url_for('static', filename='rag.js') }}", '/static/rag.js')
    .replaceAll("{{ url_for('indexing_page') }}", '/indexing')
    .replaceAll("{{ url_for('rag_page') }}", '/rag');
}
async function main() {
  const questions = JSON.parse(fs.readFileSync(path.join(root, 'evaluation/questions.json'), 'utf8'));
  const requests = [];
  const source = {label: 'S1', file: 'alpha.md', source: 'fixture', title: 'Alpha', section: 'A', line_start: 1, line_end: 2, document_hash: 'abc', score: 0.8, relevance_score: 3, chunk_id: 'abc:1', text: '<img src=x onerror=alert(1)> exact text'};
  const candidate = {...source, rank: 1, reason: 'Exact <script> evidence', decision: 'selected'};
  const trace = mode => ({original_query: 'Почему?', search_query: mode.includes('rewrite') ? 'Уточнённый <script> запрос' : 'Почему?', rewrite_applied: mode.includes('rewrite'), filter_applied: mode.includes('filter'), top_k_before: 20, top_k_after: 6, relevance_threshold: 2, candidates: [candidate], counts: {candidates: 1, passed: 1, selected: 1}, elapsed_seconds: 0.3});
  const result = (mode, status = 'ok') => ({status, text: status === 'ok' ? `${mode}: <script>window.pwned=1</script> полный ответ [S1].` : 'Подходящий контекст не найден.', code: '', request_id: `${mode}-id`, mode, sources: status === 'ok' ? [source] : [], context: status === 'ok' ? '[S1] <img src=x onerror=alert(1)>' : '', context_budget: {limit_tokens: 6000, used_utf8_bytes: 34}, retrieval: trace(mode), usage: {input_tokens: 20, cached_input_tokens: 2, output_tokens: 8, reasoning_tokens: 1, total_tokens: 28}, cost_usd: mode === 'filter' ? null : 0.0001, elapsed_seconds: 0.6, input_policy: {code: 'accepted'}, output_policy: {code: 'accepted'}});
  const stage = (kind, cost) => ({kind, status: 'ok', api_called: true, cost_usd: cost, usage: {input_tokens: 20, cached_input_tokens: 2, output_tokens: 8, reasoning_tokens: 1, total_tokens: 28}, input_policy: {code: 'accepted'}, output_policy: {code: 'accepted'}, model: 'fixture', tariff: kind === 'query_embedding' ? {source: 'fixture', verified_on: '2026-10-03', input_usd_per_million: '0.02'} : {source: 'fixture', verified_on: '2026-10-03', input_usd_per_million: '0.10', cached_input_usd_per_million: '0.05', cache_write_usd_per_million: '0.04', output_usd_per_million: '0.20'}});
  let session = '', calls = 0, showAssessed = false, release;
  const pending = new Promise(resolve => { release = resolve; });
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const send = (code, data, type = 'application/json') => { res.writeHead(code, {'Content-Type': type}); res.end(type === 'application/json' ? JSON.stringify(data) : data); };
    if (url.pathname === '/rag') return send(200, html('rag.html'), 'text/html');
    if (url.pathname === '/') return send(200, html('index.html'), 'text/html');
    if (url.pathname === '/indexing') return send(200, html('indexing.html'), 'text/html');
    if (url.pathname.startsWith('/static/')) return send(200, fs.readFileSync(path.join(root, url.pathname)), url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript');
    if (url.pathname === '/api/rag/questions') return send(200, questions);
    if (url.pathname === '/api/rag/config') return send(200, {retrieval: {top_k_before: 20, top_k_after: 6, relevance_threshold: 2, context_budget_utf8_bytes: 6000}});
    if (url.pathname === '/api/rag/evaluation') {
      if (!showAssessed) return send(200, {version: 2, status: 'failed', questions: [{id: 'q01', question: questions.questions[0].question, results: {rag: result('rag'), rewrite: null, filter: null, rewrite_filter: null}, assessment: 'pending'}], summary: {assessment: 'pending', known_cost_usd: 0.0001, complete: false, unknown_calls: 1}, config: {top_k_before: 20}});
      const grades = Object.fromEntries(modes.map(mode => [mode, {correct_facts: 2, total_facts: 2, full_answer: true, supported_facts: 2, abstention_correct: false, fact_scores: [{fact: 'Факт', score: 1, reason: 'Exact retrieved line.'}], unsupported_claims: [{claim: 'Invented role', reason: 'Absent from source.'}]}]));
      const retrieval = Object.fromEntries(modes.map(mode => [mode, {candidate_sufficient: true, context_sufficient: true, relevant_sources: 1, total_sources: 1, reason: 'Exact source.'}]));
      return send(200, {version: 2, status: 'complete', questions: [{id: 'q01', question: questions.questions[0].question, results: Object.fromEntries(modes.map(mode => [mode, result(mode)])), assessment: {modes: grades, retrieval}}], summary: {assessment: {modes: Object.fromEntries(modes.map(mode => [mode, {correct_facts: 2, total_facts: 2, full_answers: 1, supported_facts: 2, sufficient_contexts: 1, correct_abstentions: 0, mean_seconds: 0.6, cost_usd: 0.0001}])), scope: 'Independent fixture'}, known_cost_usd: 0.0004, complete: true, unknown_calls: 0}, config: {top_k_before: 20}});
    }
    if (url.pathname === '/api/rag/state') return send(200, {session_id: url.searchParams.get('session_id'), requests, cumulative: {known_cost_usd: 0.0003, complete: false, unknown_calls: 1}});
    if (url.pathname === '/api/rag/compare') {
      let raw = ''; for await (const chunk of req) raw += chunk;
      const value = JSON.parse(raw); session = value.session_id; calls++;
      if (calls === 1) await pending;
      const results = calls === 1 ? {rag: result('rag'), rewrite: result('rewrite'), filter: result('filter', 'no_context'), rewrite_filter: result('rewrite_filter')} : {rag: result('rag'), rewrite: {status: 'error', text: 'Сбой rewrite', mode: 'rewrite'}, filter: null, rewrite_filter: null};
      for (const mode of modes) if (results[mode]) requests.push({...results[mode], question: value.question, session_id: session, comparison_id: `comparison-${calls}`, stages: [stage(mode.includes('rewrite') ? 'query_rewrite' : 'query_embedding', results[mode].cost_usd)]});
      return send(calls === 1 ? 200 : 502, {status: calls === 1 ? 'ok' : 'error', results, comparison_id: `comparison-${calls}`});
    }
    send(404, {});
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({headless: true});
    const page = await browser.newPage();
    await page.goto(address + '/rag');
    await page.locator('#questions .question-item').first().waitFor();
    assert.equal(await page.locator('#questions .question-item').count(), 10);
    assert.match(await page.locator('#rag-config').innerText(), /20.*6.*2/);
    await page.locator('#questions .question-item button').first().click();
    assert.equal(await page.locator('#question').inputValue(), questions.questions[0].question);
    await page.locator('#compare').click();
    await page.getByText('Ожидаем фактические ответы API…').waitFor();
    assert.equal(await page.locator('#compare').isDisabled(), true);
    release();
    await page.locator('#rewrite_filter-result .answer-text').waitFor();
    assert.match(await page.locator('#filter-result').innerText(), /no_context/);
    assert.match(await page.locator('#rewrite-result').textContent(), /Уточнённый <script> запрос/);
    assert.match(await page.locator('#rag-result').textContent(), /Отбор кандидатов: 1/);
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.equal(await page.locator('script').count(), 1);
    assert.match(await page.locator('#cumulative').innerText(), /неполная/);
    assert.match(await page.locator('#evaluation-status').innerText(), /failed/);
    assert.match(await page.locator('#evaluation-summary').innerText(), /ожидается/);
    assert.match(await page.locator('#evaluation-summary').innerText(), /неизвестных вызовов 1/);
    const embeddingTariff = page.locator('#ledger-body tr').first().locator('details');
    await embeddingTariff.locator('summary').click();
    assert.match(await embeddingTariff.innerText(), /Ввод: \$0\.02 \/ 1M токенов/);
    assert.match(await embeddingTariff.innerText(), /cached input: неизвестно/);
    const rewriteTariff = page.locator('#ledger-body tr').nth(1).locator('details');
    await rewriteTariff.locator('summary').click();
    for (const value of ['0.10', '0.05', '0.04', '0.20']) assert.match(await rewriteTariff.innerText(), new RegExp(`\\$${value.replace('.', '\\.')} \/ 1M токенов`));
    await page.reload();
    assert.equal(await page.evaluate(() => localStorage.getItem('day23-rag-session-id')), session);
    await page.locator('#rewrite_filter-result .answer-text').waitFor();
    assert.equal(calls, 1, 'reload must not call compare again');
    assert.equal(await page.locator('#ledger-body tr').count(), 4);
    for (const mode of modes) assert.match(await page.locator(`#${mode}-result`).textContent(), /Статус:/);
    showAssessed = true;
    await page.locator('#refresh-evaluation').click();
    await page.getByText('Independent fixture', {exact: false}).waitFor();
    const evaluation = await page.locator('#evaluation-body').textContent();
    assert.match(evaluation, /Exact retrieved line/);
    assert.match(evaluation, /Invented role/);
    assert.match(evaluation, /Exact source/);
    assert.match(await page.locator('#evaluation-summary').innerText(), /Independent fixture/);
    assert.match(await page.locator('#evaluation-summary').innerText(), /неизвестных вызовов 0/);
    await page.locator('#question').fill(questions.questions[0].question);
    await page.locator('#compare').click();
    await page.locator('#error:not([hidden])').waitFor();
    assert.match(await page.locator('#filter-result').innerText(), /ещё не запускался/);
    await page.goto(address + '/');
    assert.equal(await page.locator('#chat-evaluation-open').count(), 1,
      'main chat exposes the current two-chat comparison');
    console.log('BROWSER_FIXTURE_OK: four modes, busy, literal text, no_context, partial failure, config, trace, assessment, reload, navigation; synthetic API only');
  } finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
