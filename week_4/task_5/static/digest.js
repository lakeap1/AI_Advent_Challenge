'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const view = $('digest-chat-view');
  const date = n => n == null ? '—' : new Date(n * 1000).toLocaleString('ru-RU', {timeZone:'Asia/Omsk',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  const names = {computergraphics:'Computer Graphics',graphicdesign:'Graphic Design',blender:'Blender'};
  const statuses = {running:'Выполняется',success:'Готово',partial:'Неполная сводка',empty:'Новых вопросов нет',error:'Ошибка',interrupted:'Прерван'};
  let state=null, busy=false, reading=false, generation=0, profileId=null, renderedKey='', renderedSelection=null;
  const el = (tag, text, cls) => {const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  function notice(text, error=false) {$('digest-notice').textContent=text;$('digest-notice').className=error?'digest-error':'';}
  function controls() {
    view.querySelectorAll('button,select,textarea').forEach(n=>{n.disabled=busy;});
    $('digest-send').disabled=busy||!state?.selected_run_id||!$('digest-prompt').value.trim();
    $('digest-prompt').disabled=busy||!state?.selected_run_id;
  }
  function show(open) {
    view.hidden=!open;$('regular-workspace').hidden=open;$('digest-chat-open').setAttribute('aria-pressed',String(open));
    document.querySelector('.skip-link').setAttribute('href',open?'#digest-prompt':'#prompt');
    document.querySelector('.app-shell').classList.remove('chats-mobile-open');
    document.querySelectorAll('[data-shell-toggle="chats"]').forEach(n=>n.setAttribute('aria-expanded','false'));
    localStorage.setItem('graphics-view',open?'digest':'regular');
    if(open){refresh();$('digest-chat-title').setAttribute('tabindex','-1');$('digest-chat-title').focus();}
  }
  function render(data) {
    state=data;profileId=data.profile_id;
    const s=data.monitor?.schedule;
    $('digest-mode').textContent=s?(s.enabled?'Расписание включено на капсуле':'Расписание приостановлено'):'Связь с сервером пока не подтверждена';
    $('digest-next').textContent=s?.enabled?'Следующий сбор: '+date(s.next_due)+' по Омску'+(s.backoff_until?' · пауза источника до '+date(s.backoff_until):''):'';
    $('digest-runs').replaceChildren(...(data.monitor?.runs||[]).slice(0,10).map(r=>el('li','#'+r.id+' · '+date(r.started_at)+' · '+statuses[r.status]+(r.error?' · '+r.error:''))));
    const selected=data.digests.find(d=>d.run_id===data.selected_run_id);
    $('digest-select').replaceChildren(...[...data.digests].reverse().map(d=>{const n=el('option','#'+d.run_id+' · '+date(d.generated_at));n.value=d.run_id;n.selected=d.run_id===data.selected_run_id;return n;}));
    $('digest-selection-info').textContent=selected?'Окно: '+date(selected.window_start)+' — '+date(selected.window_end)+' (Омск). Новые сводки не меняют выбранный контекст.':'Соберите первую сводку или дождитесь расписания.';
    $('digest-context-label').textContent=selected?'Вопрос по сводке №'+selected.run_id:'Вопрос по выбранной сводке';
    const c=data.conversation, sum=c.summary;
    $('digest-cost').textContent='Расход чата: $'+sum.known_cost_usd+' USD'+(sum.cost_complete?'':' · сумма неполная; неизвестных расходов: '+sum.unknown_cost_requests)+' · вызовов: '+sum.api_requests;
    $('digest-requests').replaceChildren(...[...c.requests].reverse().map(r=>{
      const d=el('details'),u=r.usage;d.append(el('summary','Запрос №'+r.id+' · сводка №'+(r.metadata.digest_run_id||'—')+' · '+r.status));
      d.append(el('p',u?'Вход: '+u.input_tokens+' · выход: '+u.output_tokens+' · всего: '+u.total_tokens:'Токены: '+(r.usage_status==='not_requested'?'API не вызывался':'неизвестно')));
      if(u)d.append(el('p','Кэш входа: '+(u.cached_input_tokens??'неизвестно')+' · reasoning: '+(u.reasoning_tokens??'неизвестно')));
      d.append(el('p','Стоимость: '+(r.cost_usd===null?'неизвестна':'$'+r.cost_usd+' USD')+' · вход: '+r.input_policy.status+' · выход: '+r.output_policy.status));
      d.append(el('p','Модель: '+(r.metadata.actual_model||r.metadata.requested_model)+' · режим: '+(r.metadata.actual_service_tier||r.metadata.requested_service_tier)));
      const p=r.metadata.pricing;if(p)d.append(el('p','Тариф: '+JSON.stringify(p)));
      if(r.metadata.digest_total_questions!==undefined)d.append(el('p','В контексте: '+r.metadata.digest_included_questions+' из '+r.metadata.digest_total_questions+' вопросов.'));
      return d;
    }));
    const key=JSON.stringify([data.profile_id,data.selected_run_id,data.digests.map(d=>d.run_id),c.requests,c.messages]);
    if(key!==renderedKey){
      renderedKey=key;const feed=$('digest-feed'),previous=feed.scrollTop,nearBottom=feed.scrollHeight-feed.clientHeight-previous<100;
      feed.replaceChildren();
      if(!data.digests.length)feed.append(el('div','Пока нет сводок. Нажмите «Собрать сейчас» — сервер соберёт новые вопросы за сутки.','digest-empty'));
      const byRequest=new Map(c.requests.map(r=>[r.id,r]));
      // Keep every report in the conversation; older reports are compact until selected.
      for(const d of data.digests){
        const card=el('article',undefined,'digest-card'+(d.run_id===data.selected_run_id?' selected':''));
        card.append(el('p','СВОДКА №'+d.run_id+' · '+date(d.generated_at)+' ОМСК','eyebrow'));
        card.append(el('h2',d.question_count+' вопросов · '+d.unanswered_count+' без ответов'));
        card.append(el('p',Object.entries(d.source_counts||{}).map(([k,v])=>(names[k]||k)+': '+v).join(' · '),'digest-caption'));
        if(d.partial)card.append(el('p','Неполная выборка: проверьте состояние источников ниже.','digest-warning'));
        if(d.run_id===data.selected_run_id){
          card.append(el('p',d.text,'digest-report-text'));
          const list=el('details');list.append(el('summary','Материалы и ссылки ('+d.questions.length+')'));
          const ul=el('ul');for(const q of d.questions){const li=el('li'),a=el('a',q.title);a.href=q.url;a.target='_blank';a.rel='noopener noreferrer';li.append(a,el('small',(names[q.source]||q.source)+' · ответов: '+q.answer_count));if(q.excerpt)li.append(el('p',q.excerpt));ul.append(li);}list.append(ul);card.append(list);
          const sourceDetails=el('p',Object.entries(d.source_status||{}).map(([k,v])=>(names[k]||k)+': '+v).join(' · '),'digest-caption');card.append(sourceDetails);
          for(const [k,v] of Object.entries(d.source_errors||{}))card.append(el('p',(names[k]||k)+': '+v,'digest-warning'));
        }else{const button=el('button','Обсудить эту сводку');button.type='button';button.addEventListener('click',()=>select(d.run_id));card.append(button);}
        feed.append(card);
        for(const m of c.messages.filter(m=>byRequest.get(m.request_id)?.metadata.digest_run_id===d.run_id)){
          const message=el('article',undefined,'digest-message '+m.role);message.append(el('small',m.role==='user'?'Вы · сводка №'+d.run_id:'Помощник · сводка №'+d.run_id),el('p',m.content));feed.append(message);
        }
        for(const r of c.requests.filter(r=>r.metadata.digest_run_id===d.run_id&&r.status!=='ok'))feed.append(el('p',r.text||'Запрос не завершён. Автоматического повтора нет.','digest-error'));
      }
      if(renderedSelection!==data.selected_run_id){
        const selectedCard=feed.querySelector('.digest-card.selected');
        if(selectedCard)feed.scrollTop+=selectedCard.getBoundingClientRect().top-feed.getBoundingClientRect().top;
      }else feed.scrollTop=nearBottom?feed.scrollHeight:previous;
      renderedSelection=data.selected_run_id;
    }
    controls();
  }
  async function request(path,body){
    const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const value=await response.json();
    if(!response.ok){const e=new Error(value.text||'Операция не выполнена.');e.data=value.data;throw e;}
    return value;
  }
  async function refresh(){
    if(view.hidden||document.hidden||busy||reading)return;
    const epoch=generation;reading=true;
    try{const v=await request('/api/digest/chat');if(epoch===generation){render(v.data);notice(v.data.warning||'Сводки сохранены. История восстановится после перезапуска.',!!v.data.warning);}}
    catch(e){if(epoch===generation)notice(e.message,true);}
    finally{reading=false;}
  }
  async function action(path,body,success){
    if(busy)return;busy=true;generation++;const epoch=generation;controls();notice(path.endsWith('/ask')?'Готовим ответ по выбранной сводке…':'Выполняем…');
    try{const v=await request(path,body);if(epoch!==generation)return;if(v.data?.conversation)render(v.data);else {const next=await request('/api/digest/chat');if(epoch===generation)render(next.data);}notice(v.data?.warning||success+(v.executed===false?' Новый сбор не выполнен: действует пауза или другой сбор.':''),!!v.data?.warning);}
    catch(e){if(epoch===generation){if(e.data)render(e.data);notice(e.message,true);}}
    finally{busy=false;controls();}
  }
  function select(run){action('/api/digest/chat/select',{profile_id:profileId,run_id:run},'Контекст: сводка №'+run);}
  $('digest-chat-open').addEventListener('click',()=>show(true));$('digest-back').addEventListener('click',()=>show(false));
  $('digest-refresh').addEventListener('click',refresh);
  $('digest-select').addEventListener('change',()=>select(Number($('digest-select').value)));
  $('digest-collect').addEventListener('click',()=>action('/api/digest/collect',{},'Сбор через MCP завершён.'));
  $('digest-enable').addEventListener('click',()=>action('/api/digest/configure',{},'Расписание включено на капсуле.'));
  $('digest-pause').addEventListener('click',()=>action('/api/digest/pause',{},'Расписание приостановлено. Сохранённые сводки доступны.'));
  $('digest-settings-toggle').addEventListener('click',()=>{const panel=$('digest-settings');panel.hidden=!panel.hidden;$('digest-settings-toggle').setAttribute('aria-expanded',String(!panel.hidden));});
  $('digest-prompt').addEventListener('input',controls);
  $('digest-prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!$('digest-send').disabled)$('digest-ask-form').requestSubmit();}});
  $('digest-ask-form').addEventListener('submit',async e=>{e.preventDefault();if(busy||!state?.selected_run_id)return;const prompt=$('digest-prompt').value;await action('/api/digest/chat/ask',{profile_id:profileId,run_id:state.selected_run_id,prompt},'Ответ сохранён.');if(state?.conversation.messages.some(m=>m.role==='user'&&m.content===prompt))$('digest-prompt').value='';controls();});
  document.addEventListener('click',e=>{if(e.target.closest('.dialogue-item, [data-panel="create"]'))show(false);});
  document.addEventListener('workspace-state',e=>{const pid=e.detail.personalization?.selected_id;if(pid&&profileId&&pid!==profileId){generation++;profileId=pid;state=null;renderedKey='';$('digest-feed').replaceChildren(el('p','Загружаем чат выбранного профиля…'));$('digest-prompt').value='';controls();if(!reading)refresh();}});
  setInterval(refresh,15000);
  if(localStorage.getItem('graphics-view')==='digest')show(true);
  controls();
})();
