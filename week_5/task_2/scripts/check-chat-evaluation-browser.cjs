// Synthetic HTTP fixture at the real main-chat UI boundary. No model call occurs.
'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULES || 'playwright');

const root = path.resolve(__dirname, '..');
const questions = JSON.parse(fs.readFileSync(path.join(root, 'evaluation/questions.json'), 'utf8')).questions;
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
let failNextQuestion=false;
let traces = 0;
let assessed = false;
let pendingResolve;
const pending = new Promise(resolve => {pendingResolve = resolve;});
let holdNextTrace = false;
let releaseHeldTrace;
let heldTrace = new Promise(resolve => {releaseHeldTrace = resolve;});
const current = () => clone(stores.get(activeId));
const owner = () => ({profile_id:1,task_id:1,dialogue_id:activeId,branch_id:1});
const run = () => stores.get(activeId).chat_evaluations.at(-1);
const receipt = (runId, qid, mode) => ({id:`eval:${runId}:${qid}:${mode}:1`,status:'ok',
  text:'',usage_status:'available',usage:{input_tokens:25,output_tokens:10,total_tokens:35},cost_usd:0.0001,
  input_policy:{status:'passed'},output_policy:{status:'passed'},
  metadata:{kind:'answer',question_id:qid,mode,run_id:runId}});
