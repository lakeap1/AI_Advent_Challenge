// Deterministic event/transport regression, no browser or network required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor(tag){this.tag=tag;this.hidden=false;this.disabled=false;this.value='';this.textContent='';this.events={};this.children=[];this.classList={remove(){}};this.scrollTop=0;this.scrollHeight=0;this.clientHeight=0;}
  addEventListener(name, fn){this.events[name]=fn;}
  setAttribute(){} focus(){} querySelectorAll(){return [];} querySelector(){return null;}
  replaceChildren(...nodes){this.children=nodes;}
  append(...nodes){this.children.push(...nodes);}
}
function state(enabled){return {profile_id:1,selected_run_id:null,digests:[],monitor:{schedule:{enabled,next_due:null},runs:[]},conversation:{messages:[],requests:[],summary:{known_cost_usd:'0',cost_complete:true,api_requests:0}},warning:''};}
function stateWithDigest(){
  const result=state(true);
  result.selected_run_id=7;
  result.monitor.analysis_summary={known_cost_usd:'0.25',cost_complete:true,api_requests:1};
  result.digests=[{run_id:7,digest_date:'2026-09-29',window_start:0,window_end:86400,question_count:1,unanswered_count:1,source_counts:{blender:1},partial:false,text:'Детерминированная сводка',questions:[{title:'Как настроить свет?',url:'https://blender.stackexchange.com/questions/7',source:'blender',answer_count:0,excerpt:'Вопрос об освещении'}],source_status:{blender:'ok'},source_errors:{},analysis:{status:'error',metadata:{http_status:403},text:'Старый AI-разбор'}}];
  result.conversation.summary={known_cost_usd:'0.10',cost_complete:true,api_requests:1};
  return result;
}
function descendants(node){return [node,...node.children.flatMap(descendants)];}
async function main(){
  const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const document={hidden:false,getElementById:get,querySelector:get,querySelectorAll:()=>[],createElement:tag=>new Element(tag),addEventListener(){}};
  let releaseRead, releaseCollect, collectCalls=0;
  const read=new Promise(resolve=>{releaseRead=()=>resolve({ok:true,json:async()=>({data:state(false)})});});
  const collect=new Promise(resolve=>{releaseCollect=()=>resolve({ok:true,json:async()=>({data:stateWithDigest(),executed:true})});});
  const context={document,localStorage:{getItem:()=>null,setItem(){}},setInterval(){},fetch:async(url,options)=>{
    if(url==='/api/digest/chat')return read;
    if(url==='/api/digest/collect'){collectCalls++;assert.deepEqual(JSON.parse(options.body),{});return collect;}
    throw new Error(url);
  }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/digest.js'),'utf8'),context);
  get('digest-chat-open').events.click();
  const action=get('digest-collect').events.click();
  assert.equal(collectCalls,1,'collection must be delivered while refresh is in flight');
  assert.equal(get('digest-send').disabled,true);
  releaseCollect();await action;
  assert.equal(get('digest-mode').textContent,'Расписание включено на капсуле');
  const report=descendants(get('digest-feed'));
  const text=report.map(node=>node.textContent).join(' ');
  assert.match(text,/Детерминированная сводка/);
  assert.match(text,/Вопросы и источники \(1\)/);
  assert.equal(report.find(node=>node.tag==='a')?.href,'https://blender.stackexchange.com/questions/7');
  assert.doesNotMatch(text,/AI-разбор|Старый AI-разбор|Ожидаем автоматический|403/);
  assert.match(get('digest-cost').textContent,/\$0\.10 USD/,'personal question costs remain visible');
  releaseRead();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(get('digest-mode').textContent,'Расписание включено на капсуле','stale refresh must not overwrite newer mutation');
  assert.equal(collectCalls,1);
  console.log('PASS: collect during refresh delivered once; stale refresh ignored; summary and source visible; historical analysis hidden; personal costs visible');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
