// Synthetic HTTP fixture at the real main-chat UI boundary. No model call occurs.
'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULES || 'playwright');

const root = process.env.UI_SOURCE_ROOT ? path.resolve(process.env.UI_SOURCE_ROOT) : path.resolve(__dirname, '..');
const questions = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'evaluation/questions.json'), 'utf8')).questions;
const modes = ['rag','filter'];
const clone = value => JSON.parse(JSON.stringify(value));
const dialogue = id => ({id,task_id:1,name:'Диалог ' + id,mode:'sliding'});
const profile = {id:1,name:'Тестовый профиль',style:'',format:'plain',constraints:'',refs:[]};
const hostile = '<img src=x onerror="window.evaluationPwned=1">';
const source = {label:'S1',file:'corpus/project_readme.md',line_start:2,line_end:4,
  text:hostile + ' Полный фрагмент с точной границей. END SOURCE'};
const stores = new Map([1,2].map(id => [id, {
  workspace:{active_dialogue:dialogue(id),dialogues:[dialogue(1),dialogue(2)],tasks:[{id:1,name:'Тестовая задача'}],
    layers:{working:[],long_term:[]},user_id:'local'},
  personalization:{selected_id:1,profile,profiles:[profile]},
  task_state:{task_id:1,stage:'execution',paused:false,plan:[],step_results:[]},
  invariants:{task_id:1,revision:1,rules:[]},messages:[],requests:[],retrievals:[],
  summary:{known_cost_usd:0,cost_complete:true,unknown_cost_requests:0,api_requests:0},
  token_accounting:{known_total_tokens:0},memory:{facts:{}},checkpoints:[],branches:[],active_branch:1,
  chat_evaluations:[],chat_evaluation_requests:[],
}]));
let activeId = 1;
let starts = 0;
let asks = 0;
let questionsPosted = 0;
let traces = 0;
let assessed = false;
let pendingResolve;
const pending = new Promise(resolve => {pendingResolve = resolve;});
let holdNextTrace = false;
let releaseHeldTrace;
const heldTrace = new Promise(resolve => {releaseHeldTrace = resolve;});
const current = () => clone(stores.get(activeId));
const owner = () => ({profile_id:1,task_id:1,dialogue_id:activeId,branch_id:1});
const run = () => stores.get(activeId).chat_evaluations.at(-1);
const receipt = (runId, qid, mode) => ({id:`eval:${runId}:${qid}:${mode}:1`,status:'ok',
  text:'',usage_status:'available',usage:{input_tokens:25,output_tokens:10,total_tokens:35},cost_usd:0.0001,
  input_policy:{status:'passed'},output_policy:{status:'passed'},
  metadata:{kind:'answer',question_id:qid,mode,run_id:runId}});
