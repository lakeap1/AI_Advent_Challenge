const assert = require('node:assert/strict');
const path = require('node:path');
const { mkdir } = require('node:fs/promises');
const { chromium } = require(require.resolve('playwright', { paths: [process.env.PLAYWRIGHT_NODE_MODULES] }));

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1920,height:1080}});
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:5004');
    assert.equal(await page.locator('.result').count(), 3);
    await page.getByRole('button', {name:'Подставить пример'}).click();
    assert.match(await page.locator('#prompt').inputValue(), /андроида/);
    await page.route('**/api/compare', async route => {
      const request = route.request().postDataJSON();
      assert.match(request.prompt, /андроида/);
      await new Promise(resolve => setTimeout(resolve, 200));
      await route.fulfill({json:{id:'ui-test',request:{input:request.prompt},results:[0,0.7,1.2].map(temperature => ({temperature,status:'completed',seconds:1,usage:{output_tokens:30},answer:'ТЕСТОВЫЙ ответ <script>window.injected = true</script>\n' + 'Текст для проверки переноса строк. '.repeat(12)}))}});
    });
    await page.getByRole('button', {name:'Сравнить ответы'}).click();
    await page.waitForFunction(() => document.querySelector('#status').textContent.startsWith('3 из 3'));
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.equal(await page.locator('.completed').count(), 3);
    assert.equal(await page.locator('textarea').count(), 1);
    assert.equal(await page.locator('#download, .notes, .guidance').count(), 0);
    await page.unroute('**/api/compare');
    await page.route('**/api/compare', route => route.fulfill({status:503,json:{error:'Тест ошибки API'}}));
    await page.getByRole('button', {name:'Сравнить ответы'}).click();
    await page.waitForFunction(() => document.querySelector('#status').textContent === 'Тест ошибки API');
    assert.equal(await page.locator('#submit').isEnabled(), true);
    await page.reload();
    await mkdir('.recording-tmp/ui', {recursive:true});
    await page.screenshot({path:'.recording-tmp/ui/desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.locator('.results').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length), 1);
    await page.screenshot({path:'.recording-tmp/ui/mobile.png',fullPage:true});
    assert.deepEqual(errors, []);
    console.log('UI PASS: 3 columns, example, safe text, no export or extra fields, recoverable error, mobile width. Mocked API; no paid calls.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
