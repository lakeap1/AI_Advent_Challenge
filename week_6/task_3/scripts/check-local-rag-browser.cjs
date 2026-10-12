/* Deterministic browser contract against the actual Flask-rendered page. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');

const taskRoot = path.resolve(__dirname, '..');
const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'day28-browser-'));
const python = process.env.PYTHON || path.join(taskRoot, '.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const render = spawnSync(python, ['-B', '-c',
  'from app import create_app; import sys; sys.stdout.buffer.write(create_app(data_dir=sys.argv[1]).test_client().get("/").data)', dataDir],
  { cwd: taskRoot, encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
fs.rmSync(dataDir, { recursive: true, force: true });
assert.equal(render.status, 0, render.stderr);
const html = render.stdout;

const source = (label, text) => ({ label, chunk_id: 'chunk-' + label, file: 'corpus/unity.md',
  title: 'Unity <source>', source: 'https://example.test/docs', section: ['Свет', 'URP'],
  line_start: 10, line_end: 25, text, score: 0.031 });
const receipt = (provider, id) => ({ id, stage: 'generation', provider, model: provider === 'local' ? 'qwen3.5:9b' : 'gpt-6-luna',
  status: 'ok', usage_status: 'complete', usage: { input_tokens: 120, output_tokens: 40, total_tokens: 160,
    cached_input_tokens: provider === 'cloud' ? 12 : null, reasoning_output_tokens: provider === 'cloud' ? 4 : null },
  cost_usd: provider === 'local' ? 0 : 0.00008, elapsed_seconds: 0.4,
  tariff: provider === 'local' ? { model_mode: 'local', api_cost_usd: 0, infrastructure_cost_usd: null,
    basis: 'Локальная Ollama не взимает плату за токенный API.' } :
    { model_mode: 'cloud', service_tier: 'standard', price_checked_date: '2026-10-11',
      price_source: 'https://developers.openai.com/api/docs/pricing', input_usd_per_million: .1,
      cached_input_usd_per_million: .01, output_usd_per_million: .5 },
  input_policy: { status: 'accepted' }, output_policy: { status: 'accepted' } });
const retrieval = { sources: [source('S1', '<img src=x onerror=alert(1)>'), source('S2', 'Второй полный фрагмент')],
  context_sha256: 'context-hash', request_sha256: 'request-hash', index_id: 'index-id', elapsed_seconds: 0.0012 };
const localResult = { provider: 'local', model: 'qwen3.5:9b', status: 'ok',
  answer: '<script>window.evil=1</script> Полный ответ', source_labels: ['S1'], request_sha256: 'request-hash',
  receipt: receipt('local', 2), elapsed_seconds: .4, error: null };
const ledger = { total_usd: null, known_usd: .00008, incomplete: true, receipt_count: 3 };
const result = { status: 'rejected', question: 'Как работает свет?', retrieval,
  results: [localResult, { provider: 'cloud', model: 'gpt-6-luna', status: 'rejected', answer: null,
    source_labels: [], request_sha256: 'request-hash', receipt: { ...receipt('cloud', 3),
      status: 'rejected', usage_status: 'unknown', cost_usd: null,
      output_policy: { status: 'rejected', code: 'missing_usage' } }, elapsed_seconds: .6,
    error: { code: 'missing_usage', message: 'Cloud usage неизвестен.' } }],
  error: { code: 'missing_usage', message: 'Cloud usage неизвестен.' }, ledger };
const state = { index: { status: 'ready', chunks: 674, model: 'qwen3-embedding:0.6b', generation_model: 'qwen3.5:9b' },
  runtime: { status: 'unchecked', embedding_model: 'qwen3-embedding:0.6b', generation_model: 'qwen3.5:9b' },
  history: [], receipts: [], ledger: { total_usd: 0, known_usd: 0, incomplete: false, receipt_count: 0 },
  cloud_available: true, blocked: false };

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    let stateCount = 0, askCount = 0, releaseState;
    const lateState = new Promise(resolve => { releaseState = resolve; });
    await page.route('http://rag.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
      if (url.pathname === '/static/style.css') return route.fulfill({ status: 200, contentType: 'text/css', body: fs.readFileSync(path.join(taskRoot, '../task_2/static/style.css')) });
      if (url.pathname === '/day28-static/local-rag.css') return route.fulfill({ status: 200, contentType: 'text/css', body: fs.readFileSync(path.join(taskRoot, 'static/local-rag.css')) });
      if (url.pathname === '/day28-static/local-rag.js') return route.fulfill({ status: 200, contentType: 'text/javascript', body: fs.readFileSync(path.join(taskRoot, 'static/local-rag.js')) });
      if (url.pathname === '/api/local-rag/state') {
        stateCount++;
        if (stateCount === 2) await lateState;
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) });
      }
      if (url.pathname === '/api/local-rag/ask') {
        askCount++;
        assert.deepEqual(route.request().postDataJSON(), { prompt: 'Как работает свет?', mode: 'compare' });
        await new Promise(resolve => setTimeout(resolve, 100));
        return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify(result) });
      }
      throw new Error('Unexpected request: ' + url.pathname);
    });
    await page.goto('http://rag.test/');
    await page.getByText('Проверка доступности Ollama не выполнялась').waitFor();
    await page.locator('#rag-mode').selectOption('compare');
    await page.locator('#rag-prompt').fill('Как работает свет?');
    await page.locator('#rag-refresh').click();
    await page.locator('#rag-submit').click();
    assert.equal(await page.locator('#rag-submit').isDisabled(), true);
    await page.locator('#rag-submit').evaluate(button => button.form.requestSubmit());
    await page.locator('.answer-card[data-provider="local"] .answer-text').getByText('<script>window.evil=1</script> Полный ответ').waitFor();
    assert.equal(askCount, 1);
    assert.equal(await page.locator('img[src="x"]').count(), 0);
    assert.equal(await page.locator('#rag-sources .source-card').count(), 2);
    await page.locator('#rag-sources .source-card').nth(1).locator('summary').click();
    assert.match(await page.locator('#rag-sources').innerText(), /Второй полный фрагмент/);
    const sourceLink = page.locator('#rag-sources .source-card').first().locator('a');
    assert.equal(await sourceLink.getAttribute('rel'), 'noopener noreferrer');
    assert.equal(await sourceLink.getAttribute('href'), 'https://example.test/docs');
    await page.locator('.answer-card[data-provider="local"] .rag-receipt summary').click();
    assert.match(await page.locator('.answer-card[data-provider="local"] .rag-receipt').innerText(), /вход 120, выход 40, всего 160/);
    assert.match(await page.locator('.answer-card[data-provider="local"] .rag-receipt').innerText(), /Стоимость инфраструктуры не измерялась/);
    assert.match(await page.locator('#rag-results').innerText(), /Cloud usage неизвестен/);
    assert.match(await page.locator('#rag-ledger').innerText(), /неполн|неизвестн/i);
    releaseState();
    await page.waitForTimeout(100);
    assert.match(await page.locator('#rag-results').innerText(), /Полный ответ/);
    assert.equal(await page.locator('#rag-submit').isDisabled(), true);

    const noKey = await browser.newPage();
    let buildCount = 0;
    await noKey.route('http://rag.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
      if (url.pathname === '/static/style.css') return route.fulfill({ status: 200, contentType: 'text/css', body: fs.readFileSync(path.join(taskRoot, '../task_2/static/style.css')) });
      if (url.pathname === '/day28-static/local-rag.css') return route.fulfill({ status: 200, contentType: 'text/css', body: fs.readFileSync(path.join(taskRoot, 'static/local-rag.css')) });
      if (url.pathname === '/day28-static/local-rag.js') return route.fulfill({ status: 200, contentType: 'text/javascript', body: fs.readFileSync(path.join(taskRoot, 'static/local-rag.js')) });
      if (url.pathname === '/api/local-rag/state') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...state, cloud_available: false, index: { status: 'missing' }, history: [{ ...result, id: 7, created_at: '2026-10-11T10:00:00Z' }] }) });
      if (url.pathname === '/api/local-rag/build') {
        buildCount++;
        assert.deepEqual(route.request().postDataJSON(), {});
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', index: state.index, ledger: state.ledger }) });
      }
      throw new Error('Unexpected request: ' + url.pathname);
    });
    await noKey.goto('http://rag.test/');
    await noKey.getByText('Индекс: отсутствует').waitFor();
    assert.equal(await noKey.locator('#rag-mode').inputValue(), 'local');
    assert.equal(await noKey.locator('#rag-mode option[value="cloud"]').isDisabled(), true);
    assert.equal(await noKey.locator('#rag-mode option[value="compare"]').isDisabled(), true);
    assert.equal(await noKey.locator('a[href="/chat"]').count() > 0, true);
    await noKey.locator('.rag-history-item').click();
    assert.match(await noKey.locator('#rag-results').innerText(), /Полный ответ/);
    assert.match(await noKey.locator('#rag-sources').innerText(), /Unity <source>/);
    assert.equal(buildCount, 0);
    await noKey.locator('#rag-build').click();
    await noKey.getByText('Индекс: готов').waitFor();
    assert.equal(buildCount, 1);

    const oldAnswer = { ...result, id: 4, question: 'Старый вопрос', created_at: '2026-10-10T10:00:00Z',
      ledger: { total_usd: .001, known_usd: .001, incomplete: false, receipt_count: 10 },
      results: [{ ...localResult, answer: 'Старый ответ', receipt: { ...receipt('local', 4), id: 4 } }] };
    const olderWithoutLedger = { ...oldAnswer, id: 3, question: 'Ещё старше',
      results: [{ ...localResult, answer: 'Ещё более старый ответ', receipt: { ...receipt('local', 3), id: 3 } }] };
    delete olderWithoutLedger.ledger;
    const noisyReceipts = [
      receipt('cloud', 30),
      { ...receipt('local', 29), stage: 'query_embed' },
      receipt('local', 28),
      ...Array.from({ length: 16 }, (_, i) => ({ ...receipt('local', 27 - i), stage: 'runtime_metadata', metered: false }))
    ];
    const ledgerPage = await browser.newPage();
    await ledgerPage.route('http://rag.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
      if (url.pathname === '/static/style.css') return route.fulfill({ status: 200, contentType: 'text/css', body: fs.readFileSync(path.join(taskRoot, '../task_2/static/style.css')) });
      if (url.pathname === '/day28-static/local-rag.css') return route.fulfill({ status: 200, contentType: 'text/css', body: fs.readFileSync(path.join(taskRoot, 'static/local-rag.css')) });
      if (url.pathname === '/day28-static/local-rag.js') return route.fulfill({ status: 200, contentType: 'text/javascript', body: fs.readFileSync(path.join(taskRoot, 'static/local-rag.js')) });
      if (url.pathname === '/api/local-rag/state') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        ...state, receipts: noisyReceipts, history: [oldAnswer, olderWithoutLedger,
          ...Array.from({ length: 15 }, (_, i) => ({ ...olderWithoutLedger, id: 50 + i, question: `Исторический вопрос ${i + 1}` }))],
        ledger: { total_usd: .003, known_usd: .003, incomplete: false, receipt_count: 189 }
      }) });
      throw new Error('Unexpected request: ' + url.pathname);
    });
    await ledgerPage.goto('http://rag.test/');
    await ledgerPage.getByText('Всего API: $0.003').waitFor();
    assert.equal(await ledgerPage.locator('#rag-ledger .rag-receipts > .rag-receipt').count(), 3);
    assert.doesNotMatch(await ledgerPage.locator('#rag-ledger .rag-receipts').innerText(), /runtime_metadata/);
    assert.equal(await ledgerPage.locator('#rag-receipt-journal').getAttribute('open'), null);
    assert.equal(await ledgerPage.locator('#rag-receipt-journal .rag-receipt').count(), noisyReceipts.length);
    assert.equal(await ledgerPage.locator('.rag-nav-history').evaluate(node => node.scrollHeight > node.clientHeight), true);
    await ledgerPage.locator('#rag-receipt-journal > summary').click();
    assert.equal(await ledgerPage.locator('.rag-journal-list').evaluate(node => node.scrollHeight > node.clientHeight), true);
    await ledgerPage.locator('.rag-history-item').first().click();
    assert.match(await ledgerPage.locator('#rag-results').innerText(), /Старый ответ/);
    assert.match(await ledgerPage.locator('#rag-ledger').innerText(), /Всего API: \$0\.003/);
    await ledgerPage.locator('.rag-history-item').nth(1).click();
    assert.match(await ledgerPage.locator('#rag-results').innerText(), /Ещё более старый ответ/);
    assert.match(await ledgerPage.locator('#rag-ledger').innerText(), /Всего API: \$0\.003/);
    assert.equal(await ledgerPage.locator('#rag-ledger .rag-receipts > .rag-receipt').count(), 3);
    console.log('PASS controlled Flask template + injected HTTP browser checks');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
