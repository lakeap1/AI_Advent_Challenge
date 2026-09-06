const modes = ['direct','step','meta','experts'];
const titles = {direct:'Прямой ответ',step:'Решай пошагово',meta:'Промпт от модели',experts:'Группа экспертов'};
const descriptions = {direct:'Исходное условие без подсказок о способе решения.',step:'К исходному условию добавлено: «Решай пошагово».',meta:'Модель составляет промпт, затем решает задачу с его помощью.',experts:'Математик, планировщик и скептик: разные подходы в системном промпте.'};
let results = {}, selected = 'direct', busy = false;
const el = id => document.getElementById(id);
function node(tag, text, className) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(className)n.className=className; return n; }
async function api(url, payload) { const response = await fetch(url, payload===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const body=await response.json(); if(!response.ok)throw Error(body.error || `Ошибка ${response.status}`); return body; }
function render() {
  document.querySelectorAll('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mode===selected)));
  el('mode-title').textContent=titles[selected]; el('mode-description').textContent=descriptions[selected];
  const r=results[selected]; el('empty').hidden=!!r; el('result').hidden=!r; el('metrics').textContent=r ? `${r.seconds.toFixed(1)} с · ${r.tokens ?? '—'} токенов` : '';
  el('calls').replaceChildren();
  if(r) {
    el('answer').textContent=r.answer; el('answer').scrollTop=0;
    r.calls.forEach((c,i)=>{ el('calls').append(node('h3',`Вызов ${i+1}`),node('pre',JSON.stringify(c.request,null,2))); if(i<r.calls.length-1)el('calls').append(node('h3','Сгенерированный промпт'),node('pre',c.answer)); });
  }
}
document.querySelectorAll('[data-mode]').forEach(b=>b.addEventListener('click',()=>{selected=b.dataset.mode;el('prompts').open=false;render();}));
el('run-all').addEventListener('click',async()=>{
  if(busy)return;busy=true;el('run-all').disabled=true;el('load').disabled=true;
  // Keep previous saved files, but never mix their checks into this new experiment.
  results={};render();let done=0;
  try {for(const mode of modes){selected=mode;render();el('status').textContent=`${done+1}/4 · ${titles[mode]}: ожидаем API…`;results[mode]=await api(`/api/run/${mode}`,{});done++;render();}el('status').textContent='4/4 · Ответы получены и сохранены.';}
  catch(error){el('status').textContent=`Готово ${done}/4. ${error.message}`;}
  finally{busy=false;el('run-all').disabled=false;el('load').disabled=false;}
});
el('load').addEventListener('click',async()=>{try{results=await api('/api/latest');render();el('status').textContent=Object.keys(results).length?'Открыты последние сохранённые ответы по каждому способу.':'Сохранённых ответов пока нет.';}catch(e){el('status').textContent=e.message;}});
render();
