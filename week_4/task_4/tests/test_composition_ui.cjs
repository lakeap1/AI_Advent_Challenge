// Controlled browser flow: local task-4 assets and mocked API, no model or MCP call.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULES || 'playwright');

(async () => {
  const base = process.env.TEST_APP_URL || 'http://127.0.0.1:5019';
  const root = path.resolve(__dirname, '..');
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const initialResponse = await page.request.get(base + '/api/state');
    assert.equal(initialResponse.ok(), true, 'a local app must supply the baseline state');
    const initial = await initialResponse.json();
    const state = {...initial, messages:[], requests:[], retrievals:[], composition_runs:[], composition_calls:[],
      summary:{known_cost_usd:'0',api_requests:0,cost_complete:true,unknown_cost_requests:0}};
    const profile = initial.personalization.selected_id;
    const dialogue = initial.workspace.active_dialogue.id;
    const branch = initial.active_branch;
    const digest = {profile_id:profile,selected_run_id:null,digests:[],monitor:{schedule:{enabled:false,next_due:null},runs:[]},
      conversation:{messages:[],requests:[],summary:{known_cost_usd:'0',cost_complete:true,api_requests:0}},warning:''};
    const template = fs.readFileSync(path.join(root,'templates','index.html'),'utf8')
      .replace(/\{\{ url_for\('static', filename='([^']+)'\) \}\}/g, (_,file) => '/static/' + file);
    let asks = 0, manual = 0, progressReads = 0, scenario = 'success';
    const runId = 'a'.repeat(32), failedId = 'b'.repeat(32), finalErrorId = 'c'.repeat(32);
    const question = 'Найди материалы о швах normal map и сохрани разбор';
    const savedText = 'Карта нормалей меняет направление нормали при освещении.\n\nИсточник: Wikipedia (en).';
    const finalText = 'Разбор сохранён. Проверьте отдельный блок с файлом.';
    const run = id => ({id,profile_id:profile,dialogue_id:dialogue,branch_id:branch,parent_request_id:null,
      question,query:'normal map seams',status:'running',stage:'search',
      events:[{stage:'connection',status:'done'},{stage:'search',status:'running'}],materials:null,call:null,saved:null});
    const receipt = (id, kind, metadata = {}) => ({id,status:'ok',text:'',usage_status:'reported',
      usage:{input_tokens:100,output_tokens:20,total_tokens:120},cost_usd:'0.00004',
      input_policy:{status:'accepted'},output_policy:{status:'accepted'},metadata:{kind,...metadata}});

    await page.route(base + '/', route => route.fulfill({body:template,contentType:'text/html; charset=utf-8'}));
    await page.route('**/static/*', route => {
      const name = path.basename(new URL(route.request().url()).pathname);
      assert.match(name,/^[a-z.]+$/);
      const contentType = name.endsWith('.css') ? 'text/css' : 'text/javascript';
      return route.fulfill({body:fs.readFileSync(path.join(root,'static',name)),contentType});
    });
    await page.route('**/api/state', route => route.fulfill({json:state}));
    await page.route('**/api/digest/chat', route => route.fulfill({json:{data:digest}}));
    await page.route('**/api/composition/progress?*', route => {
      progressReads++;
      assert.equal(Number(new URL(route.request().url()).searchParams.get('profile_id')),profile);
      assert.equal(Number(new URL(route.request().url()).searchParams.get('dialogue_id')),dialogue);
      return route.fulfill({json:{run:state.composition_runs.at(-1) || null}});
    });
    await page.route('**/api/composition', route => {manual++;return route.fulfill({status:404,json:{error:'manual route removed'}});});
    await page.route('**/api/ask', async route => {
      asks++;
      const payload = route.request().postDataJSON();
      assert.equal(route.request().method(),'POST');
      assert.equal(Object.hasOwn(payload,'retrieval'),false);
      assert.equal(Object.hasOwn(payload,'query'),false);
      if (scenario === 'success') {
        assert.equal(payload.prompt,question);
        const current = run(runId);
        state.composition_runs.push(current);
        await new Promise(resolve => setTimeout(resolve,1100));
        current.parent_request_id = 7;
        current.status = 'success';current.stage = 'save';
        current.events = [{stage:'connection',status:'done'},{stage:'search',status:'done'},
          {stage:'summarize',status:'done'},{stage:'save',status:'success'}];
        current.source = 'wikipedia_en';
        current.materials = {source:'wikipedia_en',provider:'wikipedia',sources:[{title:'Normal mapping'}],limitation:'Фрагменты статей.'};
        current.saved = {content:savedText,filename:runId+'.txt',bytes_written:Buffer.byteLength(savedText),sha256:'c'.repeat(64)};
        current.saved_path = 'data/composition/results/'+runId+'.txt';
        current.call = receipt(8,'composition_summary',{composition_run_id:runId,parent_request_id:7});
        state.requests = [receipt(7,'answer'),receipt(9,'mcp_step',{parent_request_id:7}),current.call];
        state.retrievals = [{provider:'stackexchange',request_id:9,query:'normal map seams',status:'ok',
          sources:[{title:'UV seams and normal maps',url:'https://blender.stackexchange.com/questions/1',excerpt:'Check tangent basis.'}]}];
        state.messages = [{id:1,request_id:7,role:'user',content:question},{id:2,request_id:7,role:'assistant',content:finalText}];
      } else if (scenario === 'ordinary') {
        assert.equal(payload.prompt,'Поясни первый пункт');
        state.requests.push(receipt(12,'answer'));
        state.messages.push({id:3,request_id:12,role:'user',content:payload.prompt},
          {id:4,request_id:12,role:'assistant',content:'Пояснение без сохранения файла.'});
      } else if (scenario === 'rejected') {
        assert.equal(payload.prompt,'Сохрани ещё один разбор');
        const current = run(failedId);state.composition_runs.push(current);
        await new Promise(resolve => setTimeout(resolve,500));
        current.parent_request_id = 20;current.status = 'rejected';current.stage = 'summarize';
        current.error = 'Ответ отклонён output policy.';
        current.events = [{stage:'connection',status:'done'},{stage:'search',status:'done'},
          {stage:'summarize',status:'rejected'}];
        state.requests.push(receipt(20,'answer'),receipt(21,'composition_summary',{composition_run_id:failedId,parent_request_id:20}));
        state.messages.push({id:5,request_id:20,role:'user',content:payload.prompt});
      } else {
        assert.equal(scenario,'saved_final_error');
        assert.equal(payload.prompt,'Сохрани разбор, затем сообщи результат');
        const current = run(finalErrorId);state.composition_runs.push(current);
        await new Promise(resolve => setTimeout(resolve,500));
        current.parent_request_id = 30;current.status = 'success';current.stage = 'save';
        current.events = [{stage:'connection',status:'done'},{stage:'search',status:'done'},
          {stage:'summarize',status:'done'},{stage:'save',status:'success'}];
        current.saved = {content:savedText,filename:finalErrorId+'.txt',bytes_written:Buffer.byteLength(savedText),sha256:'d'.repeat(64)};
        current.saved_path = 'data/composition/results/'+finalErrorId+'.txt';
        current.call = receipt(31,'composition_summary',{composition_run_id:finalErrorId,parent_request_id:30});
        const rejectedAnswer = receipt(30,'answer');
        rejectedAnswer.status = 'rejected';rejectedAnswer.text = 'Финальный ответ отклонён output policy.';
        rejectedAnswer.output_policy = {status:'rejected'};
        state.requests.push(rejectedAnswer,current.call);
        state.messages.push({id:6,request_id:30,role:'user',content:payload.prompt});
        return route.fulfill({status:502,json:{status:'error',text:rejectedAnswer.text,state}});
      }
      return route.fulfill({json:{status:'ok',state}});
    });

    await page.goto(base);
    await page.waitForFunction(() => !document.querySelector('#prompt').disabled);
    assert.equal(await page.locator('#composition-launch, #composition-enabled, #knowledge-sources, #knowledge-query').count(),0);
    assert.equal(await page.locator('textarea#prompt').count(),1);
    await page.locator('#prompt').fill(question);
    await page.locator('#send').click();
    await page.waitForSelector('.composition-pending .composition-progress[data-status="running"]');
    assert.ok(progressReads > 0,'progress must be polled during /api/ask');
    await page.waitForSelector(`.composition-progress[data-run-id="${runId}"][data-status="success"]`);
    assert.equal(asks,1);assert.equal(manual,0);
    assert.equal(await page.locator('#conversation .assistant .message-text').first().innerText(),finalText);
    assert.equal(await page.locator('.composition-saved h3').innerText(),'Сохранённый разбор');
    assert.equal(await page.locator('.composition-content').textContent(),savedText);
    assert.match(await page.locator('.composition-progress').innerText(),/Wikipedia \(en\)/);
    assert.doesNotMatch(await page.locator('.composition-progress').innerText(),/Blender Stack Exchange/);
    assert.equal(await page.locator('[data-composition-download]').getAttribute('href'),'/api/composition/'+runId+'/file');
    assert.equal(await page.locator(`#conversation [data-composition-run-id="${runId}"]`).count(),1);
    await page.locator('.hud [data-panel="sources"]').click();
    assert.match(await page.locator('#knowledge-results').innerText(),/UV seams and normal maps/);
    await page.locator('#workspace-dialog > .dialog-header [data-close-panel]').click();
    await page.locator('.hud [data-panel="usage"]').click();
    assert.match(await page.locator('#request-list').innerText(),/Шаг с источниками/);
    assert.match(await page.locator('#request-list').innerText(),/Сохранённый разбор/);
    await page.locator('#workspace-dialog > .dialog-header [data-close-panel]').click();
    await page.reload();
    await page.waitForSelector('[data-composition-download]');
    assert.equal(await page.locator(`#conversation [data-composition-run-id="${runId}"]`).count(),1);
    assert.equal(await page.locator('#conversation .message').count(),2);

    scenario = 'ordinary';
    await page.locator('#prompt').fill('Поясни первый пункт');await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelectorAll('#conversation .message').length === 4);
    assert.equal(asks,2);assert.equal(await page.locator('.composition-progress').count(),1);
    scenario = 'rejected';
    await page.locator('#prompt').fill('Сохрани ещё один разбор');await page.locator('#send').click();
    await page.waitForSelector(`.composition-progress[data-run-id="${failedId}"][data-status="rejected"]`);
    assert.equal(await page.locator(`.composition-progress[data-run-id="${failedId}"] .composition-saved`).count(),0);
    assert.equal(await page.locator(`.composition-progress[data-run-id="${failedId}"] [data-stage="save"] .step-state`).innerText(),'Не выполнялся');
    assert.match(await page.locator(`.composition-progress[data-run-id="${failedId}"]`).innerText(),/Ответ отклонён/);

    scenario = 'saved_final_error';
    await page.locator('#prompt').fill('Сохрани разбор, затем сообщи результат');await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('Финальный ответ отклонён'));
    assert.match(await page.locator('#notice').getAttribute('class'),/error/);
    const savedAfterFinalError = page.locator(`.composition-progress[data-run-id="${finalErrorId}"]`);
    assert.equal(await savedAfterFinalError.getAttribute('data-status'),'success');
    assert.equal(await savedAfterFinalError.locator('.composition-content').textContent(),savedText);
    assert.equal(await savedAfterFinalError.locator('[data-composition-download]').getAttribute('href'),'/api/composition/'+finalErrorId+'/file');
    assert.equal(await page.locator('#conversation .assistant .message-text').filter({hasText:'Финальный ответ'}).count(),0);
    assert.equal(await page.locator(`#conversation [data-composition-run-id="${finalErrorId}"]`).count(),1);
    await page.reload();
    await page.waitForSelector(`.composition-progress[data-run-id="${finalErrorId}"]`);
    assert.equal(await page.locator(`#conversation [data-composition-run-id="${finalErrorId}"]`).count(),1);
    assert.equal(await page.locator(`.composition-progress[data-run-id="${finalErrorId}"] .composition-content`).textContent(),savedText);
    assert.equal(await page.locator('#conversation .assistant .message-text').filter({hasText:'Финальный ответ'}).count(),0);

    await page.locator('#digest-chat-open').click();
    await page.waitForSelector('#digest-chat-view:visible');
    await page.waitForFunction(() => document.querySelector('#digest-feed')?.textContent.includes('Пока нет сводок'));
    assert.match(await page.locator('#digest-feed').innerText(),/Пока нет сводок/);
    await page.locator('#digest-back').click();
    assert.equal(await page.locator('#regular-workspace').isVisible(),true);
    assert.deepEqual(errors,[]);
    console.log('PASS: one normal submit, live progress, exact saved text/download, source and cost journal, reload, ordinary answer, rejection, saved file with final HTTP error, digest navigation');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
