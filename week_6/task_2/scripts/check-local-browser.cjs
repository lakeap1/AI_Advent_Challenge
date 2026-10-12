// Browser acceptance fixture: HTTP is synthetic; no model, cloud API, or secrets.
'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULES || 'playwright');

const root = path.resolve(__dirname, '..');
const pricing = {model:'qwen3.5:9b',service_mode:'local',input_usd_per_million:'0',output_usd_per_million:'0',source:'local API',checked_on:'2026-10-11',infrastructure_cost:'unknown'};
const state = {messages:[],receipts:[],ledger:{requests:0,known_total_usd:'0',total_cost_usd:'0',complete:true,unknown_usage:false},pricing,blocked:false,block_reason:null};
const profile = {id:1,name:'Локальный профиль',style:'',format:'plain',constraints:'',refs:[]};
const dialogue = {id:1,task_id:1,name:'Обычный чат',mode:'sliding'};
const cloudState = {workspace:{active_dialogue:dialogue,dialogues:[dialogue],tasks:[{id:1,name:'Вопросы по графике'}],layers:{working:[],long_term:[]},user_id:'local'},personalization:{selected_id:1,profile,profiles:[profile]},task_state:{task_id:1,stage:'execution',paused:false,plan:[],step_results:[]},invariants:{task_id:1,revision:1,rules:[]},messages:[],requests:[],retrievals:[],summary:{known_cost_usd:'0',cost_complete:true,unknown_cost_requests:0,api_requests:0},token_accounting:{known_total_tokens:0},memory:{facts:{}},checkpoints:[],branches:[],active_branch:1,rag_settings:{top_k_before:20,top_k_after:6,relevance_threshold:2,context_budget_utf8_bytes:6000}};
const clone = value => structuredClone(value);
let cloudCalls = 0, digestCalls = 0, asks = 0;
const hostile = '<img src=x onerror="window.localPwned=1">';
function html() {
  return fs.readFileSync(path.join(root,'templates','index.html'),'utf8')
    .replace(/\{\{ url_for\('static', filename='([^']+)'\) \}\}/g,'/static/$1')
    .replace("{{ url_for('indexing_page') }}",'/indexing');
}
function receipt(id,status,usage,error=null) {
  return {id,status,error,input_policy:'accepted',output_policy:status==='ok'?'accepted':'rejected',usage_status:usage?'known':'not_requested',usage,cost_usd:usage?'0':null,pricing};
}
async function main() {
  const server=http.createServer(async(req,res)=>{
    const send=(code,data,type='application/json')=>{res.writeHead(code,{'Content-Type':type});res.end(type==='application/json'?JSON.stringify(data):data);};
    const url=new URL(req.url,'http://127.0.0.1');
    if(url.pathname==='/')return send(200,html(),'text/html');
    if(url.pathname.startsWith('/static/')){
      const file=path.resolve(root,'.'+url.pathname);
      if(!file.startsWith(path.join(root,'static')+path.sep))return send(404,{});
      return send(200,fs.readFileSync(file),file.endsWith('.css')?'text/css':'text/javascript');
    }
    if(url.pathname==='/api/local/state')return send(200,clone(state));
    if(url.pathname==='/api/local/runtime')return send(200,{status:'ready',model:'qwen3.5:9b',error:null});
    if(url.pathname==='/api/local/ask'){
      let raw='';for await(const part of req)raw+=part;
      const {prompt}=JSON.parse(raw);asks++;
      if(asks===1){
        state.messages.push({role:'user',content:prompt},{role:'assistant',content:'Свет формирует объём. '+hostile});
        state.receipts.push(receipt(1,'ok',{input_tokens:12,output_tokens:8,total_tokens:20,cached_input_tokens:null,reasoning_tokens:null}));
        state.ledger.requests=1;
        return send(200,{status:'ok',answer:state.messages.at(-1).content,receipt:state.receipts.at(-1),error:null,state:clone(state)});
      }
      state.receipts.push(receipt(2,'rejected',{input_tokens:4,output_tokens:3,total_tokens:7,cached_input_tokens:null,reasoning_tokens:null},'Ответ отклонён политикой.'));
      state.ledger.requests=2;
      return send(400,{status:'rejected',answer:null,receipt:state.receipts.at(-1),error:'Ответ отклонён политикой.',state:clone(state)});
    }
    if(url.pathname==='/api/state'){cloudCalls++;return send(200,clone(cloudState));}
    if(url.pathname==='/api/digest/chat'){digestCalls++;return send(503,{text:'Digest fixture unavailable'});}
    return send(404,{});
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1920,height:1080}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.locator('#local-chat-view:visible').waitFor();
    assert.equal(await page.locator('#regular-workspace:visible,#digest-chat-view:visible').count(),0);
    assert.equal(cloudCalls,0,'new local view must not request cloud state');
    assert.equal(digestCalls,0,'new local view must not poll digest');
    await page.locator('#local-prompt').fill('Как работает свет?');
    await page.locator('#local-send').click();
    await page.locator('#local-feed .local-message.assistant').getByText(/Свет формирует объём/).waitFor();
    assert.match(await page.locator('#local-accounting').innerText(),/20/);
    assert.match(await page.locator('#local-accounting').innerText(),/USD/);
    assert.equal(await page.locator('#local-feed img').count(),0,'answer rendered as text');
    assert.equal(await page.evaluate(()=>window.localPwned),undefined);
    await page.reload();
    await page.locator('#local-feed .local-message.assistant').getByText(/Свет формирует объём/).waitFor();
    assert.equal(asks,1,'reload must not retry');
    assert.equal(cloudCalls,0,'restored local view must not request cloud state');
    await page.locator('#local-prompt').fill('Следующий вопрос');
    await page.locator('#local-send').click();
    await page.locator('#local-notice').getByText(/отклонён/).waitFor();
    assert.equal(await page.locator('#local-feed .local-message.assistant').count(),1,'rejected answer hidden');
    assert.equal(asks,2,'no hidden retry');
    assert.match(await page.locator('#local-accounting').textContent(),/вход: accepted · выход: rejected/);
    assert.match(await page.locator('#local-accounting').textContent(),/всего: 7/,'rejected output is still accounted');
    await page.locator('#digest-chat-open').click();
    await page.locator('#digest-chat-view:visible').waitFor();
    assert.equal(await page.locator('#local-chat-view:visible,#regular-workspace:visible').count(),0);
    await page.locator('#digest-back').click();
    await page.locator('#regular-workspace:visible').waitFor();
    assert.equal(await page.locator('#local-chat-view:visible,#digest-chat-view:visible').count(),0);
    assert.ok(cloudCalls>0,'regular entry requests cloud state');
    await page.waitForFunction(()=>document.querySelector('#conversation-title')?.textContent==='Обычный чат');
    await page.locator('#local-chat-open').click();
    await page.locator('#local-chat-view:visible').waitFor();
    assert.equal(await page.locator('#local-chat-open').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('.skip-link').getAttribute('href'),'#local-prompt');
    const stalePage=await browser.newPage({viewport:{width:960,height:720}});
    let releaseStale;
    const staleWait=new Promise(resolve=>{releaseStale=resolve;});
    let servedStale=false;
    await stalePage.route('**/api/local/state',async route=>{
      if(servedStale)return route.continue();
      servedStale=true;
      const snapshot=clone(state);
      await staleWait;
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(snapshot)});
    });
    await stalePage.goto(`http://127.0.0.1:${server.address().port}/`);
    await stalePage.locator('#local-chat-view:visible').waitFor();
    await stalePage.locator('#local-prompt').fill('Поздняя загрузка');
    await stalePage.locator('#local-send').click();
    await stalePage.locator('#local-notice').getByText(/отклонён/).waitFor();
    assert.match(await stalePage.locator('#local-accounting').textContent(),/Запросы, токены и проверки \(3\)/);
    releaseStale();
    await stalePage.waitForTimeout(100);
    assert.match(await stalePage.locator('#local-accounting').textContent(),/Запросы, токены и проверки \(3\)/,'late history response must not erase ask receipt');
    await stalePage.close();
    const savedRegular=await browser.newPage();
    await savedRegular.addInitScript(()=>localStorage.setItem('graphics-view','regular'));
    const regularCalls=cloudCalls;
    await savedRegular.goto(`http://127.0.0.1:${server.address().port}/`);
    await savedRegular.locator('#regular-workspace:visible').waitFor();
    await savedRegular.waitForFunction(()=>document.querySelector('#conversation-title')?.textContent==='Обычный чат');
    assert.ok(cloudCalls>regularCalls,'saved regular view restores cloud state');
    assert.equal(await savedRegular.locator('#local-chat-view:visible,#digest-chat-view:visible').count(),0);
    await savedRegular.close();
    const savedDigest=await browser.newPage();
    await savedDigest.clock.install();
    await savedDigest.addInitScript(()=>localStorage.setItem('graphics-view','digest'));
    const initialDigestCalls=digestCalls;
    await savedDigest.goto(`http://127.0.0.1:${server.address().port}/`);
    await savedDigest.locator('#digest-chat-view:visible').waitFor();
    await savedDigest.waitForFunction(()=>document.querySelector('#digest-notice')?.textContent?.length>0);
    assert.ok(digestCalls>initialDigestCalls,'saved digest view loads digest');
    await savedDigest.locator('#local-chat-open').click();
    await savedDigest.locator('#local-chat-view:visible').waitFor();
    const parkedDigestCalls=digestCalls;
    await savedDigest.clock.runFor(16000);
    assert.equal(digestCalls,parkedDigestCalls,'hidden digest does not poll');
    await savedDigest.close();
    for(let i=0;i<40;i++)state.messages.push({role:'user',content:'Вопрос о материалах '+i},{role:'assistant',content:'Ответ о материалах '+i});
    const longPage=await browser.newPage({viewport:{width:1920,height:1080}});
    await longPage.goto(`http://127.0.0.1:${server.address().port}/`);
    await longPage.locator('#local-feed .local-message.assistant').last().waitFor();
    const largeLayout=await longPage.evaluate(()=>({scroll:document.querySelector('#local-feed').scrollHeight>document.querySelector('#local-feed').clientHeight,composerBottom:document.querySelector('#local-ask-form').getBoundingClientRect().bottom,viewport:innerHeight}));
    assert.equal(largeLayout.scroll,true,'long feed scrolls within fixed workspace');
    assert.ok(largeLayout.composerBottom<=largeLayout.viewport,'composer stays visible at 1920x1080');
    await longPage.close();
    const mobile=await browser.newPage({viewport:{width:390,height:844}});
    await mobile.goto(`http://127.0.0.1:${server.address().port}/`);
    await mobile.locator('#local-chat-view:visible').waitFor();
    assert.equal(await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'mobile has no horizontal overflow');
    const mobileLayout=await mobile.evaluate(()=>({scroll:document.querySelector('#local-feed').scrollHeight>document.querySelector('#local-feed').clientHeight,composerBottom:document.querySelector('#local-ask-form').getBoundingClientRect().bottom,viewport:innerHeight}));
    assert.equal(mobileLayout.scroll,true,'mobile feed scrolls');
    assert.ok(mobileLayout.composerBottom<=mobileLayout.viewport,'mobile composer stays visible');
    state.receipts.push({id:99,status:'error',error:'Ollama не сообщила usage.',input_policy:'accepted',output_policy:'unknown',usage_status:'unknown',usage:null,cost_usd:null,pricing});
    state.ledger={requests:4,known_total_usd:'0',total_cost_usd:null,complete:false,unknown_usage:true};
    state.blocked=true;state.block_reason='Неизвестен расход токенов предыдущего запроса.';
    const blockedPage=await browser.newPage();
    await blockedPage.goto(`http://127.0.0.1:${server.address().port}/`);
    await blockedPage.locator('#local-accounting .local-blocked').getByText(/Неизвестен расход/).waitFor();
    assert.equal(await blockedPage.locator('#local-prompt').isDisabled(),true,'unknown usage locks composer after reload');
    assert.match(await blockedPage.locator('#local-accounting').textContent(),/стоимость чата: неизвестна/);
    assert.equal(asks,3,'blocked reload does not call model');
    await blockedPage.close();
    assert.equal(errors.length,0,'browser errors: '+errors.join('; '));
    console.log('LOCAL_BROWSER_FIXTURE_OK: default/restored local, safe answer, accounting, refusal, navigation, mobile');
  }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