function answer(qid, mode, runId) {
  const marker = `${mode.toUpperCase()} ${qid}`;
  const text = `${marker} START\n${mode === 'plain' && qid === 'q01' ? hostile : 'Фактический длинный ответ.'}\n`
    + 'Контрольный полный текст. '.repeat(24) + `\n${marker} END`;
  const id = `eval:${runId}:${qid}:${mode}:1`;
  return {status:'ok',text,code:'accepted',requests:[id],duration_ms:23,
    summary:{known_cost_usd:0.0001,cost_complete:true},
    token_accounting:{input_tokens:25,output_tokens:10,total_tokens:35},
    sources:mode === 'rag' ? [source] : [],context:mode === 'rag' ? '[S1] ' + source.text : '',
    payloads:{model_input:mode === 'rag' ? '[S1] ' + source.text : 'neutral'},
    initial_context_sha256:'same-neutral-sha'};
}
function review(question) {
  return {facts:question.expected_facts.map((_,fact_index) => ({fact_index,plain_correct:true,
    plain_reason:'Факт найден в полном plain ответе.',rag_correct:true,rag_supported:true,
    rag_source_labels:['S1'],rag_reason:'Подтверждено точным фрагментом S1.'})),
  plain:{full_answer:true,unsupported_claims:[],abstained:false,reason:'Проверен полный текст.'},
  rag:{full_answer:true,unsupported_claims:[],abstained:false,reason:'Ответ опирается на S1.'},
  context_sufficient:true,context_reason:'Фрагмента достаточно.',conclusion:'Оба ответа проверены.'};
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
    if (url.pathname === '/api/digest/chat') return send(200,{status:'ok',data:{profile_id:1,monitor:{schedule:null,runs:[]},digests:[],selected_run_id:null,conversation:{messages:[],requests:[],summary:{known_cost_usd:0,cost_complete:true,api_requests:0}}}});
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
      const item={id:'run-' + starts,owner:owner(),status:'running',created_at:'2026-10-04T00:00:00Z',
        gold_sha256:'gold-sha',evidence_sha256:null,reviewer:null,
        questions:questions.map(question => ({...clone(question),status:'not_started',
          pair:{plain:null,rag:null},assessment:'pending'})),totals:{known_cost_usd:0,cost_complete:true}};
      stores.get(activeId).chat_evaluations.push(item);
      return send(200,{status:'ok',run:clone(item),state:current()});
    }
    if (url.pathname === '/api/rag/chat/question') {
      assert.deepEqual(Object.fromEntries(['profile_id','task_id','dialogue_id','branch_id'].map(key => [key,payload[key]])),owner());
      assert.equal(payload.prompt,questions.find(item => item.id === payload.question_id).question);
      questionsPosted++;
      if (questionsPosted === 1) await pending;
      const item=run();
      if(failNextQuestion) {
        failNextQuestion=false;
        const failed=item.questions.find(q=>q.id===payload.question_id);
        failed.status='failed';
        failed.pair={plain:{...answer(failed.id,'plain',item.id),status:'api_error',text:'',code:'api_error',error:'HTTP fixture interruption',summary:{known_cost_usd:0,cost_complete:false,unknown_cost_requests:1}},rag:null};
        return send(503,{status:'error',text:'HTTP fixture interruption',state:current()});
      }
      const question=item.questions.find(entry => entry.id === payload.question_id);
      assert.equal(question.status,'not_started','fixture refuses duplicate registration');
      question.pair={plain:answer(question.id,'plain',item.id),rag:answer(question.id,'rag',item.id)};
      question.status='complete';
      stores.get(activeId).chat_evaluation_requests.push(receipt(item.id,question.id,'plain'),receipt(item.id,question.id,'rag'));
      item.totals={known_cost_usd:questionsPosted * 0.0002,cost_complete:true};
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
    const refresh=async()=>{const button=page.locator(await page.locator('#comparison-refresh').isVisible()?'#comparison-refresh':'#state-refresh');await button.click();};
    await page.goto(base);
    await page.waitForFunction(() => document.querySelector('#agent-status')?.textContent === 'Готов к вопросу');
    assert.equal(await page.locator('#chat-evaluation-mode').count(),0,'ordinary chat has no second checkbox/mode');
    await page.locator('#chat-evaluation-open').click();
    await page.locator('#comparison-view').waitFor({state:'visible'});
    assert.equal(await page.locator('#regular-workspace').isVisible(),false);
    assert.equal(await page.locator('#digest-chat-view').isVisible(),false);
    assert.equal(starts,0,'tab viewing does not create paid calls or runs');
    await page.locator('#chat-evaluation-new-run').click();
    await page.locator('#comparison-view[data-run-id="run-1"]').waitFor();
    assert.equal(starts,1);
    assert.equal(questionsPosted,0);
    await page.locator('#chat-evaluation-insert-question').click();
    assert.equal(await page.locator('#comparison-prompt').inputValue(),questions[0].question);
    assert.equal(questionsPosted,0,'insert is read only');
    await page.locator('#comparison-send').click();
    await page.waitForFunction(() => document.querySelector('#comparison-send')?.disabled);
    assert.equal(await page.locator('#chat-evaluation-new-run').isDisabled(),true,'busy blocks duplicate run');
    assert.equal(await page.locator('#comparison-prompt').isDisabled(),true,'busy blocks composer');
    pendingResolve();
    await page.locator('.chat-evaluation-answer[data-question-id="q01"][data-mode="rag"] .chat-evaluation-answer-text').getByText(/RAG q01 END/).waitFor();
    for(let index=1;index<10;index++) {
      await page.locator('#chat-evaluation-insert-question').click();
      assert.equal(await page.locator('#comparison-prompt').inputValue(),questions[index].question);
      await page.locator('#comparison-send').click();
      await page.locator(`.chat-evaluation-answer[data-question-id="${questions[index].id}"][data-mode="rag"] .chat-evaluation-answer-text`).getByText(new RegExp(`RAG ${questions[index].id} END`)).waitFor();
    }
    assert.equal(questionsPosted,10);
    assert.equal(await page.locator('.chat-evaluation-answer').count(),20,'two entire histories retain all ten pairs');
    await page.locator('#chat-evaluation-question-select').selectOption('q01');
    assert.equal(questionsPosted,10,'jumping to a question is read only');
    const ragBox=await page.locator('#comparison-rag').boundingBox();
    const plainBox=await page.locator('#comparison-plain').boundingBox();
    assert.ok(ragBox.x < plainBox.x && Math.abs(ragBox.y-plainBox.y)<5,'desktop RAG is left, plain is right');
    for(const question of questions) {
      for(const mode of ['rag','plain']) {
        const card=page.locator(`.chat-evaluation-answer[data-question-id="${question.id}"][data-mode="${mode}"]`);
        assert.match(await card.locator('.chat-evaluation-answer-text').innerText(),new RegExp(`${mode.toUpperCase()} ${question.id} START[\\s\\S]*${mode.toUpperCase()} ${question.id} END`));
        assert.equal(await card.locator('.chat-evaluation-source').count(),mode==='rag'?1:0,'actual sources belong only to RAG');
        const questionText=page.locator(`#comparison-${mode} .chat-evaluation-exchange[data-question-id="${question.id}"] .chat-evaluation-user`);
        assert.ok((await questionText.innerText()).includes(question.question),'both histories show the exact same question');
      }
    }
    assert.equal(await page.locator('#comparison-view img').count(),0);
    assert.equal(await page.evaluate(()=>window.evaluationPwned),undefined,'hostile HTML remains text');
    const firstSource=page.locator('.chat-evaluation-answer[data-question-id="q01"][data-mode="rag"] .chat-evaluation-source').first();
    await firstSource.locator('summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await firstSource.evaluate(el=>el.open),true,'native source details supports keyboard');
    assert.match(await firstSource.innerText(),/END SOURCE/);
    assert.equal(questionsPosted,10);
    const plainOne=stores.get(1).chat_evaluations[0].questions[0].pair.plain;
    plainOne.summary.cost_complete=false;plainOne.summary.unknown_cost_requests=1;
    await refresh();
    await page.waitForFunction(()=>document.querySelector('.chat-evaluation-answer[data-question-id="q01"][data-mode="plain"] .chat-evaluation-usage')?.textContent.includes('сумма неполная'));
    await page.reload();
    await page.locator('#comparison-view[data-run-id="run-1"]').waitFor();
    assert.equal(await page.locator('#comparison-view').isVisible(),true,'sidebar view survives reload');
    assert.equal(await firstSource.evaluate(el=>el.open),true,'source detail survives reload');
    assert.equal(starts,1);assert.equal(questionsPosted,10,'reload never pays again');
    const stored=stores.get(1).chat_evaluations[0];
    stored.questions.forEach(q=>q.assessment=review(q));stored.status='assessed';stored.reviewer={kind:'independent',model:'fixture-only'};
    stored.totals={known_cost_usd:0.002,cost_complete:true,plain_full:10,rag_full:10};
    await refresh();
    await page.waitForFunction(()=>document.querySelector('.chat-evaluation-summary')?.textContent.includes('10 из 10'));
    assert.equal(await page.locator('#comparison-totals .chat-evaluation-assessment').count(),10,'full control set has ten reviews');
    const interrupted=stored.questions[7],oldRag=clone(interrupted.pair.rag);
    interrupted.pair.rag={...oldRag,status:'interrupted',text:'',code:'interrupted',error:'Процесс прерван. Автоматического повтора нет.'};
    await refresh();
    await page.locator('.chat-evaluation-answer[data-question-id="q08"][data-mode="rag"] .chat-evaluation-error').waitFor();
    assert.equal(questionsPosted,10,'error display does not retry');
    interrupted.pair.rag=oldRag;
    await refresh();
    await page.locator('#digest-chat-open').click();
    await page.locator('#digest-chat-view').waitFor({state:'visible'});
    assert.equal(await page.locator('#comparison-view').isVisible(),false,'digest and comparison are mutually exclusive');
    assert.equal(await page.locator('#regular-workspace').isVisible(),false);
    await page.locator('#chat-evaluation-open').click();
    await page.locator('#comparison-view').waitFor({state:'visible'});
    assert.equal(await page.locator('#digest-chat-view').isVisible(),false);
    const oldRuns=stores.get(1).chat_evaluations,oldReceipts=stores.get(1).chat_evaluation_requests;
    const redacted=oldReceipts.map(item=>({...clone(item),text:'',metadata:{kind:'chat_evaluation',redacted:true}}));
    const branchState=stores.get(1);
    await page.locator('#chat-evaluation-question-select').selectOption('q01');
    const plainTech=page.locator('.chat-evaluation-answer[data-question-id="q01"][data-mode="plain"] details').filter({has:page.locator('[data-chat-evaluation-trace]')}).first();
    if(!await plainTech.evaluate(el=>el.open))await plainTech.locator('summary').first().click();
    holdNextTrace=true;
    const delayedOwnerRequest=page.waitForRequest(r=>r.url().includes('/api/rag/chat/trace/'));
    await plainTech.locator('[data-chat-evaluation-trace]').first().click();await delayedOwnerRequest;
    branchState.active_branch=2;branchState.chat_evaluations=[];branchState.chat_evaluation_requests=redacted;
    await refresh();
    await page.waitForFunction(()=>document.querySelectorAll('.chat-evaluation-answer').length===0);
    assert.equal(await page.locator('.chat-evaluation-source').count(),0,'other branch has no RAG text');
    const delayedOwnerResponse=page.waitForResponse(r=>r.url().includes('/api/rag/chat/trace/'));
    releaseHeldTrace();await delayedOwnerResponse;await page.waitForTimeout(100);
    assert.doesNotMatch(await page.locator('body').innerText(),/STALE TRACE MUST NOT APPEAR/,'owner switch discards a late trace');
    branchState.active_branch=1;branchState.chat_evaluations=oldRuns;branchState.chat_evaluation_requests=oldReceipts;
    await refresh();
    await page.locator('#comparison-view[data-run-id="run-1"]').waitFor();
    const secondary={...clone(stored),id:'secondary-fixture',questions:questions.map(q=>({...clone(q),status:'not_started',pair:{plain:null,rag:null},assessment:'pending'}))};
    branchState.chat_evaluations.push(secondary);await refresh();
    await page.locator('#chat-evaluation-question-select').selectOption('q01');
    const runTech=page.locator('.chat-evaluation-answer[data-question-id="q01"][data-mode="plain"] details').filter({has:page.locator('[data-chat-evaluation-trace]')}).first();
    if(!await runTech.evaluate(el=>el.open))await runTech.locator('summary').first().click();
    heldTrace=new Promise(resolve=>{releaseHeldTrace=resolve;});holdNextTrace=true;
    const delayedRunRequest=page.waitForRequest(r=>r.url().includes('/api/rag/chat/trace/'));
    await runTech.locator('[data-chat-evaluation-trace]').first().click();await delayedRunRequest;
    await page.locator('#chat-evaluation-run-select').selectOption('secondary-fixture');
    const delayedRunResponse=page.waitForResponse(r=>r.url().includes('/api/rag/chat/trace/'));
    releaseHeldTrace();await delayedRunResponse;await page.waitForTimeout(100);
    assert.doesNotMatch(await page.locator('body').innerText(),/STALE TRACE MUST NOT APPEAR/,'run switch discards late trace');
    await page.locator('#chat-evaluation-run-select').selectOption('run-1');branchState.chat_evaluations.pop();await refresh();
    await page.locator('#dialogue-list [data-dialogue-id="2"]').click();
    await page.waitForFunction(()=>document.querySelector('#conversation-title')?.textContent==='Диалог 2');
    assert.equal(await page.locator('.chat-evaluation-answer').count(),0,'other dialogue cannot see saved answers');
    await page.locator('#dialogue-list [data-dialogue-id="1"]').click();
    await page.waitForFunction(()=>document.querySelector('#conversation-title')?.textContent==='Диалог 1');
    await page.locator('#prompt').fill('Обычный вопрос');
    await page.locator('#send').click();
    await page.locator('.message.assistant .message-text').getByText('Обычный ответ.',{exact:true}).waitFor();
    assert.equal(asks,1,'ordinary chat keeps its actual /api/ask route');
    await page.locator('#chat-evaluation-open').click();
    await page.locator('#comparison-view[data-run-id="run-1"]').waitFor();
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.locator('#comparison-mobile-mode').isVisible(),true);
    await page.locator('#comparison-mobile-mode').selectOption('rag');
    assert.equal(await page.locator('#comparison-rag').isVisible(),true);
    assert.ok((await page.locator('#comparison-rag').boundingBox()).height>=250,'mobile leaves a readable chat viewport, not a tiny strip');
    assert.equal(await page.locator('#comparison-plain').isVisible(),false);
    await page.locator('#comparison-mobile-mode').selectOption('plain');
    assert.equal(await page.locator('#comparison-plain').isVisible(),true);
    assert.equal(await page.locator('#comparison-rag').isVisible(),false);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'mobile has no horizontal overflow');
    assert.equal(await page.locator('.chat-evaluation-answer').count(),20,'mobile toggles preserve both histories');
    assert.equal(starts,1);assert.equal(questionsPosted,10);
    await page.setViewportSize({width:1440,height:900});
    await page.locator('#chat-evaluation-new-run').click();
    await page.locator('#comparison-view[data-run-id="run-2"]').waitFor();
    await page.locator('#chat-evaluation-insert-question').click();failNextQuestion=true;
    const failedResponse=page.waitForResponse(r=>r.url().endsWith('/api/rag/chat/question'));
    await page.locator('#comparison-send').click();assert.equal((await failedResponse).status(),503);
    await page.waitForFunction(()=>!document.querySelector('#comparison-prompt').disabled);
    assert.match(await page.locator('.chat-evaluation-answer[data-mode="rag"]').innerText(),/API-вызов.*не запускался/,'skipped RAG distinguishes no call from unknown spend');
    assert.match(await page.locator('.chat-evaluation-answer[data-mode="plain"]').innerText(),/сумма неполная/);
    await page.waitForTimeout(100);
    assert.equal(questionsPosted,11,'HTTP failure has no automatic retry');
    assert.equal(traces,2,'both delayed trace races actually ran');
    assert.deepEqual(errors,[],'no uncaught browser errors');
    console.log('CHAT_EVALUATION_BROWSER_FIXTURE_OK: sidebar tab, mutually exclusive digest/regular/comparison, two histories with 10 exact questions and 20 full answers, RAG-only sources, safe text, no API on view/insert/reload, owner/branch boundaries, delayed trace owner/run races, HTTP failure/no retry/skipped-stage accounting, busy, keyboard and mobile');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