function answer(qid, mode, runId) {
  const marker = `${mode.toUpperCase()} ${qid}`;
  const text = `${marker} START\n${mode === 'rag' && qid === 'q01' ? hostile : 'Фактический длинный ответ.'}\n`
    + 'Контрольный полный текст. '.repeat(24) + `\n${marker} END`;
  const id = `eval:${runId}:${qid}:${mode}:1`;
  return {status:'ok',text,code:'accepted',requests:[id],duration_ms:23,
    summary:{known_cost_usd:0.0001,cost_complete:true},
    token_accounting:{input_tokens:25,output_tokens:10,total_tokens:35},
    sources:[source],context:'[S1] ' + source.text,
    candidates:Array.from({length:20},(_,index)=>({rank:index+1,chunk_id:'project:'+index,
      file:source.file,line_start:index+2,line_end:index+3,
      text:index===0 ? source.text : 'Полный кандидат '+(index+1)+' END CANDIDATE',
      score:0.8-index/100,relevance_score:index<6 ? 3 : 1,
      reason:index<6 ? 'Факт из документа.' : 'Не отвечает на вопрос.',
      decision:index<6 ? 'selected' : 'threshold'})),
    retrieval:{counts:{candidates:20,passed:6,selected:6}},
    payloads:{model_input:'[S1] ' + source.text},
    initial_context_sha256:'same-neutral-sha'};
}
function review(question) {
  return {modes:Object.fromEntries(modes.map(mode => [mode,{
    facts:question.expected_facts.map((_,fact_index) => ({fact_index,correct:true,supported:true,
      source_labels:['S1'],reason:'Подтверждено точным фрагментом S1.'})),
    full_answer:true,unsupported_claims:[],abstained:false,reason:'Проверен полный текст.',
    candidate_sufficient:true,candidate_reason:'Кандидат содержит факт.',
    context_sufficient:true,context_reason:'Фрагмента достаточно.'}])),
    conclusion:'Четыре ответа проверены.'};
}
function html() {
  return fs.readFileSync(path.join(root,'templates','index.html'),'utf8')
    .replace(/\{\{ url_for\('static', filename='([^']+)'\) \}\}/g,'/static/$1')
    .replace("{{ url_for('indexing_page') }}",'/indexing')
    .replace("{{ url_for('rag_page') }}",'/rag');
}
async function main() {
  const server = http.createServer(async (req,res) => {
    const url = new URL(req.url,'http://127.0.0.1');
    const send = (code,data,type='application/json') => {
      res.writeHead(code,{'Content-Type':type});
      res.end(type === 'application/json' ? JSON.stringify(data) : data);
    };
    if (url.pathname === '/') return send(200,html(),'text/html');
    if (url.pathname.startsWith('/static/')) {
      const file = path.resolve(root,'.' + url.pathname);
      if (!file.startsWith(path.join(root,'static') + path.sep)) return send(404,{});
      return send(200,fs.readFileSync(file),file.endsWith('.css') ? 'text/css' : 'text/javascript');
    }
    if (url.pathname === '/api/state') return send(200,current());
    let payload = {};
    if (req.method === 'POST') {
      let raw='';for await (const chunk of req) raw += chunk;
      payload=raw ? JSON.parse(raw) : {};
    }
    if (url.pathname === '/api/open') {
      activeId=payload.dialogue_id;
      return send(200,{status:'ok',state:current()});
    }
    if (url.pathname === '/api/rag/chat/start') {
      assert.deepEqual(payload,owner());
      starts++;
      const item={id:'run-' + starts,version:3,modes:[...modes],owner:owner(),status:'running',created_at:'2026-10-04T00:00:00Z',
        gold_sha256:'gold-sha',evidence_sha256:null,reviewer:null,
        questions:questions.map(question => ({...clone(question),status:'not_started',
          pair:Object.fromEntries(modes.map(mode => [mode,null])),assessment:'pending'})),
        totals:{known_cost_usd:0,cost_complete:true,modes:{}}};
      stores.get(activeId).chat_evaluations.push(item);
      return send(200,{status:'ok',run:clone(item),state:current()});
    }
    if (url.pathname === '/api/rag/chat/question') {
      assert.deepEqual(Object.fromEntries(['profile_id','task_id','dialogue_id','branch_id'].map(key => [key,payload[key]])),owner());
      assert.equal(payload.prompt,questions.find(item => item.id === payload.question_id).question);
      questionsPosted++;
      if (questionsPosted === 1) await pending;
      const item=run();
      const question=item.questions.find(entry => entry.id === payload.question_id);
      assert.equal(question.status,'not_started','fixture refuses duplicate registration');
      question.pair=Object.fromEntries(modes.map(mode => [mode,answer(question.id,mode,item.id)]));
      question.status='complete';
      stores.get(activeId).chat_evaluation_requests.push(...modes.map(mode => receipt(item.id,question.id,mode)));
      item.totals={known_cost_usd:questionsPosted * 0.0004,cost_complete:true,
        modes:Object.fromEntries(modes.map(mode => [mode,{api_requests:questionsPosted,
          known_cost_usd:questionsPosted * 0.0001,correct_facts:questionsPosted,supported_facts:questionsPosted,
          full_answers:questionsPosted,duration_ms:questionsPosted * 23}]))};
      return send(200,{status:'ok',run:clone(item),state:current()});
    }
    if (url.pathname.startsWith('/api/rag/chat/trace/')) {
      traces++;
      const id=decodeURIComponent(url.pathname.split('/').at(-1));
      const ownedAtRequest=stores.get(activeId).chat_evaluation_requests.some(item => item.id === id && item.metadata?.redacted !== true);
      if (!ownedAtRequest) return send(404,{status:'error'});
      if (holdNextTrace) {
        holdNextTrace=false;
        await heldTrace;
        return send(200,{status:'ok',trace:{receipt_id:id,context:'STALE TRACE MUST NOT APPEAR'}});
      }
      return send(200,{status:'ok',trace:{receipt_id:id,context:'OWNER SCOPED TRACE'}});
    }
    if (url.pathname === '/api/ask') {
      asks++;
      assert.equal(payload.prompt,'Обычный вопрос');
      const state=stores.get(activeId);
      state.messages.push({id:1,role:'user',content:payload.prompt,request_id:1},
        {id:2,role:'assistant',content:'Обычный ответ.',request_id:1});
      return send(200,{status:'ok',state:current()});
    }
    return send(404,{});
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:900}});
    const errors=[];page.on('pageerror',error => errors.push(String(error)));
    const base=`http://127.0.0.1:${server.address().port}/`;
    await page.goto(base);
    await page.waitForFunction(() => document.querySelector('#agent-status')?.textContent === 'Готов к вопросу');
    assert.equal(await page.locator('#chat-evaluation-mode').count(),0,'control checkbox is absent from ordinary composer');
    assert.equal(await page.locator('#chat-evaluation-view').count(),1,'comparison has its own view');
    assert.equal(await page.locator('#chat-evaluation-view').isHidden(),true);
    await page.locator('#chat-evaluation-open').click();
    await page.locator('.chat-evaluation-run[data-run-id="run-1"]').waitFor();
    assert.equal(await page.locator('#chat-evaluation-feed-rag').count(),1,'RAG has a separate feed');
    assert.equal(await page.locator('#chat-evaluation-feed-filter').count(),1,'filtered RAG has a separate feed');
    assert.equal(await page.locator('#regular-workspace').isHidden(),true);
    assert.equal(starts,1,'opening creates a free saved run');
    await page.locator('#chat-evaluation-insert').click();
    assert.equal(await page.locator('#chat-evaluation-prompt').inputValue(),questions[0].question);
    await page.locator('#chat-evaluation-send').click();
    await page.waitForFunction(() => document.querySelector('#waiting')?.hidden === false);
    assert.equal(await page.locator('#chat-evaluation-new-run').isDisabled(),true,'busy state locks run mutation');
    pendingResolve();
    await page.locator('#chat-evaluation-feed-rag .chat-evaluation-answer-text').getByText(/RAG q01 END/).waitFor();
    await page.locator('#chat-evaluation-feed-filter .chat-evaluation-answer-text').getByText(/FILTER q01 END/).waitFor();
    assert.equal(questionsPosted,1);
    assert.equal(await page.locator('.chat-evaluation-lane').count(),2);
    for (const mode of modes) {
      const card=page.locator(`.chat-evaluation-lane[data-mode="${mode}"] .chat-evaluation-answer`).first();
      assert.match(await card.innerText(),/START[\s\S]*END/,'full answer is visible');
      assert.match(await card.innerText(),/project_readme.md.*строки 2–4/);
      assert.doesNotMatch(await card.textContent(),/cosine|Сходство|запрос для поиска|USD|Ожидаемые факты|payload|TRACE/i);
      assert.equal(await card.locator('img').count(),0,'source remains literal text');
      const sourceDetail=card.locator('.chat-evaluation-source');
      await sourceDetail.locator('summary').focus();
      await page.keyboard.press('Enter');
      assert.equal(await sourceDetail.evaluate(item => item.open),true,'source opens by keyboard');
      assert.match(await sourceDetail.innerText(),/END SOURCE/);
    }
    assert.equal(await page.evaluate(() => window.evaluationPwned),undefined);
    await page.locator('#chat-evaluation-insert').click();
    await page.locator('#chat-evaluation-send').click();
    await page.locator('#chat-evaluation-feed-filter .chat-evaluation-answer[data-question-id="q02"] .chat-evaluation-answer-text')
      .getByText(/FILTER q02 END/).waitFor();
    assert.equal(questionsPosted,2);
    const ragFeed=page.locator('#chat-evaluation-feed-rag');
    const filterFeed=page.locator('#chat-evaluation-feed-filter');
    const geometry=await page.locator('.chat-evaluation-feed').evaluateAll(items => items.map(item => ({
      clientHeight:item.clientHeight,scrollHeight:item.scrollHeight,bottom:item.getBoundingClientRect().bottom,
      viewport:innerHeight,
    })));
    for (const item of geometry) {
      assert.ok(item.scrollHeight>item.clientHeight,'each lane scrolls its own complete answers');
      assert.ok(item.bottom<=item.viewport,'feed remains inside visible camera viewport');
    }
    await ragFeed.evaluate(item => {item.scrollTop=80;item.dispatchEvent(new Event('scroll'));});
    const filterTop=await filterFeed.evaluate(item => item.scrollTop);
    assert.equal(filterTop,0,'RAG feed scroll does not move filtered feed');
    await page.locator('#chat-evaluation-question-select').selectOption('q01');
    assert.equal(await page.locator('#chat-evaluation-feed-rag .chat-evaluation-answer').count(),2,'saved history remains in feed');
    const assessedRun=stores.get(1).chat_evaluations[0];
    const assessedQuestion=assessedRun.questions[0];
    assessedQuestion.assessment=review(assessedQuestion);
    assessedQuestion.assessment.conclusion='Вывод q01: фильтр сохранил опору в локальном корпусе.';
    assessedQuestion.assessment.modes.rag.full_answer=false;
    assessedQuestion.assessment.modes.rag.unsupported_claims=['Неподтверждённая деталь о шейдере.'];
    assessedQuestion.assessment.modes.rag.candidate_sufficient=false;
    assessedQuestion.assessment.modes.rag.candidate_reason='Нужный факт отсутствует в кандидатах.';
    assessedQuestion.assessment.modes.rag.context_sufficient=false;
    assessedQuestion.assessment.modes.rag.context_reason='Переданный контекст не покрывает вопрос.';
    assessedRun.totals.paired={filter:{gains:['q01'],losses:['q02']}};
    await page.locator('#chat-evaluation-refresh').click();
    await page.locator('#chat-evaluation-report > summary').click();
    const report=page.locator('#chat-evaluation-report');
    assert.match(await report.innerText(),/Набор.*10.*фиксирован.*вопрос/i,'report states its fixed sample limit');
    assert.match(await report.innerText(),/контроль.*не выполнен/i,'report does not invent a complete control run');
    assert.match(await report.innerText(),/Время, мс.*46/s,'report includes saved duration metric');
    assert.match(await report.innerText(),/Выигрыш.*q01.*Потеря.*q02/s,'report exposes saved paired gains and losses');
    await report.locator('.chat-evaluation-assessment').first().locator('summary').click();
    assert.match(await report.innerText(),/Вывод q01: фильтр сохранил опору/);
    assert.match(await report.innerText(),/Неподтверждённая деталь о шейдере/);
    assert.match(await report.innerText(),/Отказ: нет/);
    assert.match(await report.innerText(),/Кандидаты достаточны: нет.*Нужный факт отсутствует в кандидатах/s);
    assert.match(await report.innerText(),/Контекст достаточен: нет.*Переданный контекст не покрывает вопрос/s);
    assert.doesNotMatch(await page.locator('.chat-evaluation-lane .chat-evaluation-answer').first().textContent(),
      /Неподтверждённая деталь|Кандидаты достаточны|Вывод q01/,'assessment remains outside answer cards');
    await page.reload();
    await page.locator('.chat-evaluation-run[data-run-id="run-1"]').waitFor();
    assert.equal(questionsPosted,2,'reload reads saved answers without a model call');
    assert.equal(starts,1,'reload reads selected run');
    const q05=stores.get(1).chat_evaluations[0].questions[4];
    q05.status='complete';
    q05.pair={rag:answer('q05','rag','run-1'),filter:{...answer('q05','filter','run-1'),status:'no_context',text:'',sources:[],generation_requested:false}};
    await page.locator('#chat-evaluation-refresh').click();
    await page.locator('#chat-evaluation-question-select').selectOption('q05');
    assert.match(await page.locator('#chat-evaluation-feed-filter .chat-evaluation-answer[data-question-id="q05"]').innerText(),/контекст не найден.*Генерация не запускалась/s);
    assert.doesNotMatch(await page.locator('#chat-evaluation-feed-filter .chat-evaluation-answer[data-question-id="q05"]').textContent(),/FILTER q05 END/);
    const historical=clone(stores.get(1).chat_evaluations[0]);
    historical.id='legacy-1';historical.version=2;historical.modes=['rag','rewrite','filter','rewrite_filter'];
    historical.questions[0].pair.rewrite=answer('q01','rewrite','legacy-1');
    historical.questions[0].pair.rewrite_filter=answer('q01','rewrite_filter','legacy-1');
    stores.get(1).chat_evaluations.push(historical);
    await page.locator('#chat-evaluation-refresh').click();
    await page.locator('#chat-evaluation-run-select').selectOption('legacy-1');
    assert.match(await page.locator('.chat-evaluation-legacy').innerText(),/Архивный прогон.*продолжить.*нельзя/);
    assert.equal(await page.locator('#chat-evaluation-form').count(),0,'legacy run is read-only');
    await page.locator('#chat-evaluation-report > summary').click();
    assert.match(await page.locator('#chat-evaluation-report').innerText(),/Переформулировка/,'legacy report retains four mode names');
    await page.locator('#chat-evaluation-run-select').selectOption('run-1');
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.locator('.chat-evaluation-lane:visible').count(),1);
    await page.locator('#chat-evaluation-mobile-mode').selectOption('filter');
    assert.equal(await page.locator('.chat-evaluation-lane[data-mode="filter"]:visible').count(),1);
    assert.equal(await page.locator('.chat-evaluation-lane[data-mode="rag"]:visible').count(),0);
    await page.locator('#chat-evaluation-close').click();
    assert.equal(await page.locator('#regular-workspace').isVisible(),true);
    await page.locator('#prompt').fill('Обычный вопрос');
    await page.locator('#send').click();
    await page.locator('.message.assistant .message-text').getByText('Обычный ответ.',{exact:true}).waitFor();
    assert.equal(asks,1,'ordinary composer still posts /api/ask');
    await page.locator('#regular-workspace [data-shell-toggle="chats"]').click();
    await page.locator('#chat-evaluation-open').click();
    await page.locator('.chat-evaluation-run[data-run-id="run-1"]').waitFor();
    assert.equal(starts,1,'reopening saved view does not create a run');
    await page.locator('#chat-evaluation-expense').click();
    holdNextTrace=true;
    const tracePending=page.waitForRequest(request => request.url().includes('/api/rag/chat/trace/'));
    await page.locator('#request-list [data-chat-evaluation-trace]').first().click();
    await tracePending;
    assert.equal(traces,1,'expense panel requested one owner-scoped trace');
    await page.locator('#workspace-dialog [data-close-panel]').click();
    const branchState=stores.get(1);
    const originalRuns=branchState.chat_evaluations;
    const originalReceipts=branchState.chat_evaluation_requests;
    branchState.active_branch=2;
    branchState.chat_evaluations=[];
    branchState.chat_evaluation_requests=originalReceipts.map(item => ({...clone(item),text:'',
      metadata:{kind:'chat_evaluation',redacted:true}}));
    await page.locator('#chat-evaluation-refresh').click();
    await page.waitForFunction(() => document.querySelector('#chat-evaluation-view')?.hidden === true);
    const delayedResponse=page.waitForResponse(response => response.url().includes('/api/rag/chat/trace/'));
    releaseHeldTrace();
    await delayedResponse;
    await page.locator('#regular-workspace .hud [data-panel="usage"]').click();
    assert.doesNotMatch(await page.locator('#trace-content').innerText(),/STALE TRACE MUST NOT APPEAR/,
      'late owner trace is not rendered in the other branch');
    assert.equal(await page.locator('#request-list [data-chat-evaluation-trace]').count(),0,
      'redacted receipts in the other branch have no trace action');
    await page.locator('#workspace-dialog [data-close-panel]').click();
    branchState.active_branch=1;
    branchState.chat_evaluations=originalRuns;
    branchState.chat_evaluation_requests=originalReceipts;
    await page.locator('#state-refresh').click();
    await page.locator('.chat-evaluation-run[data-run-id="run-1"]').waitFor();
    assert.equal(await page.locator('#chat-evaluation-view').isVisible(),true,
      'owner-scoped comparison view is restored for its original branch');
    await page.locator('#chat-evaluation-view [data-shell-toggle="chats"]').click();
    await page.locator('#dialogue-list [data-dialogue-id="2"]').click();
    await page.waitForFunction(() => document.querySelector('#conversation-title')?.textContent === 'Диалог 2');
    assert.equal(await page.locator('#chat-evaluation-view').isHidden(),true,'owner change hides old comparison');
    assert.deepEqual(errors,[],'no uncaught browser errors');
    console.log('TWO_CHAT_BROWSER_FIXTURE_OK: separate feeds, full answers/sources only, no_context, legacy read-only, owner, reload, mobile, ordinary chat');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
