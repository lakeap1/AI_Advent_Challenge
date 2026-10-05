// Synthetic main-chat browser fixture. It never calls a model or provider.
'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULES || 'playwright');

const root = path.resolve(__dirname, '..');
const hostile = '<img src=x onerror="window.ragPwned=1">';
const used = {label:'S1',source:'corpus/project_readme.md',section:'Reports',file:'corpus/project_readme.md',
  line_start:12,line_end:14,chunk_id:'report:12',text:hostile + ' Reports are UTF-8.'};
const unused = {label:'S2',source:'corpus/unused.md',section:'Other',file:'corpus/unused.md',
  chunk_id:'unused:1',text:'A selected but uncited candidate.'};
const profile = (id,name) => ({id,name,style:'',format:'plain',constraints:'',refs:[]});
const profiles = [profile(1,'Первый'),profile(2,'Второй')];
const dialogue = id => ({id,task_id:id === 3 ? 2 : 1,name:'Диалог ' + id,mode:'sliding'});
const states = new Map();
for (const id of [1,2,3]) states.set(id, {
  workspace:{active_dialogue:dialogue(id),dialogues:id === 3 ? [dialogue(3)] : [dialogue(1),dialogue(2)],
    tasks:id === 3 ? [{id:2,name:'Другая задача'}] : [{id:1,name:'Тестовая задача'}],
    layers:{working:[],long_term:[]},user_id:'local'},
  personalization:{selected_id:id === 3 ? 2 : 1,profile:profiles[id === 3 ? 1 : 0],profiles},
  task_state:{task_id:id === 3 ? 2 : 1,stage:'execution',paused:false,plan:[],step_results:[]},
  invariants:{task_id:id === 3 ? 2 : 1,revision:1,rules:[]},
  messages:[],requests:[],retrievals:[],summary:{known_cost_usd:'0',cost_complete:true,unknown_cost_requests:0,api_requests:0},
  token_accounting:{known_total_tokens:0},memory:{facts:{}},checkpoints:[],branches:[],active_branch:1,
  rag_settings:{top_k_before:20,top_k_after:6,relevance_threshold:2,context_budget_utf8_bytes:6000},
});
let activeId = 1;
const sent = [], previews = [];
let releaseFirst;
const firstPending = new Promise(resolve => {releaseFirst = resolve;});
const clone = value => JSON.parse(JSON.stringify(value));
const current = () => clone(states.get(activeId));
function request(id,status,text,withUsed) {
  return {id,status,text,usage:null,usage_status:'not_requested',cost_usd:null,
    input_policy:{status:'passed'},output_policy:{status:status === 'ok' ? 'passed' : 'not_checked'},
    metadata:{kind:'answer',rag:{mode:'filter',status:status === 'no_context' ? 'no_context' : 'ok',
      sources:status === 'no_context' ? [] : [used,unused],
      used_sources:withUsed ? [used] : [],
      grounding:{status:withUsed ? 'answered' : 'unknown',claims:withUsed ?
        [{text:'Отчёты используют UTF-8.',source_labels:['S1']}] : [],
        clarification:withUsed ? '' : 'Уточните вопрос о материалах локальной базы.'},
      candidates:[{chunk_id:used.chunk_id,decision:'selected'}]}}};
}
function addResult(status) {
  const state = states.get(activeId), id = sent.length * 10;
  state.messages.push({id:state.messages.length + 1,role:'user',content:sent.at(-1).prompt,request_id:id});
  const text = status === 'ok' ? 'Отчёты используют UTF-8. [S1]' :
    status === 'no_context' ? 'Не знаю. Уточните вопрос о материалах локальной базы.' :
      'Ответ не прошёл проверку источников.';
  state.requests.push(request(id,status,text,status === 'ok'));
  if (status === 'ok') state.messages.push({id:state.messages.length + 1,role:'assistant',content:text,request_id:id});
  state.retrievals.push({request_id:id,provider:'local_index',query:sent.at(-1).prompt,status:'ok',
    sources:status === 'no_context' ? [] : [used,unused]});
  return {status,text,state:current()};
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
    if (url.pathname === '/rag') return send(200,
      fs.readFileSync(path.join(root,'templates','rag.html'),'utf8'),'text/html');
    if (url.pathname.startsWith('/static/')) {
      const file = path.resolve(root,'.' + url.pathname);
      if (!file.startsWith(path.join(root,'static') + path.sep)) return send(404,{});
      return send(200,fs.readFileSync(file),file.endsWith('.css') ? 'text/css' : 'text/javascript');
    }
    if (url.pathname === '/api/state') return send(200,current());
    if (url.pathname === '/api/open' || url.pathname === '/api/profiles/select') {
      let raw='';for await (const chunk of req) raw += chunk;
      const value=JSON.parse(raw);
      activeId=url.pathname === '/api/open' ? value.dialogue_id : 3;
      return send(200,{status:'ok',state:current()});
    }
    if (url.pathname === '/api/preview') {
      let raw='';for await (const chunk of req) raw += chunk;
      previews.push(JSON.parse(raw));
      return send(200,{status:'ok',token_metrics:{input_text_tokens_estimate:42,
        estimate_boundary:'Точный объём найденных фрагментов станет известен при запросе.'}});
    }
    if (url.pathname === '/api/ask') {
      let raw='';for await (const chunk of req) raw += chunk;
      sent.push(JSON.parse(raw));
      if (sent.length === 1) await firstPending;
      const status=sent.length === 2 ? 'no_context' : sent.length === 3 ? 'rejected' : 'ok';
      return send(status === 'rejected' ? 400 : 200,addResult(status));
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
    assert.equal(await page.locator('#use-rag, #rag-mode, #chat-evaluation-open, #chat-evaluation-view').count(),0,
      'main chat has no RAG mode or comparison controls');
    assert.equal(await page.locator('#rag-settings').count(),0,
      'composer does not expose internal RAG settings');
    assert.doesNotMatch(await page.locator('#ask-form').innerText(),
      /До 20|после 6|порог 2|6000|UTF-8 байт|Настройки поиска загружаются/,
      'composer does not display retrieval internals');
    assert.match(await page.locator('#composer-help').innerText(),/фрагмент.*локальн|источник/i);
    await page.locator('#prompt').fill('Как сохраняется отчёт?');
    await page.locator('#preview-button').click();
    await page.locator('#token-preview').waitFor({state:'visible'});
    assert.equal(previews[0].rag_mode,'filter','preview uses fixed filter');
    await page.locator('#workspace-dialog [data-close-panel]').click();
    await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('#waiting')?.hidden === false);
    assert.equal(await page.locator('#send').isDisabled(),true,'busy state locks send');
    assert.equal(sent[0].rag_mode,'filter','main send uses fixed filter');
    releaseFirst();
    await page.locator('.message.assistant .message-text').getByText(/Отчёты используют UTF-8/).waitFor();
    const card=page.locator('.message.assistant .rag-message').first();
    assert.match(await card.locator('.chat-rag-sources summary').first().innerText(),/1/);
    await card.locator('.chat-rag-sources summary').first().click();
    await card.locator('.chat-rag-source summary').first().click();
    const cardText=await card.innerText();
    assert.match(cardText,/corpus\/project_readme\.md.*Reports.*report:12/s,'full source locator');
    assert.match(cardText,/Reports are UTF-8/,'used fragment visible');
    assert.doesNotMatch(cardText,/unused\.md|uncited candidate/,'selected but uncited source hidden');
    assert.equal(await card.locator('img').count(),0,'fragment text does not become HTML');
    assert.equal(await page.evaluate(() => window.ragPwned),undefined);
    fs.mkdirSync(path.join(root,'docs'),{recursive:true});
    await page.screenshot({path:path.join(root,'docs','day24-synthetic-main-chat.png')});
    await page.reload();
    await page.locator('.message.assistant .rag-message').waitFor();
    assert.equal(sent.length,1,'reload sends no new question');
    await page.locator('#prompt').fill('Нет ответа в корпусе');
    await page.locator('#send').click();
    await page.locator('.message.assistant .message-text').getByText(/Не знаю/).waitFor();
    assert.match(await page.locator('.message.assistant .message-text').last().innerText(),/Уточните вопрос/);
    assert.equal(await page.locator('.message.assistant .rag-message').last().locator('.chat-rag-source').count(),0);
    assert.equal(await page.locator('.message.user .rag-message').count(),0,
      'unknown response belongs to assistant, not user request');
    await page.reload();
    await page.locator('.message.assistant .message-text').getByText(/Не знаю/).waitFor();
    await page.locator('#prompt').fill('Ошибка ответа');
    await page.locator('#send').click();
    await page.locator('#notice').getByText(/Ответ не прошёл проверку источников/).waitFor();
    assert.equal(await page.locator('.message.assistant .message-text').count(),2,
      'rejected request does not become an epistemic answer');
    await page.locator('[data-dialogue-id="2"]').click();
    await page.waitForFunction(() => document.querySelector('#conversation-title')?.textContent === 'Диалог 2');
    assert.equal(await page.locator('.rag-message').count(),0,'dialogue switch isolates answer sources');
    await page.locator('[data-panel="profile"]').first().click();
    await page.locator('#profile-select').selectOption('2');
    await page.waitForFunction(() => document.querySelector('#profile-launch-name')?.textContent === 'Второй');
    assert.equal(await page.locator('.rag-message').count(),0,'profile switch isolates messages');
    await page.goto(`http://127.0.0.1:${server.address().port}/rag`);
    assert.equal(await page.locator('#compare, #mode, #rag-result, #rewrite-result').count(),0,
      'legacy RAG page exposes no comparison interface');
    assert.equal(errors.length,0,'browser script errors: ' + errors.join('; '));
    console.log('CHAT_RAG_BROWSER_FIXTURE_OK: fixed main, used fragments, safe text, persisted unknown, refusal/error distinction, dialogue/profile isolation');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
