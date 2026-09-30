/** Real HTTP + Chromium checks with a synthetic Outlook service.
 * Authentication, encryption and Graph behavior have separate Python tests.
 * This suite never signs in to Microsoft, opens a mailbox or uses credentials.
 * Run: node scripts/outlook_web_smoke.mjs
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temporary = await mkdtemp(join(tmpdir(), 'ariadne-outlook-smoke-'));
const successClient = '11111111-1111-4111-8111-111111111111';
const reauthClient = '22222222-2222-4222-8222-222222222222';
const tenantId = '33333333-3333-4333-8333-333333333333';
const connectionPath = '/api/connections/outlook';
const subject = '<img src=x onerror="window.outlookXss=1">';
const bodyText = '<script>window.outlookXss=2</script>\n가상 메일 본문입니다.';
const checks = [];
let child;
let browser;
let baseUrl;

async function startService() {
  child = spawn(process.env.ARIADNE_PYTHON || 'python', [
    '-m', 'scripts.outlook_test_server', '--db', join(temporary, 'synthetic.sqlite3'),
  ], {
    cwd: root,
    env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const lines = createInterface({ input: child.stdout });
  child.stderr.resume();
  return new Promise((accept, reject) => {
    const timer = setTimeout(() => finish(new Error('Synthetic HTTP fixture did not become ready')), 15_000);
    const finish = (error, value) => {
      clearTimeout(timer);
      lines.off('line', onLine);
      child.off('error', onError);
      child.off('exit', onExit);
      error ? reject(error) : accept(value);
    };
    const onError = () => finish(new Error('Could not start the synthetic Python fixture'));
    const onExit = code => finish(new Error(`Synthetic fixture exited before readiness (${code})`));
    const onLine = line => {
      let result;
      try { result = JSON.parse(line); } catch { return; }
      if (typeof result.url === 'string' && /^http:\/\/127\.0\.0\.1:\d+$/.test(result.url)) {
        finish(null, result.url);
      }
    };
    lines.on('line', onLine);
    child.once('error', onError);
    child.once('exit', onExit);
  });
}

async function stopService() {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise(accept => {
    const timer = setTimeout(() => child.kill('SIGKILL'), 5_000);
    child.once('exit', () => { clearTimeout(timer); accept(); });
    child.kill('SIGTERM');
  });
}

async function status() {
  const response = await fetch(baseUrl + connectionPath);
  assert.equal(response.status, 200);
  return response.json();
}

async function verify(name, action) {
  await action();
  checks.push(name);
  process.stdout.write(`PASS ${name}\n`);
}

async function within(promise, message) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), 10_000); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function responseFor(page, suffix, method) {
  return page.waitForResponse(response =>
    response.url() === baseUrl + suffix && response.request().method() === method);
}

async function openOutlook(page) {
  await page.locator('#settings-open').click();
  await page.locator('#outlook-open').click();
  await page.locator('#outlook-dialog').waitFor({ state: 'visible' });
  await page.waitForFunction(() => document.querySelector('#outlook-client-id')?.disabled === false);
}

async function configure(page, clientId) {
  await page.locator('#outlook-client-id').fill(clientId);
  await page.locator('#outlook-tenant-id').fill(tenantId);
  const saved = responseFor(page, connectionPath, 'POST');
  await page.locator('#outlook-save').click();
  const response = await saved;
  assert.equal(response.status(), 200);
  assert.equal((await response.json()).state, 'disconnected');
  await page.waitForFunction(() => document.querySelector('#outlook-login')?.disabled === false);
}

async function login(page) {
  const started = responseFor(page, connectionPath + '/login', 'POST');
  await page.locator('#outlook-login').click();
  const response = await started;
  assert.equal(response.status(), 202);
  assert.equal((await response.json()).state, 'connecting');
  await page.waitForFunction(() => document.querySelector('#outlook-fetch')?.disabled === false);
  assert.equal((await status()).state, 'connected');
  assert.match(await page.locator('#outlook-status').innerText(), /로그인 정보 저장됨/);
}

async function readMail(page, expectedStatus) {
  await page.locator('#outlook-folder').selectOption('sentitems');
  await page.locator('#outlook-start').fill('2026-09-01');
  await page.locator('#outlook-end').fill('2026-10-01');
  await page.locator('#outlook-limit').fill('1');
  const loaded = responseFor(page, connectionPath + '/messages', 'POST');
  await page.locator('#outlook-fetch').click();
  const response = await loaded;
  assert.equal(response.status(), expectedStatus);
  assert.deepEqual(response.request().postDataJSON(), {
    folder: 'sentitems', startDate: '2026-09-01', endDate: '2026-10-01', limit: 1,
  });
}

async function run() {
  baseUrl = await startService();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, locale: 'ko-KR' });
  const externalRequests = [];
  await context.route('**/*', async route => {
    if (new URL(route.request().url()).origin === baseUrl) return route.continue();
    externalRequests.push(route.request().url());
    return route.abort();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10_000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await page.goto(baseUrl);
  await page.locator('#detail-title').waitFor();

  await verify('Outlook configuration survives closing and reopening the dialog', async () => {
    await openOutlook(page);
    assert.equal((await status()).state, 'not_configured');
    await configure(page, successClient);
    await page.locator('#outlook-close').click();
    await page.locator('#settings-close').click();
    await openOutlook(page);
    await page.waitForFunction(id => document.querySelector('#outlook-client-id')?.value === id, successClient);
    assert.equal(await page.locator('#outlook-tenant-id').inputValue(), tenantId);
    assert.deepEqual((await status()).config, { clientId: successClient, tenantId });
  });

  await verify('an in-progress login can be cancelled through the actual DELETE route', async () => {
    const started = responseFor(page, connectionPath + '/login', 'POST');
    await page.locator('#outlook-login').click();
    assert.equal((await started).status(), 202);
    await page.waitForFunction(() => document.querySelector('#outlook-disconnect')?.disabled === false);
    assert.match(await page.locator('#outlook-disconnect').innerText(), /취소/);
    const cancelled = responseFor(page, connectionPath, 'DELETE');
    await page.locator('#outlook-disconnect').click();
    assert.equal((await cancelled).status(), 200);
    await page.waitForFunction(() => document.querySelector('#outlook-login')?.disabled === false);
    assert.equal((await status()).state, 'disconnected');
    assert.equal((await status()).account, null);
  });

  await verify('login polls the real HTTP status endpoint until synthetic connection completes', async () => {
    await login(page);
    assert.equal((await status()).account.username, 'synthetic@example.invalid');
  });

  await verify('bounded mail preview renders hostile subject and body as plain text', async () => {
    await readMail(page, 200);
    const preview = page.locator('#outlook-results details').first();
    await preview.waitFor();
    assert((await preview.locator('summary').innerText()).includes(subject));
    if (!(await preview.evaluate(element => element.open))) await preview.locator('summary').click();
    assert((await preview.innerText()).includes(bodyText));
    assert.equal(await page.locator('#outlook-results img, #outlook-results script, #outlook-results a').count(), 0);
    assert.equal(await page.evaluate(() => window.outlookXss), undefined);
    assert.match(await page.locator('#outlook-results-summary').innerText(), /1건.*조회 한도/);
    assert.match(await page.locator('#outlook-results').innerText(), /일부|생략|제한|추가|한도/);
  });

  await verify('disconnect clears previews and ignores an already in-flight mail response', async () => {
    let releaseResponse;
    let reportHeld;
    const release = new Promise(accept => { releaseResponse = accept; });
    const held = new Promise(accept => { reportHeld = accept; });
    await page.route(baseUrl + connectionPath + '/messages', async route => {
      const response = await route.fetch();
      reportHeld();
      await release;
      await route.fulfill({ response });
    });
    const lateResponse = responseFor(page, connectionPath + '/messages', 'POST');
    await page.locator('#outlook-fetch').click();
    await within(held, 'Mail response was not held before disconnect');
    const disconnected = responseFor(page, connectionPath, 'DELETE');
    await page.locator('#outlook-disconnect').click();
    const response = await disconnected;
    assert.equal(response.status(), 200);
    const result = await response.json();
    assert.equal(result.state, 'disconnected');
    assert.equal(result.account, null);
    assert.deepEqual(result.config, { clientId: successClient, tenantId });
    releaseResponse();
    await (await lateResponse).finished();
    await page.unroute(baseUrl + connectionPath + '/messages');
    await page.evaluate(() => new Promise(accept => requestAnimationFrame(() => requestAnimationFrame(accept))));
    await page.waitForFunction(() => document.querySelector('#outlook-fetch')?.disabled === true);
    assert.equal(await page.locator('#outlook-results details').count(), 0);
  });

  await verify('revoked authentication prompts re-login without showing stale mail', async () => {
    await configure(page, reauthClient);
    await login(page);
    await readMail(page, 401);
    await page.locator('#outlook-error').waitFor({ state: 'visible' });
    assert.match(await page.locator('#outlook-error').innerText(), /다시 로그인|다시 진행/);
    await page.waitForFunction(() => document.querySelector('#outlook-fetch')?.disabled === true);
    assert.equal((await status()).state, 'reauth_required');
    assert.equal(await page.locator('#outlook-results details').count(), 0);
    assert.equal(await page.locator('#outlook-login').isEnabled(), true);
  });

  await verify('failed status refresh preserves unsaved settings and permits retry', async () => {
    await page.locator('#outlook-client-id').fill(successClient);
    await page.route(baseUrl + connectionPath, async route => {
      if (route.request().method() === 'GET') return route.abort('failed');
      return route.fallback();
    });
    await page.locator('#outlook-refresh').click();
    await page.locator('#outlook-error').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#outlook-client-id').inputValue(), successClient);
    await page.unroute(baseUrl + connectionPath);
    const refreshed = responseFor(page, connectionPath, 'GET');
    await page.locator('#outlook-refresh').click();
    assert.equal((await refreshed).status(), 200);
    await page.waitForFunction(() => document.querySelector('#outlook-refresh')?.disabled === false);
    assert.equal(await page.locator('#outlook-client-id').inputValue(), successClient);
  });

  assert.deepEqual(errors, [], 'Browser raised an uncaught script error');
  assert.deepEqual(externalRequests, [], 'Synthetic browser checks must not call an external provider');
}

try {
  await run();
  process.stdout.write(`\n${checks.length} synthetic Outlook browser checks passed.\n`);
} catch (error) {
  process.stderr.write(`FAIL ${error.message}\n`);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  await stopService();
  await rm(temporary, { recursive: true, force: true });
}
