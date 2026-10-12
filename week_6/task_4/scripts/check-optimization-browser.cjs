/* Deterministic browser check of the real Day 29 template and client script. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates/optimization.html'), 'utf8')
  .replace("{{ url_for('static', filename='style.css') }}", '/static/style.css');
const receipt = { id: 9, stage: 'generation', preset: 'baseline', model: 'qwen3.5:9b', status: 'ok',
  usage_status: 'complete', usage: { input_tokens: 120, output_tokens: 40, total_tokens: 160,
    cached_input_tokens: null, reasoning_output_tokens: null }, cost_usd: 0, elapsed_seconds: 1.2,
  tariff: { model_mode: 'local', basis: 'Локальный API: 0 USD', infrastructure_cost_usd: null },
  input_policy: { status: 'accepted' }, output_policy: { status: 'accepted' } };
const result = { status: 'rejected', question: 'Как работает свет?',
  retrieval: { context_sha256: 'same-context', sources: [{ label: 'S1', title: 'Unity <source>',
    file: 'corpus/unity.md', section: ['Свет'], line_start: 10, line_end: 25, chunk_id: 'chunk-1',
    source: 'https://example.test/guide', text: '<img src=x onerror=window.evil=1> Полный фрагмент', score: 0.4 },
    { label: 'S2', title: 'Второй источник', file: 'corpus/second.md', line_start: 20, line_end: 40,
      chunk_id: 'chunk-2', source: 'javascript:alert(1)', text: 'Второй полный фрагмент', score: 0.2 }] },
  results: [{ preset: 'baseline', model: 'qwen3.5:9b', status: 'ok',
    answer: '<script>window.evil=1</script> Полный ответ', source_labels: ['S1'],
    request_sha256: 'baseline-hash', receipt, elapsed_seconds: 1.2,
    parameters: { temperature: 0.2, context_window: 8192, max_output_tokens: 1024 }, metrics: { eval_seconds: 0.9 } },
    { preset: 'optimized', model: 'qwen3.5:9b', status: 'rejected', answer: null,
      source_labels: [], request_sha256: 'optimized-hash',
      receipt: { ...receipt, id: 10, status: 'rejected', usage_status: 'unknown', cost_usd: null,
        output_policy: { status: 'rejected', code: 'missing_usage' } }, elapsed_seconds: 0.8,
      error: { code: 'missing_usage', message: 'Usage неизвестен.' }, parameters: {}, metrics: {} }],
  error: { code: 'missing_usage', message: 'Usage неизвестен.' },
  ledger: { total_usd: null, known_usd: 0, incomplete: true, receipt_count: 2 } };
const state = { index: { status: 'ready', chunks: 674 }, selected_preset: 'optimized', blocked: false,
  presets: { baseline: { model: 'qwen3.5:9b', temperature: 0.2, context_window: 8192,
    max_output_tokens: 1024 }, optimized: { model: 'qwen3.5:9b', temperature: 0,
    context_window: 6144, max_output_tokens: 512 }, quantized: { model: 'qwen3.5:9b-q8',
    temperature: 0, context_window: 6144, max_output_tokens: 512 } },
  history: [], receipts: [], ledger: { total_usd: 0, known_usd: 0, incomplete: false, receipt_count: 0 },
  benchmark: { kind: 'saved', report: { created_at: '2026-10-12T10:00:00Z', selected_preset: 'optimized',
    presets: { baseline: { successes: 15, wall_seconds: { median: 11.2 },
      tokens_per_second: { median: 18 }, resident_vram_bytes: { median: 5000000000 } },
      optimized: { successes: 15, wall_seconds: { median: 7.4 }, tokens_per_second: { median: 25 },
        resident_vram_bytes: { median: 4800000000 } } },
    quality: { baseline: { correct_facts: 35, total_facts: 50 },
      optimized: { correct_facts: 43, total_facts: 50 } } } } };

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    let stateCalls = 0, askCalls = 0, releaseState;
    const lateState = new Promise(resolve => { releaseState = resolve; });
    await page.route('http://optimization.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
      for (const [routePath, file] of [
        ['/static/style.css', '../task_2/static/style.css'],
        ['/day28-static/local-rag.css', '../task_3/static/local-rag.css'],
        ['/day29-static/optimization.css', 'static/optimization.css'],
        ['/day29-static/optimization.js', 'static/optimization.js']]) {
        if (url.pathname === routePath) return route.fulfill({ contentType: routePath.endsWith('.js') ? 'text/javascript' : 'text/css',
          body: fs.readFileSync(path.join(root, file)) });
      }
      if (url.pathname === '/api/optimization/state') {
        stateCalls++;
        if (stateCalls === 2) await lateState;
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(stateCalls > 2 ? {
          ...state, blocked: true, receipts: result.results.map(item => item.receipt), ledger: result.ledger
        } : state) });
      }
      if (url.pathname === '/api/optimization/ask') {
        askCalls++;
        assert.deepEqual(route.request().postDataJSON(), { prompt: 'Как работает свет?', preset: 'compare' });
        await new Promise(resolve => setTimeout(resolve, 100));
        return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify(result) });
      }
      throw new Error('Unexpected request: ' + url.pathname);
    });
    await page.goto('http://optimization.test/');
    await page.getByText('Индекс: готов').waitFor();
    assert.match(await page.locator('#optimization-benchmark').innerText(), /35 \/ 50/);
    assert.match(await page.locator('#optimization-benchmark').innerText(), /7\.4/);
    assert.match(await page.locator('#optimization-benchmark').innerText(), /медиана наибольших наблюдений каждого запроса/i);
    await page.locator('#optimization-preset').selectOption('compare');
    await page.locator('#optimization-prompt').fill('Как работает свет?');
    await page.locator('#optimization-refresh').click();
    await page.locator('#optimization-submit').click();
    assert.equal(await page.locator('#optimization-submit').isDisabled(), true);
    await page.locator('#optimization-submit').evaluate(button => button.form.requestSubmit());
    await page.locator('.answer-card[data-preset="baseline"] .answer-text').getByText('<script>window.evil=1</script> Полный ответ').waitFor();
    assert.equal(askCalls, 1);
    assert.equal(await page.locator('script').count(), 1);
    assert.equal(await page.locator('img[src="x"]').count(), 0);
    assert.equal(await page.locator('#optimization-sources .source-card').count(), 2);
    await page.locator('#optimization-sources .source-card').nth(1).locator('summary').click();
    assert.match(await page.locator('#optimization-sources').innerText(), /Второй полный фрагмент/);
    assert.equal(await page.locator('#optimization-sources a[href^="javascript:"]').count(), 0);
    assert.equal(await page.locator('#optimization-sources a').first().getAttribute('rel'), 'noopener noreferrer');
    await page.locator('.answer-card[data-preset="baseline"] .rag-receipt summary').click();
    assert.match(await page.locator('.answer-card[data-preset="baseline"] .rag-receipt').innerText(), /вход 120, выход 40, всего 160/);
    assert.match(await page.locator('.answer-card[data-preset="baseline"]').innerText(), /Запрос генерации: 1\.200 с/);
    assert.match(await page.locator('.answer-card[data-preset="baseline"]').innerText(), /Генерация токенов: 0\.900 с/);
    assert.match(await page.locator('#optimization-results').innerText(), /Usage неизвестен/);
    assert.match(await page.locator('#optimization-ledger').innerText(), /неизвестн/i);
    releaseState();
    await page.waitForTimeout(100);
    assert.match(await page.locator('#optimization-results').innerText(), /Полный ответ/);
    assert.equal(await page.locator('#optimization-submit').isDisabled(), true);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    assert.equal(await page.locator('.rag-spinner').evaluate(element => getComputedStyle(element).animationName), 'none');
    const keyboard = await browser.newPage();
    await keyboard.route('http://optimization.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
      if (url.pathname === '/api/optimization/state') return route.fulfill({ contentType: 'application/json', body: JSON.stringify(state) });
      return route.fulfill({ status: 204 });
    });
    await keyboard.goto('http://optimization.test/');
    await keyboard.keyboard.press('Tab');
    assert.equal(await keyboard.evaluate(() => document.activeElement?.className), 'skip-link');
    const policyPage = await browser.newPage();
    const policyReceipt = { ...receipt, id: 11, stage: 'input_policy', preset: null,
      status: 'rejected', metered: false, usage_status: 'not_applicable', cost_usd: 0,
      input_policy: { status: 'rejected', code: 'input_policy' }, output_policy: { status: 'not_requested' } };
    let policyStateCalls = 0, policyAskCalls = 0;
    await policyPage.route('http://policy.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
      for (const [routePath, file] of [
        ['/static/style.css', '../task_2/static/style.css'],
        ['/day28-static/local-rag.css', '../task_3/static/local-rag.css'],
        ['/day29-static/optimization.css', 'static/optimization.css'],
        ['/day29-static/optimization.js', 'static/optimization.js']]) {
        if (url.pathname === routePath) return route.fulfill({ contentType: routePath.endsWith('.js') ? 'text/javascript' : 'text/css', body: fs.readFileSync(path.join(root, file)) });
      }
      if (url.pathname === '/api/optimization/state') {
        policyStateCalls++;
        const next = policyStateCalls === 1 ? state : { ...state, receipts: [policyReceipt],
          ledger: { total_usd: 0, known_usd: 0, incomplete: false, receipt_count: 1 } };
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(next) });
      }
      if (url.pathname === '/api/optimization/ask') {
        policyAskCalls++;
        assert.deepEqual(route.request().postDataJSON(), { prompt: 'Ошибка\u0001', preset: 'optimized' });
        return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({
          status: 'rejected', question: 'Ошибка\u0001', retrieval: null, results: [],
          error: { code: 'input_policy', message: 'Недопустимый вопрос.' },
          ledger: { total_usd: 0, known_usd: 0, incomplete: false, receipt_count: 1 }
        }) });
      }
      throw new Error('Unexpected request: ' + url.pathname);
    });
    await policyPage.goto('http://policy.test/');
    await policyPage.getByText('Индекс: готов').waitFor();
    await policyPage.locator('#optimization-prompt').fill('Ошибка\u0001');
    await policyPage.locator('#optimization-submit').click();
    const policySummary = policyPage.locator('#optimization-ledger .rag-receipts > .rag-receipt > summary');
    await policySummary.getByText(/Проверка входа.*rejected/).waitFor();
    assert.equal(policyAskCalls, 1);
    assert.equal(policyStateCalls, 2);
    await policySummary.click();
    assert.match(await policyPage.locator('#optimization-ledger .rag-receipts').innerText(), /Input policy: rejected \(input_policy\)/);
    assert.match(await policyPage.locator('#optimization-results').innerText(), /Ответ не получен/);
    console.log('PASS Day 29 browser flow, saved report, race, double submit, escaped full evidence');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
