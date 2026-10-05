// Browser fixture for the main chat. API replies are synthetic; no model call occurs.
'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULES || 'playwright');

const root = path.resolve(__dirname, '..');
const hostile = '<img src=x onerror="window.ragPwned=1">';
const source = {label:'S1',file:'corpus/normal-maps.md',line_start:12,line_end:14,
  chunk_id:'normal:12',score:0.82,text:hostile + ' Tangent-space normal.'};
const branchSource = {label:'S2',file:'corpus/hidden-branch.md',line_start:20,line_end:21,
  chunk_id:'hidden:20',score:0.75,text:'Only the alternative branch used this text.'};
const profile = (id, name) => ({id,name,style:'',format:'plain',constraints:'',refs:[]});
const profiles = [profile(1,'Первый'),profile(2,'Второй')];
const dialogue = id => ({id,task_id:id === 3 ? 2 : 1,name:'Диалог ' + id,mode:'sliding'});
const allDialogues = [dialogue(1),dialogue(2)];
const states = new Map();
for (const id of [1,2,3]) states.set(id, {
  workspace:{active_dialogue:dialogue(id),dialogues:id === 3 ? [dialogue(3)] : allDialogues,
    tasks:id === 3 ? [{id:2,name:'Другая задача'}] : [{id:1,name:'Тестовая задача'}],layers:{working:[],long_term:[]},user_id:'local'},
  personalization:{selected_id:id === 3 ? 2 : 1,profile:profiles[id === 3 ? 1 : 0],profiles},
  task_state:{task_id:id === 3 ? 2 : 1,stage:'execution',paused:false,plan:[],step_results:[]},
  invariants:{task_id:id === 3 ? 2 : 1,revision:1,rules:[]},
  messages:[],requests:[],retrievals:[],summary:{known_cost_usd:'0',cost_complete:true,unknown_cost_requests:0,api_requests:0},
  token_accounting:{known_total_tokens:0},memory:{facts:{}},checkpoints:[],branches:[],active_branch:1,
});
let activeId = 1;
let branchMessages = null;
const sent = [];
const previews = [];
let releaseFirst;
const firstPending = new Promise(resolve => {releaseFirst = resolve;});
const clone = value => JSON.parse(JSON.stringify(value));
const current = () => clone(states.get(activeId));
const parent = (id, mode, cost, usage, status = 'ok', hasSource = true) => ({id,status,text:'',
  usage:status === 'error' ? null : usage,usage_status:status === 'error' ? 'not_requested' : 'available',
  cost_usd:status === 'error' ? null : cost,
  input_policy:{status:'passed'},output_policy:{status:status === 'ok' ? 'passed' : status === 'rejected' ? 'rejected' : 'not_checked'},
  metadata:{kind:'answer',rag:{mode,status,sources:mode === 'rag' && hasSource ? [source] : [],context:mode === 'rag' && hasSource ? '[S1] Tangent-space normal.' : '',
    context_budget:{limit_tokens:5000},embedding_request_id:mode === 'rag' ? id + 1 : null}}});
const child = (id, cost, usage, usageStatus = 'available') => ({id,status:usageStatus === 'available' ? 'ok' : 'interrupted',
  text:'',usage,usage_status:usageStatus,cost_usd:cost,input_policy:{status:'passed'},output_policy:{status:'not_checked'},
  metadata:{kind:'query_embedding',parent_request_id:id - 1}});
