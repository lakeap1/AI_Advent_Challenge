/* Browser smoke for a running private site. Credentials only via environment. */
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const base = process.env.BROWSER_BASE_URL;
const username = process.env.BROWSER_USERNAME;
const password = process.env.BROWSER_PASSWORD;
if (!base || !username || !password) {
  throw new Error('Set BROWSER_BASE_URL, BROWSER_USERNAME and BROWSER_PASSWORD');
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1365, height: 850 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base, { waitUntil: 'networkidle' });
    await page.locator('#username').fill(username);
    await page.locator('#password').fill(password);
    await page.getByRole('button', { name: 'Войти' }).click();
    await page.locator('#app-view:visible').waitFor();
    await page.locator('#new-chat').click();
    await page.locator('#question').fill('Чем материал отличается от шейдера?');
    await page.getByRole('button', { name: /Отправить/ }).click();
    await page.locator('#send:not(:disabled)').waitFor({ timeout: 60000 });
    const content = await page.locator('#main').innerText();
    assert(!content.includes('PRIVATE_DRAFT_SENTINEL'));
    assert(!content.includes('PRIVATE_TRACE_SENTINEL'));
    async function command(text) {
      await page.locator('#question').fill(text);
      await page.locator('#send').click();
      await page.locator('#send:not(:disabled)').waitFor();
    }
    if ((process.env.BROWSER_FINAL_CASE || '').startsWith('auth-')) {
      const mode = process.env.BROWSER_FINAL_CASE;
      await command('/profile role=PROFILE_A');
      let startedA, releaseA;
      const waitingA = new Promise(resolve => { startedA = resolve; });
      const blockedA = new Promise(resolve => { releaseA = resolve; });
      const pathA = mode === 'auth-profile' ? '**/api/profile'
        : mode === 'auth-memory' ? '**/api/memories' : '**/api/bootstrap';
      let heldA = false;
      let oldResponseRefreshesCookie = false;
      await page.route(pathA, async route => {
        if (heldA) { await route.continue(); return; }
        heldA = true;
        const response = await route.fetch(); // Capture authorized A response before logout.
        oldResponseRefreshesCookie = Boolean(response.headers()['set-cookie']);
        startedA();
        await blockedA;
        if (mode === 'auth-error' || mode === 'auth-401') {
          await route.fulfill({ status: mode === 'auth-401' ? 401 : 500,
            contentType: 'application/json', body: JSON.stringify({ error: 'STALE_A_ERROR' }) });
        } else await route.fulfill({ response });
      });
      if (mode === 'auth-profile' || mode === 'auth-memory') {
        if (mode === 'auth-profile') {
          await page.getByRole('button', { name: 'Профиль' }).click();
          await page.getByRole('dialog').getByLabel('Роль или профессия').fill('PROFILE_A_LATE');
          await page.getByRole('button', { name: 'Сохранить профиль' }).click();
        } else {
          await page.getByRole('button', { name: 'Память' }).click();
          await page.getByRole('dialog').getByLabel('Название сведения').fill('private-form-A');
          await page.getByRole('dialog').getByLabel('Значение').fill('MEMORY_A');
          await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
        }
        await waitingA;
        await page.getByRole('button', { name: 'Закрыть панель' }).click();
      } else {
        await page.locator('#question').fill('/remember private-A: MEMORY_A');
        await page.locator('#send').click();
        await waitingA;
      }
      await page.locator('#logout').click();
      await page.locator('#login-view:visible').waitFor();
      const clearedAtLogout = await page.evaluate(() => !state.memories.length
        && !Object.keys(state.profile).length && !state.branch && !state.chat && !state.source);
      await page.locator('#username').fill('browser-b');
      await page.locator('#password').fill('browser-fixture-password-b');
      await page.getByRole('button', { name: 'Войти' }).click();
      await page.locator('#app-view:visible').waitFor();
      if (await page.evaluate(() => state.busy)) {
        releaseA();
        await page.waitForTimeout(200);
        assert.equal(await page.evaluate(() => state.profile.role), 'PROFILE_B', 'late A response overwrote B profile');
        assert.fail('old A busy state survived logout/login B');
      }
      await page.locator('#send:not(:disabled)').waitFor();
      let startedB, releaseB;
      const waitingB = new Promise(resolve => { startedB = resolve; });
      const blockedB = new Promise(resolve => { releaseB = resolve; });
      await page.route('**/ask', async route => {
        const response = await route.fetch(); startedB(); await blockedB; await route.fulfill({ response });
      });
      await page.locator('#question').fill('Вопрос B');
      await page.locator('#send').click();
      await waitingB;
      await page.locator('#logout').focus();
      const before = await page.evaluate(() => ({ notice: document.getElementById('notice').textContent,
        branch: state.branch.id, chat: state.chat.id, focus: document.activeElement.id }));
      const lateA = page.waitForResponse(response => response.url().endsWith(mode === 'auth-profile'
        ? '/api/profile' : mode === 'auth-memory' ? '/api/memories' : '/api/bootstrap'));
      releaseA();
      await lateA;
      await page.waitForTimeout(150);
      const after = await page.evaluate(() => ({ profile: state.profile, memories: state.memories,
        branch: state.branch?.id, chat: state.chat?.id, busy: state.busy,
        notice: document.getElementById('notice').textContent, focus: document.activeElement.id,
        appVisible: !document.getElementById('app-view').hidden }));
      assert.equal(after.profile.role, 'PROFILE_B', 'late A response overwrote B profile');
      assert.deepEqual(after.memories.map(item => item.key), ['private-B'], 'late A response overwrote B memory');
      assert.equal(after.busy, true, 'stale A finally cleared B busy');
      assert.equal(after.notice, before.notice, 'stale A notice/error changed B notice');
      assert.equal(after.focus, before.focus, 'stale A finally changed B focus');
      assert.equal(after.appVisible, true, 'stale A 401 logged B out');
      assert.equal(after.chat, before.chat); assert.equal(after.branch, before.branch);
      assert(clearedAtLogout, 'logout retained A private state');
      const immediateServerB = await page.evaluate(async () => (await (await fetch('/api/bootstrap')).json()));
      assert.equal(immediateServerB.profile?.role, 'PROFILE_B', 'late A cookie changed authenticated server user');
      assert.deepEqual(immediateServerB.memories.map(item => item.key), ['private-B']);
      releaseB();
      await page.locator('#send:not(:disabled)').waitFor();
      await page.getByRole('button', { name: 'Профиль' }).click();
      assert.equal(await page.getByRole('dialog').getByLabel('Роль или профессия').inputValue(), 'PROFILE_B');
      await page.getByRole('button', { name: 'Закрыть панель' }).click();
      await page.getByRole('button', { name: 'Память' }).click();
      assert(!(await page.getByRole('dialog').innerText()).includes('MEMORY_A'));
      const serverB = await page.evaluate(async () => (await (await fetch('/api/bootstrap')).json()));
      assert.equal(serverB.profile.role, 'PROFILE_B');
      assert.deepEqual(serverB.memories.map(item => item.key), ['private-B']);
      assert.deepEqual(errors, []);
      console.log(`BROWSER ${mode} PASS: captured A response, logout clears state, login B, stale success/error/401/finally discarded; old Set-Cookie present=${oldResponseRefreshesCookie}, immediate authenticated bootstrap remains B`);
      return;
    }
    if (process.env.BROWSER_FINAL_CASE === 'commands') {
      await command('/remember command-key: командное значение');
      await page.getByRole('button', { name: 'Память' }).click();
      assert((await page.getByRole('dialog').innerText()).includes('командное значение'), 'remember composer did not refresh memory');
      await page.getByRole('button', { name: 'Закрыть панель' }).click();
      let bootstrapStarted;
      let releaseBootstrap;
      const started = new Promise(resolve => { bootstrapStarted = resolve; });
      const released = new Promise(resolve => { releaseBootstrap = resolve; });
      await page.route('**/api/bootstrap', async route => {
        bootstrapStarted(); await released; await route.continue();
      });
      await page.locator('#question').fill('/forget command-key');
      await page.locator('#send').click();
      await started;
      await page.getByRole('button', { name: 'Память' }).click();
      assert.equal(await page.getByRole('dialog').getByRole('button', { name: 'Изменить', exact: true }).count(), 0,
        'stale memory can be saved again while refresh is pending');
      releaseBootstrap();
      await page.locator('#send:not(:disabled)').waitFor();
      assert(!(await page.getByRole('dialog').innerText()).includes('командное значение'), 'forget left old memory in open drawer');
      assert(!(await page.locator('#notice').getAttribute('class') || '').includes('error'), 'updated was shown as failed answer');
      await page.unroute('**/api/bootstrap');
      await page.getByRole('button', { name: 'Закрыть панель' }).click();
      await command('/profile role=Новое значение; level=beginner');
      await page.getByRole('button', { name: 'Профиль' }).click();
      assert.equal(await page.getByRole('dialog').getByLabel('Роль или профессия').inputValue(), 'Новое значение');
      await page.getByRole('button', { name: 'Сохранить профиль' }).click();
      await page.getByRole('button', { name: 'Память' }).click();
      await page.getByRole('dialog').getByLabel('Название сведения').fill('replacement');
      await page.getByRole('dialog').getByLabel('Значение').fill('только новое');
      await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
      const bootstrap = await page.evaluate(async () => (await (await fetch('/api/bootstrap')).json()));
      const keys = bootstrap.memories.map(item => item.key);
      assert(keys.includes('replacement') && !keys.includes('command-key'), 'forgotten memory resurrected');
      assert.equal(bootstrap.profile.role, 'Новое значение');
      assert.deepEqual(errors, []);
      console.log('BROWSER FINAL COMMANDS PASS: remember, delayed forget refresh/open drawer, profile, positive notice, no resurrection');
      return;
    }
    if (process.env.BROWSER_FINAL_CASE === 'fork') {
      let posts = 0;
      await page.route('**/fork', async route => {
        posts++; await new Promise(resolve => setTimeout(resolve, 350)); await route.continue();
      });
      const before = await page.evaluate(() => JSON.parse(localStorage.getItem('grani.activeBranch')));
      await page.getByRole('button', { name: 'Ответвить отсюда' }).first().evaluate(element => { element.click(); element.click(); });
      await page.waitForFunction(branchId => JSON.parse(localStorage.getItem('grani.activeBranch'))?.branchId !== branchId, before.branchId);
      await page.waitForTimeout(500);
      assert.equal(posts, 1, 'fast double fork dispatched twice');
      await page.getByRole('button', { name: 'Ветки' }).click();
      assert.equal(await page.getByRole('dialog').locator('.branch-item').count(), 2);
      const branches = await page.evaluate(async chatId => (await (await fetch(`/api/chats/${chatId}/branches`)).json()).branches, before.chatId);
      assert.equal(branches.length, 2, 'fork list differs from server');
      assert.deepEqual(errors, []);
      console.log('BROWSER FINAL FORK PASS: double click, one POST, server and drawer both two branches');
      return;
    }
    await page.getByRole('button', { name: 'Профиль' }).click();
    await page.getByRole('dialog').getByLabel('Роль или профессия').fill('QA');
    await page.getByRole('dialog').getByLabel('Уровень знаний').fill('Начальный');
    await page.getByRole('button', { name: 'Сохранить профиль' }).click();
    await page.getByRole('button', { name: 'Память' }).click();
    await page.getByRole('dialog').getByLabel('Название сведения').fill('подача');
    await page.getByRole('dialog').getByLabel('Значение').fill('объяснять на примере');
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.getByRole('button', { name: 'Условия' }).click();
    await page.getByRole('dialog').getByLabel('Название', { exact: true }).fill('pipeline');
    await page.getByRole('dialog').getByLabel('Значение', { exact: true }).fill('URP');
    await page.getByRole('button', { name: 'Сохранить условие' }).click();
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.getByRole('button', { name: 'Ответвить отсюда' }).first().click();
    await page.getByRole('button', { name: 'Ветки' }).click();
    assert((await page.getByRole('dialog').locator('.branch-item').count()) >= 2);
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.route('**/api/ledger?**', route => route.fulfill({ contentType: 'application/json',
      body: JSON.stringify({ complete: false, known_cost_estimated_usd: 0.000003,
        cost_estimated_usd: null, input_tokens: 12, output_tokens: 5, total_tokens: 17,
        calls: [
          { stage: 'preparation', model: 'glm-5.3-flash', status: 'complete',
            usage: { input_tokens: 12, output_tokens: 5, total_tokens: 17,
              cached_input_tokens: 3, reasoning_tokens: 2 }, cost_estimated_usd: 0.000003,
            tariff_source: 'Z.ai standard API', tariff_checked_on: '2026-10-19' },
          { stage: 'validator', model: 'glm-5.3-flash', status: 'unknown', usage: null,
            cost_estimated_usd: null, tariff_source: 'Z.ai standard API', tariff_checked_on: '2026-10-19' }
        ] }) }));
    await page.getByRole('button', { name: 'Учёт' }).click();
    await page.getByRole('dialog').getByText('Оценка расходов, USD').waitFor();
    const usage = await page.getByRole('dialog').innerText();
    for (const value of ['Подготовка', 'Смысловая проверка', 'Вход: 12', 'Кэш входа: 3',
      'Рассуждение: 2', 'Стоимость: неизвестно', 'фактическое списание bundle неизвестно']) {
      assert(usage.includes(value), `Missing usage detail: ${value}`);
    }
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.unroute('**/api/ledger?**');
    await page.reload({ waitUntil: 'networkidle' });
    await page.getByRole('button', { name: 'Профиль' }).click();
    assert.equal(await page.getByRole('dialog').getByLabel('Роль или профессия').inputValue(), 'QA');
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.getByRole('button', { name: 'Память' }).click();
    await page.getByRole('dialog').getByText('объяснять на примере').waitFor();
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.getByRole('button', { name: 'Ветки' }).click();
    assert((await page.getByRole('dialog').locator('.branch-item').count()) >= 2);
    assert((await page.getByRole('dialog').locator('.branch-item.active').innerText()).includes('Ветка 2'));
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.route('**/api/ledger?**', async route => {
      await new Promise(resolve => setTimeout(resolve, 700));
      await route.continue();
    });
    await page.getByRole('button', { name: 'Учёт' }).click();
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.getByRole('button', { name: 'Профиль' }).click();
    await page.waitForTimeout(1000);
    assert.equal(await page.getByRole('dialog').locator('.usage-number').count(), 0);
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.unroute('**/api/ledger?**');
    const reference = await page.request.get(`${base}/reference/readme`);
    assert.equal(reference.status(), 200);
    assert((await reference.text()).includes('Грани'));
    await page.locator('#new-chat').click();
    await page.locator('#new-chat').click();
    const raceChats = await page.evaluate(async () => (await (await fetch('/api/chats')).json()).chats);
    const [chatB, chatA] = raceChats;
    assert(chatA && chatB && chatA.id !== chatB.id);
    async function assertNavigationRace(delayedPath) {
      let release;
      let requestStarted;
      const blocked = new Promise(resolve => { release = resolve; });
      const started = new Promise(resolve => { requestStarted = resolve; });
      const pattern = `**${delayedPath}`;
      await page.route(pattern, async route => {
        requestStarted();
        await blocked;
        await route.continue();
      });
      await page.locator('#chat-list .chat-item').nth(1).click();
      await started;
      await page.locator('#chat-list .chat-item').first().click();
      await page.waitForFunction(id => JSON.parse(localStorage.getItem('grani.activeBranch') || 'null')?.branchId === id,
        chatB.branch_id);
      const delayedResponse = page.waitForResponse(response => response.url().endsWith(delayedPath));
      release();
      await delayedResponse;
      await page.waitForTimeout(100);
      const active = await page.evaluate(() => JSON.parse(localStorage.getItem('grani.activeBranch') || 'null'));
      assert.equal(active.chatId, chatB.id, 'delayed A changed the active chat');
      assert.equal(active.branchId, chatB.branch_id, 'delayed A changed the active branch');
      await page.unroute(pattern);
    }
    await assertNavigationRace(`/api/chats/${chatA.id}/branches`);
    await assertNavigationRace(`/api/branches/${chatA.branch_id}`);
    const nextAsk = page.waitForRequest(request => request.method() === 'POST'
      && request.url().endsWith(`/api/branches/${chatB.branch_id}/ask`));
    await page.locator('#question').fill('Проверка текущей ветки');
    await page.locator('#send').click();
    await nextAsk;
    await page.locator('#send:not(:disabled)').waitFor();
    await page.getByRole('button', { name: 'Ветки' }).click();
    assert.equal(await page.getByRole('dialog').locator('.branch-item').count(), 1,
      'branches drawer shows a different chat');
    assert.equal(await page.getByRole('dialog').locator('.branch-item.active').count(), 1);
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.getByRole('button', { name: 'Ответвить отсюда' }).last().click();
    await page.waitForFunction(({ chatId, branchId }) => {
      const active = JSON.parse(localStorage.getItem('grani.activeBranch') || 'null');
      return active?.chatId === chatId && active.branchId !== branchId;
    }, { chatId: chatB.id, branchId: chatB.branch_id });
    const forkedActive = await page.evaluate(() => JSON.parse(localStorage.getItem('grani.activeBranch') || 'null'));
    assert.equal(forkedActive.chatId, chatB.id);
    assert.notEqual(forkedActive.branchId, chatB.branch_id);
    await page.getByRole('button', { name: 'Ветки' }).click();
    assert.equal(await page.getByRole('dialog').locator('.branch-item').count(), 2);
    await page.getByRole('button', { name: 'Закрыть панель' }).click();
    await page.locator('#new-chat').click();
    await page.waitForFunction(id => {
      const active = JSON.parse(localStorage.getItem('grani.activeBranch') || 'null');
      return active?.chatId && active.chatId !== id;
    }, chatB.id);
    await page.setViewportSize({ width: 390, height: 800 });
    const sidebar = page.locator('#sidebar');
    await page.waitForFunction(() => document.getElementById('sidebar').inert);
    assert.equal(await sidebar.evaluate(element => element.inert), true, 'closed mobile sidebar remains focusable');
    assert.equal(await sidebar.getAttribute('aria-hidden'), 'true');
    await page.getByRole('button', { name: 'Открыть список разговоров' }).click();
    await page.locator('#sidebar.open').waitFor();
    assert.equal(await sidebar.evaluate(element => element.inert), false);
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'new-chat');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'menu-toggle');
    assert.equal(await sidebar.evaluate(element => element.inert), true);
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement?.closest('#sidebar') === null), true);
    await page.getByRole('button', { name: 'Открыть список разговоров' }).click();
    await page.locator('#mobile-scrim').click({ position: { x: 380, y: 400 } });
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'menu-toggle');
    await page.setViewportSize({ width: 1365, height: 850 });
    await page.waitForFunction(() => !document.getElementById('sidebar').inert);
    assert.equal(await sidebar.evaluate(element => element.inert), false, 'desktop sidebar stayed inert');
    assert.notEqual(await sidebar.getAttribute('aria-hidden'), 'true');
    await page.setViewportSize({ width: 390, height: 800 });
    await page.waitForFunction(() => document.getElementById('sidebar').inert);
    assert.equal(await sidebar.evaluate(element => element.inert), true);
    assert.deepEqual(errors, []);
    console.log('BROWSER FIXTURE/SMOKE PASS: login, ask, draft privacy, profile, memory, conditions, branch persistence, usage, source, navigation race, mobile keyboard and resize');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
