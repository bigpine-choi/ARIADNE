/** Real-browser acceptance checks against a disposable local service.
 * Requires Playwright and Chromium; no customer files, credentials, or AI calls.
 * Run: node scripts/web_smoke.mjs
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temporary = await mkdtemp(join(tmpdir(), 'ariadne-web-smoke-'));
const database = join(temporary, 'synthetic.sqlite3');
const python = process.env.ARIADNE_PYTHON || 'python';
const checks = [];
let service;
let browser;

async function startService() {
  const child = spawn(python, ['-m', 'ariadne', '--db', database, 'serve', '--demo', '--port', '0', '--ready-json'], {
    cwd: root, env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const lines = createInterface({ input: child.stdout });
  // Drain stderr without exposing operational request data in a failing CI log.
  child.stderr.resume();
  service = { child, url: null };
  const url = await new Promise((accept, reject) => {
    const timer = setTimeout(() => reject(new Error('Local service did not become ready within 15 seconds')), 15_000);
    const finish = (error, value) => {
      clearTimeout(timer);
      lines.off('line', onLine);
      child.off('error', onError);
      child.off('exit', onExit);
      error ? reject(error) : accept(value);
    };
    const onError = () => finish(new Error('Could not start the Python service'));
    const onExit = (code) => finish(new Error(`Python service exited before readiness (${code})`));
    const onLine = (line) => {
      let result;
      try { result = JSON.parse(line); } catch { return; }
      if (typeof result.url === 'string' && /^http:\/\/127\.0\.0\.1:\d+$/.test(result.url)) finish(null, result.url);
    };
    lines.on('line', onLine);
    child.on('error', onError);
    child.on('exit', onExit);
  });
  service.url = url;
  return url;
}

async function stopService() {
  const current = service;
  service = null;
  if (!current || current.child.exitCode !== null || current.child.signalCode !== null) return;
  await new Promise((accept) => {
    const timeout = setTimeout(() => current.child.kill('SIGKILL'), 5_000);
    current.child.once('exit', () => { clearTimeout(timeout); accept(); });
    current.child.kill('SIGTERM');
  });
}

async function api(method, path, payload, expected = 200) {
  const response = await fetch(service.url + path, {
    method, headers: { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const body = await response.json();
  assert.equal(response.status, expected, `${method} ${path}: unexpected status`);
  return body;
}

async function verify(name, action) {
  await action();
  checks.push(name);
  process.stdout.write(`PASS ${name}\n`);
}

async function screenshot(page, name) {
  if (!process.env.ARIADNE_SCREENSHOT_DIR) return;
  await mkdir(process.env.ARIADNE_SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: join(process.env.ARIADNE_SCREENSHOT_DIR, name), fullPage: true });
}

async function run() {
  await startService();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, locale: 'ko-KR' });
  const page = await context.newPage();
  page.setDefaultTimeout(8_000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(service.url);

  // Acceptance flows are bound to stable frontend IDs below, not DOM mocks.
  await runFlows(page, context);
  assert.deepEqual(errors, [], 'Browser raised an uncaught script error');
  await screenshot(page, 'ariadne-web-smoke.png');
}

async function runFlows(page, context) {
  const taskPath = '/api/work-items/synthetic-task-001';
  const taskRow = 'button[data-work-id="synthetic-task-001"]';
  const changedTitle = '[가상] 웹에서 수정한 날인본 검토';
  const newerTitle = '[가상] 다른 창에서 먼저 확정한 업무';
  const staleTitle = '[가상] 보관되어야 할 미저장 입력';
  const malicious = '<img src=x onerror="window.ariadneXss=1">';
  page.on('dialog', dialog => dialog.accept());
  async function textIs(selector, expected) {
    await page.waitForFunction(({ selector, expected }) => document.querySelector(selector)?.textContent === expected, { selector, expected });
  }
  async function saveEditor(target, title, reason, status) {
    await target.locator('#edit-title').fill(title);
    await target.locator('#edit-actor').fill('browser-tester');
    await target.locator('#edit-reason').fill(reason);
    if (status) await target.locator('#edit-status').selectOption(status);
    const response = target.waitForResponse(r => r.url().endsWith(taskPath) && r.request().method() === 'PATCH');
    await target.locator('#save-task').click();
    return response;
  }

  await verify('list, detail, evidence and audit use the live local API', async () => {
    await page.locator(taskRow).waitFor();
    await textIs('#detail-title', '[가상] 펀드 A 날인본 수령·검토');
    await page.locator('#tab-evidence').click();
    await page.locator('#evidence-list .record').waitFor();
    assert.match(await page.locator('#evidence-list').innerText(), /synthetic:\/\/demo\/request-001/);
    assert.match(await page.locator('#evidence-list').innerText(), /메일 발송/);
    await page.locator('#tab-audit').click();
    assert.match(await page.locator('#audit-list').innerText(), /가상 데이터 시연/);
    await page.locator('#tab-overview').click();
  });

  await verify('title, owner and status edits persist through a browser reload', async () => {
    await page.locator('#edit-owner').fill('검증 담당자');
    assert.equal((await saveEditor(page, changedTitle, '웹 수정 저장 검증', 'in_progress')).status(), 200);
    await textIs('#detail-title', changedTitle);
    await page.reload();
    await textIs('#detail-title', changedTitle);
    assert.equal(await page.locator('#edit-owner').inputValue(), '검증 담당자');
    assert.equal(await page.locator('#edit-status').inputValue(), 'in_progress');
    const stored = (await api('GET', taskPath)).item;
    assert(stored.confirmedFields.includes('status'));
    assert.equal(stored.status, 'in_progress');
    const audit = (await api('GET', taskPath + '/audit')).entries;
    assert(audit.some(entry => entry.reason === '웹 수정 저장 검증' && entry.actorId === 'browser-tester'));
  });

  await verify('missing reviewed completion evidence blocks completion and retains input', async () => {
    const before = (await api('GET', taskPath)).item;
    await page.locator('#edit-status').selectOption('completed');
    await page.locator('#edit-actor').fill('browser-tester');
    await page.locator('#edit-reason').fill('완료 차단 검증');
    await page.locator('#save-task').click();
    await page.locator('#edit-error').waitFor({ state: 'visible' });
    assert.match(await page.locator('#edit-error').innerText(), /완료.*근거|완료 증빙/);
    assert.equal(await page.locator('#edit-status').inputValue(), 'completed');
    assert.equal(await page.locator('#edit-reason').inputValue(), '완료 차단 검증');
    const after = (await api('GET', taskPath)).item;
    assert.equal(after.version, before.version);
    assert.equal(after.status, before.status);
    await page.reload();
    await textIs('#detail-title', changedTitle);
  });

  await verify('two windows reject a stale save and preserve the draft on explicit reload', async () => {
    const second = await context.newPage();
    second.on('dialog', dialog => dialog.accept());
    second.setDefaultTimeout(8_000);
    await second.goto(service.url);
    await second.locator('#edit-title').waitFor();
    await second.waitForFunction(title => document.querySelector('#edit-title')?.value === title, changedTitle);
    await page.locator('#edit-title').fill(staleTitle);
    await page.locator('#edit-actor').fill('browser-tester');
    await page.locator('#edit-reason').fill('오래된 창 저장');
    assert.equal((await saveEditor(second, newerTitle, '새 창 우선 저장')).status(), 200);
    const staleResponse = page.waitForResponse(r => r.url().endsWith(taskPath) && r.request().method() === 'PATCH');
    await page.locator('#save-task').click();
    assert.equal((await staleResponse).status(), 409);
    await page.locator('#conflict-reload').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#edit-title').inputValue(), staleTitle);
    assert.equal((await api('GET', taskPath)).item.title, newerTitle);
    await page.locator('#conflict-reload').click();
    await textIs('#detail-title', newerTitle);
    assert((await page.locator('#retained-text').inputValue()).includes(staleTitle));
    await second.close();
  });

  await verify('untrusted titles, evidence and locators render only as text', async () => {
    const proof = { id: 'synthetic-xss-evidence', provider: 'synthetic', sourceId: 'synthetic-xss-source', capturedAt: '2026-10-01T09:00:00+09:00', evidence: malicious, locator: 'javascript:window.ariadneXss=2', sha256: null, threadId: null, finality: 'unknown', role: 'request' };
    await api('POST', '/api/evidence', { evidence: proof, actorId: 'browser-tester' }, 201);
    const base = (await api('GET', taskPath)).item;
    const { version, confirmedFields, dueRisk, ...item } = base;
    Object.assign(item, { id: 'synthetic-xss-task', title: malicious, sources: [proof.id], ownerId: null, status: 'new' });
    await api('POST', '/api/work-items', { item, actorId: 'browser-tester', reason: '가상 HTML 안전성 검증' }, 201);
    await page.locator('#refresh').click();
    await page.locator('button[data-work-id="synthetic-xss-task"]').click();
    await textIs('#detail-title', malicious);
    await page.locator('#tab-evidence').click();
    assert.match(await page.locator('#evidence-list').innerText(), /javascript:window\.ariadneXss=2/);
    assert.equal(await page.locator('#detail-title img, #work-list img, #evidence-list img, #evidence-list a').count(), 0);
    assert.equal(await page.evaluate(() => window.ariadneXss), undefined);
    await page.locator('#tab-overview').click();
  });

  await verify('search, fund and state filters preserve the selected detail', async () => {
    await page.locator('#search').fill('조건에 없는 가상 업무');
    assert.equal(await page.locator('#work-list [data-work-id]').count(), 0);
    assert.equal(await page.locator('#detail-title').textContent(), malicious);
    await page.locator('#filtered-banner').waitFor({ state: 'visible' });
    await page.locator('#search').fill('');
    await page.locator('#fund-filter').selectOption('synthetic-fund-a');
    await page.locator('#status-filter').selectOption('in_progress');
    assert.equal(await page.locator('#work-list [data-work-id]').count(), 1);
    assert.equal(await page.locator(taskRow).count(), 1);
    await page.locator('#fund-filter').selectOption('');
    await page.locator('#status-filter').selectOption('');
  });

  let createdId;
  await verify('manual registration links evidence and keeps an entered deadline unconfirmed', async () => {
    await page.locator('#new-task').click();
    await page.locator('#create-task-title').fill('[가상] 웹 수기 등록 업무');
    await page.locator('#create-owner').fill('수기 검증 담당자');
    await page.locator('#create-actor').fill('browser-tester');
    await page.locator('#create-condition').fill('제출확인증 검토');
    await page.locator('#create-source').fill('테스트 목적으로 만든 요청 문구입니다. 실제 회사 자료가 아닙니다.');
    await page.locator('#create-locator').fill('synthetic://browser/manual-request');
    await page.locator('#create-due').fill('2026-10-05');
    const created = page.waitForResponse(r => r.url().endsWith('/api/work-items') && r.request().method() === 'POST');
    await page.locator('#create-submit').click();
    const response = await created;
    assert.equal(response.status(), 201);
    const item = (await response.json()).item;
    createdId = item.id;
    assert.equal(item.due.confirmed, false);
    assert.equal(item.dueRisk, 'unknown');
    assert.equal(item.sources.length, 1);
    await page.locator('#create-dialog').waitFor({ state: 'hidden' });
    await textIs('#detail-title', item.title);
    assert.match(await page.locator('#detail-due-note').innerText(), /확인 필요/);
    assert.equal((await api('GET', `/api/evidence/${encodeURIComponent(item.sources[0])}`)).evidence.locator, 'synthetic://browser/manual-request');
  });

  await verify('proposal acceptance requires explicit review and records the human decision', async () => {
    await page.locator(taskRow).click();
    await textIs('#detail-title', newerTitle);
    const item = (await api('GET', taskPath)).item;
    const proposal = (await api('POST', taskPath + '/proposals', { patch: { waitingReason: '가상 자료를 확인한 뒤 수정 제안을 승인했습니다.' }, baseVersion: item.version, reason: '가상 수정 제안', evidenceIds: [] }, 201)).proposal;
    assert.equal((await api('GET', taskPath)).item.version, item.version);
    await page.locator('#refresh').click();
    await page.locator('#tab-proposals').click();
    await page.locator('#proposals-list form').waitFor();
    await page.locator('#proposals-list form input').nth(0).fill('browser-reviewer');
    await page.locator('#proposals-list form input').nth(1).fill('가상 근거 검토 후 수락');
    const accepted = page.waitForResponse(r => r.url().endsWith(`/api/proposals/${proposal.id}/resolve`) && r.request().method() === 'POST');
    await page.locator('#proposals-list button[value="accepted"]').click();
    assert.equal((await accepted).status(), 200);
    const proposals = (await api('GET', taskPath + '/proposals')).items;
    assert.equal(proposals.find(p => p.id === proposal.id).resolvedBy, 'browser-reviewer');
    assert.equal((await api('GET', taskPath)).item.version, item.version + 1);
    await page.locator('#tab-overview').click();
  });

  await verify('credential settings report unsupported storage without exposing a key', async () => {
    await page.locator('#settings-open').click();
    await page.locator('#settings-status').waitFor({ state: 'visible' });
    if (process.platform !== 'win32') {
      // Controls are also disabled during loading; wait for the completed UI state.
      await textIs('#settings-status', '이 실행 환경에서는 보안 저장을 지원하지 않습니다.');
      await page.waitForFunction(() => document.querySelector('#settings-key')?.disabled === true && document.querySelector('#settings-save')?.disabled === true);
      assert.match(await page.locator('#settings-status').innerText(), /Windows|지원|사용할 수 없/);
      const syntheticKey = 'synthetic-key-for-browser-test-only';
      const failure = await api('POST', '/api/connections/openai', { apiKey: syntheticKey }, 503);
      assert(!JSON.stringify(failure).includes(syntheticKey));
      const status = await api('GET', '/api/connections/openai');
      assert.deepEqual(Object.keys(status).sort(), ['backend', 'configured', 'message', 'supported']);
      assert.equal(status.supported, false);
      assert.equal(status.configured, false);
      assert.equal(status.backend, 'unavailable');
    }
    assert.equal(await page.locator('#settings-key').getAttribute('type'), 'password');
    assert.equal(await page.locator('#settings-key').inputValue(), '');
    await page.locator('#settings-close').click();
  });

  await verify('server restart preserves saved work, evidence and audit records', async () => {
    const before = (await api('GET', taskPath)).item;
    const auditBefore = (await api('GET', taskPath + '/audit')).entries;
    await stopService();
    await startService();
    await page.goto(service.url);
    await page.locator(taskRow).click();
    await textIs('#detail-title', newerTitle);
    assert.deepEqual((await api('GET', taskPath)).item, before);
    assert.deepEqual((await api('GET', taskPath + '/audit')).entries, auditBefore);
    const manual = (await api('GET', `/api/work-items/${encodeURIComponent(createdId)}`)).item;
    assert.equal(manual.title, '[가상] 웹 수기 등록 업무');
    assert.equal(manual.sources.length, 1);
    await page.locator('#tab-audit').click();
    assert.match(await page.locator('#audit-list').innerText(), /가상 근거 검토 후 수락/);
  });
}

try {
  await run();
  process.stdout.write(`\n${checks.length} browser acceptance checks passed.\n`);
} catch (error) {
  process.stderr.write(`FAIL ${error.message}\n`);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  await stopService();
  await rm(temporary, { recursive: true, force: true });
}
