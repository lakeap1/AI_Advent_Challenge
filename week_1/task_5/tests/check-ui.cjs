const assert = require('node:assert/strict');
const path = require('node:path');
const {mkdir, writeFile} = require('node:fs/promises');
const {chromium} = require(require.resolve('playwright',{paths:[process.env.PLAYWRIGHT_NODE_MODULES]}));
(async()=>{
 const out=path.resolve('docs/ui-check'); await mkdir(out,{recursive:true});
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1920,height:1080}});
  const errors=[]; page.on('pageerror',error=>errors.push(error.message));
  await page.goto('http://127.0.0.1:5005');
  console.log((await page.locator('body').innerText()).slice(0,1800));
  assert.equal(await page.locator('.result').count(),3);
  assert.equal(await page.title(),'Версии моделей · День 5');
  assert.equal(typeof page.screencast?.start,'function');
  await page.screenshot({path:path.join(out,'initial.png'),fullPage:true});
  let requests=0;
  const ids=await page.locator('.result').evaluateAll(nodes=>nodes.map(n=>n.dataset.model));
  await page.route('**/api/compare',async route=>{
   requests++;
   const body=route.request().postDataJSON();
   assert.equal(body.repeats,3); assert.ok(body.prompt.includes('Бюджет'));
   const results=[];
   for(let attempt=1;attempt<=3;attempt++) for(const id of ids) results.push({
    requested_model:id,attempt,status:'completed',answer:attempt===1?'Первый ответ':('Тест интерфейса. <img src=x onerror=alert(1)>\n\n'+('Длинный ответ для проверки прокрутки. '.repeat(30))),
    seconds:2.5,usage:{input_tokens:100,output_tokens:200,total_tokens:300,input_tokens_details:{cached_tokens:0},output_tokens_details:{reasoning_tokens:80}},cost_usd:.0014
   });
   const messages=[{type:'start',total:9},...results.flatMap(result=>[{type:'progress',model:result.requested_model,attempt:result.attempt},{type:'result',result}]),
    {type:'done',run:{summary:ids.map(model=>({model,completed:3,attempts:3,median_seconds:2.5,median_tokens:300,total_cost_usd:.0042}))}}];
   await route.fulfill({contentType:'application/x-ndjson',body:messages.map(m=>JSON.stringify(m)).join('\n')+'\n'});
  });
  await page.getByRole('button',{name:'Сравнить модели'}).click();
  await page.waitForFunction(()=>document.querySelector('#summary').hidden===false);
  assert.equal(await page.locator('.attempts button').count(),9);
  assert.equal(await page.locator('.answer img').count(),0);
  assert.match(await page.locator('.answer').first().innerText(),/<img/);
  await page.locator('.attempts').first().getByRole('button',{name:'Прогон 1'}).click();
  assert.equal(await page.locator('.answer').first().innerText(),'Первый ответ');
  assert.equal(await page.locator('#submit').isEnabled(),true);
  await page.screenshot({path:path.join(out,'results.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(out,'mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.unroute('**/api/compare');
  await page.route('**/api/compare',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Тест недоступного API'})}));
  await page.getByRole('button',{name:'Сравнить модели'}).click();
  await page.waitForFunction(()=>document.querySelector('#error').hidden===false);
  assert.equal(await page.locator('#error').innerText(),'Тест недоступного API');
  assert.equal(await page.locator('#submit').isEnabled(),true);
  await page.unroute('**/api/compare');
  await page.route('**/api/compare',route=>route.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'start',total:9})+'\n'}));
  await page.getByRole('button',{name:'Сравнить модели'}).click();
  await page.waitForFunction(()=>document.querySelector('#error').textContent.includes('прервалось'));
  assert.equal(await page.locator('#submit').isEnabled(),true);
  assert.deepEqual(errors,[]);
  await writeFile(path.join(out,'verification.json'),JSON.stringify({passed:true,paid_calls:0,checks:['desktop','mobile 390px','9 results','attempt switch','XSS text','503 recovery','interrupted stream'],requests},null,2));
  console.log('UI checks passed; no paid API requests. Screenshots: '+out);
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
