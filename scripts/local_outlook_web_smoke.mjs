/** Actual Chromium, production HTTP/DB/job flow, synthetic local Outlook. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
const {chromium} = createRequire(import.meta.url)('playwright');
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temp = await mkdtemp(join(tmpdir(), 'ariadne-local-ui-'));
let child, browser, url;
async function start() {
  child = spawn(process.env.ARIADNE_PYTHON || 'python', ['-m','scripts.local_outlook_test_server','--db',join(temp,'mail.sqlite3')],
    {cwd:root,env:{...process.env,PYTHONIOENCODING:'utf-8',PYTHONUNBUFFERED:'1'},stdio:['ignore','pipe','pipe']});
  child.stderr.resume();
  const lines=createInterface({input:child.stdout});
  url=await new Promise((accept,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture startup timed out')),15000);
    child.once('error',reject); child.once('exit',code=>{clearTimeout(timer);reject(new Error(`Fixture exited ${code}`));});
    lines.on('line',line=>{try {const data=JSON.parse(line); if(data.url){clearTimeout(timer);accept(data.url);}} catch {}});
  });
}
async function stop() {
  if(!child||child.exitCode!==null)return;
  await new Promise(resolve=>{const timer=setTimeout(()=>child.kill('SIGKILL'),5000); child.once('exit',()=>{clearTimeout(timer);resolve();});child.kill('SIGTERM');});
}
async function collect(page) {
  await page.locator('#local-outlook-collect').click();
  await page.waitForFunction(()=>document.getElementById('local-outlook-progress').textContent.includes('수집을 마쳤습니다'));
}
try {
  await start(); browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1360,height:960}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url);
  await page.locator('#local-outlook-open').click();
  await page.locator('#local-outlook-discover').click();
  await page.waitForFunction(()=>document.querySelectorAll('#local-outlook-account option').length===3);
  await page.locator('#local-outlook-account').selectOption({index:1});
  await page.locator('#local-outlook-folder').selectOption({index:1});
  await page.locator('#local-outlook-start').fill('2026-09-30');
  await page.locator('#local-outlook-end').fill('2026-10-01');
  await page.locator('#local-outlook-actor').fill('가상 검토자');
  await collect(page);
  assert.match(await page.locator('#local-outlook-result').textContent(),/신규 1건.*실패 1건/);
  assert.match(await page.locator('#local-outlook-warning').textContent(),/전체 수집 완료가 아닙니다/);
  console.log('PASS local folder selection, manual collection, partial failure and upper-bound notice');
  await collect(page);
  assert.match(await page.locator('#local-outlook-result').textContent(),/신규 0건.*기존과 동일 1건/);
  console.log('PASS repeated manual capture is idempotent');
  await page.locator('#local-outlook-close').click();
  await page.reload();
  await page.locator('#local-outlook-open').click();
  await page.waitForFunction(()=>document.getElementById('local-outlook-result').textContent.includes('기존과 동일 1건'));
  console.log('PASS latest collection result survives browser refresh');
  await page.locator('#local-outlook-archive').click();
  await page.locator('#mail-list button').first().click();
  await page.waitForFunction(()=>document.getElementById('mail-detail-meta').textContent.includes('이 PC의 Outlook'));
  assert.match(await page.locator('#mail-detail-subject').textContent(),/<img src=x/);
  assert.equal(await page.evaluate(()=>window.localXss),undefined);
  assert.match(await page.locator('#mail-detail-meta').textContent(),/펀드 A/);
  console.log('PASS collected mail opens in existing archive with local provenance and safe text');
  await stop();await start();await page.goto(url);
  await page.locator('#mail-open').click();await page.locator('#mail-list button').first().click();
  await page.waitForFunction(()=>document.getElementById('mail-detail-meta').textContent.includes('이 PC의 Outlook'));
  assert.equal(await page.locator('#mail-list button').count(),1);
  assert.deepEqual(errors,[]);
  console.log('PASS archived local mail survives server restart; no browser errors');
} finally {
  if(browser)await browser.close();await stop();await rm(temp,{recursive:true,force:true});
}
