// Fixture-only browser check. Every API response below is synthetic; no paid call occurs.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const { chromium } = require(process.env.PLAYWRIGHT_NODE_MODULES || 'playwright');

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
  let showAssessed = false;
  let session = '';
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const source = {label: 'S1', file: 'alpha.md', source: 'fixture', title: 'Alpha', section: 'A', line_start: 1, line_end: 2, document_hash: 'abc', score: 0.8, chunk_id: 'abc:1', text: '<img src=x onerror=alert(1)> exact text'};
  const result = (mode, text) => ({status: 'ok', text, code: '', request_id: mode, mode, sources: mode === 'rag' ? [source] : [], context: mode === 'rag' ? '[S1] <img src=x onerror=alert(1)>' : '', context_budget: {limit_tokens: 6000, used_utf8_bytes: 34}, usage: {input_tokens: 20, output_tokens: 8, total_tokens: 28}, cost_usd: mode === 'plain' ? null : 0.0001, elapsed_seconds: 0.6});
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const send = (code, data, type = 'application/json') => { res.writeHead(code, {'Content-Type': type}); res.end(type === 'application/json' ? JSON.stringify(data) : data); };
    if (url.pathname === '/rag') return send(200, html('rag.html'), 'text/html');
    if (url.pathname === '/') return send(200, html('index.html'), 'text/html');
    if (url.pathname === '/indexing') return send(200, html('indexing.html'), 'text/html');
    if (url.pathname.startsWith('/static/')) return send(200, fs.readFileSync(path.join(root, url.pathname)), url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript');
    if (url.pathname === '/api/rag/questions') return send(200, questions);
    if (url.pathname === '/api/rag/evaluation') {
      if (!showAssessed) return send(200, {status: 'failed', questions: [{id: 'q01', question: questions.questions[0].question, plain: {status: 'error', text: 'Запрос модели не завершён.'}, rag: null, assessment: 'pending'}], summary: {assessment: 'pending', known_cost_usd: 0, complete: false}});
      return send(200, {status: 'complete', questions: [{id: 'q01', question: questions.questions[0].question,
        plain: result('plain', 'Saved plain full answer.'), rag: result('rag', 'Saved RAG full answer [S1].'),
        retrieval: {sources: [source], context: '[S1] exact saved context'},
        assessment: {plain: {correct_facts: 1, total_facts: 2, full_answer: false, abstention_correct: false,
          fact_scores: [{fact: 'First fact', score: 1, reason: 'Supported by saved answer.'}],
          unsupported_claims: [{claim: 'Invented server role', reason: 'Absent from corpus.'}]},
        rag: {correct_facts: 2, total_facts: 2, full_answer: true, abstention_correct: true,
          fact_scores: [{fact: 'Second fact', score: 1, reason: 'Exact retrieved line.'}], unsupported_claims: ['Unverified claim text']},
        retrieval: {source_recall_proxy: 0.5, sufficient_context: true, reason: 'One named source matched.'}}}],
        summary: {assessment: {plain_full: 0, rag_full: 1, plain_correct_facts: 1, rag_correct_facts: 2, total_facts: 2,
          rag_unsupported_claims: 1, rag_abstention_correct: 1, quality_basis: 'Independent agent fixture'}, known_cost_usd: 0.0002, complete: true}});
    }
    if (url.pathname === '/api/rag/state') return send(200, {session_id: url.searchParams.get('session_id'), requests, cumulative: {known_cost_usd: 0.0001, complete: false, unknown_calls: 1}});
    if (url.pathname === '/api/rag/compare') {
      let raw = ''; for await (const chunk of req) raw += chunk;
      const value = JSON.parse(raw); session = value.session_id;
      await pending;
      const plain = result('plain', '<script>window.pwned=1</script> Direct answer');
      const rag = result('rag', 'Answer [S1].');
      requests.push({...plain, question: value.question, session_id: session, comparison_id: 'pair', stages: [{kind: 'generation', status: 'ok', api_called: true, cost_usd: null, usage: plain.usage, input_policy: {code: 'accepted'}, output_policy: {code: 'accepted'}}]});
      requests.push({...rag, question: value.question, session_id: session, comparison_id: 'pair', stages: [{kind: 'query_embedding', status: 'ok', api_called: true, cost_usd: 0.0001, usage: rag.usage, input_policy: {code: 'accepted'}, output_policy: {code: 'accepted'}}]});
      return send(200, {status: 'ok', plain, rag, comparison_id: 'pair'});
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
    await page.getByText('10 контрольных вопросов').waitFor();
    assert.equal(await page.locator('#questions .question-item').count(), 10);
    await page.locator('#questions .question-item button').first().click();
    assert.equal(await page.locator('#question').inputValue(), questions.questions[0].question);
    await page.locator('#compare').click();
    await page.getByText('Ожидаем фактический ответ API…').waitFor();
    assert.equal(await page.locator('#compare').isDisabled(), true);
    release();
    await page.getByText('Answer [S1].').waitFor();
    assert.equal(await page.locator('script').count(), 1); // application script only; hostile text is literal
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.match(await page.locator('#cumulative').innerText(), /неполная/);
    assert.match(await page.locator('#evaluation-status').innerText(), /failed/);
    assert.match(await page.locator('#rag-result').textContent(), /S1.*alpha.md/);
    await page.reload();
    assert.equal(await page.evaluate(() => localStorage.getItem('day22-rag-session-id')), session);
    await page.getByText('Answer [S1].').waitFor();
    assert.equal(await page.locator('#ledger-body tr').count(), 2);
    await page.locator('#rag-result > details').first().evaluate(element => { element.open = true; });
    await page.locator('#rag-result > details details').first().evaluate(element => { element.open = true; });
    assert.match(await page.locator('#rag-result').innerText(), /<img src=x onerror=alert\(1\)>/);
    showAssessed = true;
    await page.locator('#refresh-evaluation').click();
    await page.getByText('Saved RAG full answer [S1].').waitFor({state: 'attached'});
    const evaluation = await page.locator('#evaluation-body').textContent();
    assert.match(evaluation, /неподтверждённых тезисов: 1/);
    assert.match(evaluation, /Invented server role/);
    assert.match(evaluation, /Unverified claim text/);
    assert.match(evaluation, /Exact retrieved line/);
    assert.doesNotMatch(evaluation, /NaN/);
    assert.match(await page.locator('#evaluation-summary').innerText(), /Independent agent fixture/);
    await page.goto(address + '/');
    assert.equal(await page.locator('#chat-evaluation-open').count(), 1,
      'main chat exposes the current two-chat comparison');
    console.log('BROWSER_FIXTURE_OK: busy, literal text, source/context, pending and assessed evaluation, reload ledger, navigation');
  } finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