function addResult(mode, status = 'ok', hasSource = true) {
  const state = states.get(activeId);
  const id = sent.length * 10;
  const question = mode === 'rag' ? 'Что говорит локальный корпус?' : 'Объясни normal map.';
  state.messages.push({id:state.messages.length + 1,role:'user',content:question,request_id:id});
  if (mode === 'rag') {
    state.requests.push(parent(id,mode,'0.0002',{input_tokens:20,output_tokens:10,total_tokens:30},status,hasSource));
    state.requests.push(child(id + 1,status === 'error' ? null : '0.0001',
      status === 'error' ? null : {input_tokens:5,output_tokens:0,total_tokens:5},
      status === 'error' ? 'unavailable' : 'available'));
    state.retrievals.push({request_id:id,provider:'local_index',query:question,status:status === 'error' ? 'error' : 'ok',
      sources:status !== 'error' && hasSource ? [source] : [],error:status === 'error' ? 'Embedding failed.' : ''});
  } else state.requests.push(parent(id,mode,'0.00005',{input_tokens:7,output_tokens:3,total_tokens:10}));
  if (status === 'ok') state.messages.push({id:state.messages.length + 1,role:'assistant',
    content:mode === 'rag' ? (hasSource ? 'Ответ [S1].' : 'Локальные материалы не найдены.') : 'Обычный ответ.',request_id:id});
  const called = state.requests.filter(request => request.usage_status !== 'not_requested');
  const known = called.reduce((sum, request) => sum + Number(request.cost_usd || 0), 0);
  state.summary = {known_cost_usd:String(known),cost_complete:called.every(request => request.cost_usd != null),
    unknown_cost_requests:called.filter(request => request.cost_usd == null).length,api_requests:called.length};
  state.token_accounting = {known_total_tokens:called.reduce((sum, request) => sum + (request.usage?.total_tokens || 0), 0)};
  return {status,text:status === 'rejected' ? 'Ответ отклонён политикой.' : status === 'error' ? 'Локальное встраивание прервано.' : '',state:current()};
}
function html() {
  return fs.readFileSync(path.join(root,'templates','index.html'),'utf8')
    .replace(/\{\{ url_for\('static', filename='([^']+)'\) \}\}/g, '/static/$1')
    .replace("{{ url_for('indexing_page') }}",'/indexing')
    .replace("{{ url_for('rag_page') }}",'/rag');
}
async function main() {
  const server = http.createServer(async (req,res) => {
    const url = new URL(req.url,'http://127.0.0.1');
    const send = (code,data,type='application/json') => {res.writeHead(code,{'Content-Type':type});res.end(type === 'application/json' ? JSON.stringify(data) : data);};
    if (url.pathname === '/') return send(200,html(),'text/html');
    if (url.pathname.startsWith('/static/')) {
      const file = path.resolve(root, '.' + url.pathname);
      if (!file.startsWith(path.join(root,'static') + path.sep)) return send(404,{});
      return send(200,fs.readFileSync(file),file.endsWith('.css') ? 'text/css' : 'text/javascript');
    }
    if (url.pathname === '/api/state') return send(200,current());
    if (url.pathname === '/api/preview') {
      let raw='';for await (const chunk of req) raw += chunk;
      previews.push(JSON.parse(raw));
      return send(200,{status:'ok',token_metrics:{input_text_tokens_estimate:42,rag_context_included:false,
        estimate_boundary:previews.at(-1).use_rag ? 'RAG-контекст пока неизвестен.' : 'Продолжения пока неизвестны.'}});
    }
    if (url.pathname === '/api/open' || url.pathname === '/api/profiles/select') {
      let raw='';for await (const chunk of req) raw += chunk;
      const value=JSON.parse(raw);
      activeId=url.pathname === '/api/open' ? value.dialogue_id : 3;
      return send(200,{status:'ok',state:current()});
    }
    if (url.pathname === '/api/switch') {
      let raw='';for await (const chunk of req) raw += chunk;
      const value=JSON.parse(raw);
      const state=states.get(activeId);
      state.active_branch=value.branch_id;
      state.messages=clone(branchMessages[value.branch_id]);
      return send(200,{status:'ok',state:current()});
    }
    if (url.pathname === '/api/ask') {
      let raw='';for await (const chunk of req) raw += chunk;
      const value=JSON.parse(raw);sent.push(value);
      if (sent.length === 1) await firstPending;
      const status=sent.length === 3 ? 'rejected' : sent.length === 4 ? 'error' : 'ok';
      return send(status === 'ok' ? 200 : status === 'rejected' ? 400 : 502,
        addResult(value.use_rag ? 'rag' : 'plain',status,sent.length !== 5));
    }
    return send(404,{});
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage();
    const errors=[];page.on('pageerror',error => errors.push(String(error)));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => document.querySelector('#agent-status')?.textContent === 'Готов к вопросу');
    const toggle=page.locator('#use-rag');
    assert.equal(await toggle.count(),1,'main composer exposes RAG toggle');
    assert.equal(await toggle.isChecked(),false,'default is plain mode');
    assert.match(await page.locator('label[for="use-rag"]').innerText(),/RAG|локальн/i);
    assert.match(await page.locator('#chat-evaluation-open').innerText(),/Сравнение RAG[\s\S]*10 вопросов/);
    await toggle.check();
    await page.locator('#prompt').fill('Что говорит локальный корпус?');
    await page.locator('#preview-button').click();
    await page.locator('#token-preview').waitFor({state:'visible'});
    assert.equal(previews[0].use_rag,true,'RAG preview receives the same mode flag');
    assert.match(await page.locator('#token-preview').innerText(),/RAG-контекст пока неизвестен/);
    await page.locator('#workspace-dialog [data-close-panel]').click();
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#waiting')?.hidden === false);
    assert.equal(await toggle.isDisabled(),true,'busy state locks mode');
    assert.equal(sent[0].use_rag,true,'checked sends boolean true');
    releaseFirst();
    await page.locator('.message.assistant .message-text').getByText('Ответ [S1].').waitFor();
    const card=page.locator('.message.assistant .rag-message').first();
    assert.match(await card.innerText(),/RAG/);
    await card.locator('.chat-rag-sources > summary').click();
    assert.match(await card.innerText(),/corpus\/normal-maps.md.*12.*14/);
    await card.locator('.chat-rag-source > summary').first().click();
    assert.match(await card.innerText(),/<img src=x onerror=/);
    assert.match(await card.innerText(),/0\.000300/);
    assert.match(await card.innerText(),/35/);
    assert.match(await card.textContent(),/<img src=x onerror=/);
    assert.equal(await card.locator('img').count(),0,'source text cannot create markup');
    assert.equal(await page.evaluate(() => window.ragPwned),undefined,'source remains literal text');
    await page.locator('.hud [data-panel="sources"]').click();
    assert.match(await page.locator('#knowledge-results').innerText(),/Локальная база.*corpus\/normal-maps.md/s);
    assert.equal(await page.locator('#knowledge-results .knowledge-card a').count(),0,'local file is not an external link');
    await page.locator('#workspace-dialog [data-close-panel]').click();
    await page.reload();
    await page.locator('.message.assistant .message-text').getByText('Ответ [S1].').waitFor();
    assert.match(await page.locator('.message.assistant .rag-message').first().innerText(),/0\.000300/);
    assert.equal(sent.length,1,'reload makes no new ask');
    await page.locator('#use-rag').uncheck();
    await page.locator('#prompt').fill('Объясни normal map.');
    await page.locator('#preview-button').click();
    await page.locator('#token-preview').waitFor({state:'visible'});
    assert.equal(previews[1].use_rag,false,'plain preview receives a boolean false');
    assert.match(await page.locator('#token-preview').innerText(),/Продолжения пока неизвестны/);
    await page.locator('#workspace-dialog [data-close-panel]').click();
    await page.locator('#send').click();
    await page.locator('.message.assistant .message-text').getByText('Обычный ответ.').waitFor();
    assert.equal(sent[1].use_rag,false,'unchecked sends boolean false');
    assert.match(await page.locator('.message.assistant .rag-message').last().innerText(),/Без RAG/);
    await page.locator('#use-rag').check();
    await page.locator('#prompt').fill('Что говорит локальный корпус?');
    await page.locator('#send').click();
    await page.getByText('Ответ отклонён политикой.').waitFor();
    assert.match(await page.locator('.message.user .rag-message').last().innerText(),/Ответ не опубликован/);
    assert.match(await page.locator('.message.user .rag-message').last().innerText(),/0\.000300/);
    await page.locator('#send').click();
    await page.getByText('Локальное встраивание прервано.').waitFor();
    assert.match(await page.locator('.message.user .rag-message').last().innerText(),/неполна|неизвестн/i);
    await page.locator('#send').click();
    await page.getByText('Локальные материалы не найдены.',{exact:true}).first().waitFor();
    const emptySources=page.locator('.message.assistant .chat-rag-sources').last();
    assert.match(await emptySources.locator('summary').first().innerText(),/0/);
    await emptySources.locator('summary').first().click();
    assert.match(await emptySources.innerText(),/Подходящих фрагментов не найдено/);
    const branchState=states.get(1);
    const shared=branchState.messages.slice(0,2);
    const branchTwoOnly=branchState.messages.slice(-2);
    branchMessages={1:[...shared,{id:9,role:'user',content:'Другая ветка',request_id:60},
      {id:10,role:'assistant',content:'Ответ другой ветки [S2].',request_id:60}],
      2:[...shared,...branchTwoOnly]};
    branchState.workspace.active_dialogue.mode='branching';
    branchState.workspace.dialogues[0].mode='branching';
    branchState.branches=[{id:1,name:'Основа'},{id:2,name:'Ответвление'}];
    branchState.active_branch=2;
    branchState.messages=clone(branchMessages[2]);
    const other=parent(60,'rag','0.0002',{input_tokens:20,output_tokens:10,total_tokens:30});
    other.metadata.rag.sources=[branchSource];
    branchState.requests.push(other,child(61,'0.0001',{input_tokens:5,output_tokens:0,total_tokens:5}));
    branchState.retrievals.push({request_id:60,provider:'local_index',query:'Другая ветка',status:'ok',sources:[branchSource],error:''});
    branchState.retrievals.push({request_id:90,provider:'wikipedia',query:'Внешний поиск',status:'ok',
      sources:[{title:'Открытая статья',url:'https://example.org/source',excerpt:'Внешний фрагмент'}],error:''});
    await page.locator('#state-refresh').click();
    await page.waitForFunction(() => document.querySelector('#active-mode')?.textContent === 'Ветвление');
    await page.locator('.hud [data-panel="sources"]').click();
    assert.match(await page.locator('#knowledge-results').innerText(),/normal-maps.md/,'shared checkpoint source stays visible');
    assert.doesNotMatch(await page.locator('#knowledge-results').innerText(),/hidden-branch.md/,'other branch source is hidden');
    assert.match(await page.locator('#knowledge-results').innerText(),/Wikipedia.*Внешний поиск/s,
      'existing external source history remains available');
    await page.locator('#workspace-dialog [data-close-panel]').click();
    await page.locator('.hud [data-panel="branches"]').click();
    await page.locator('#branch-select').selectOption('1');
    await page.locator('#switch-branch').click();
    await page.waitForFunction(() => document.querySelector('#active-branch')?.textContent.includes('Основа'));
    await page.locator('#workspace-dialog [data-close-panel]').click();
    await page.locator('.hud [data-panel="sources"]').click();
    assert.match(await page.locator('#knowledge-results').innerText(),/normal-maps.md/,'checkpoint source remains');
    assert.match(await page.locator('#knowledge-results').innerText(),/hidden-branch.md/,'current branch source appears');
    assert.doesNotMatch(await page.locator('#knowledge-results').innerText(),/запрос #50/,'other branch retrieval row is absent');
    await page.locator('#workspace-dialog [data-close-panel]').click();
    await page.locator('[data-dialogue-id="2"]').click();
    await page.waitForFunction(() => document.querySelector('#conversation-title')?.textContent === 'Диалог 2');
    assert.equal(await page.locator('.rag-message').count(),0,'dialogue switch clears linked RAG rows');
    const microState=states.get(2);
    microState.messages=[{id:1,role:'user',content:'Короткий вопрос без генерации',request_id:70}];
    microState.requests=[parent(70,'rag',null,null,'error',false),
      child(71,'0.0000002',{input_tokens:10,output_tokens:0,total_tokens:10})];
    microState.summary={known_cost_usd:'0.0000002',cost_complete:true,unknown_cost_requests:0,api_requests:1};
    microState.token_accounting={known_total_tokens:10};
    await page.locator('#state-refresh').click();
    await page.locator('.message.user .chat-rag-cost').waitFor();
    assert.match(await page.locator('.message.user .chat-rag-cost').innerText(),/\$0\.0000002/,
      'paid embedding remains nonzero on the message');
    await page.locator('.hud [data-panel="usage"]').click();
    assert.match(await page.locator('#chat-cost').innerText(),/\$0\.0000002/,
      'chat total remains nonzero without generation');
    assert.match(await page.locator('.request-card').filter({hasText:'Запрос #71'}).innerText(),/\$0\.0000002/,
      'child request ledger preserves microscopic cost');
    await page.locator('#workspace-dialog [data-close-panel]').click();
    await page.locator('[data-panel="profile"]').first().click();
    await page.locator('#profile-select').selectOption('2');
    await page.waitForFunction(() => document.querySelector('#profile-launch-name')?.textContent === 'Второй');
    assert.equal(await page.locator('.rag-message').count(),0,'profile switch isolates messages');
    assert.equal(errors.length,0,'no browser script errors: ' + errors.join('; '));
    console.log('CHAT_RAG_BROWSER_FIXTURE_OK: modes, busy, reload, branch sources, microcost, safe text, errors, dialogue/profile isolation');
  } finally {if (browser) await browser.close();await new Promise(resolve => server.close(resolve));}
}
main().catch(error => {console.error(error);process.exitCode=1;});
