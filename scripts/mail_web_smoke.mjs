/** Chromium acceptance of the real mail archive, preview grants and work links.
 * Only Microsoft identity/provider responses are synthetic. The HTTP routes,
 * OutlookService, SQLite migrations, archive, revisions and UI are production.
 * Run: node scripts/mail_web_smoke.mjs
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
const temporary = await mkdtemp(join(tmpdir(), 'ariadne-mail-smoke-'));
const database = join(temporary, 'synthetic.sqlite3');
const taskId = 'synthetic-task-001';
const taskPath = `/api/work-items/${taskId}`;
const outlookPath = '/api/connections/outlook';
const subject = '<img src=x onerror="window.mailXss=1"> 수익자 요청';
const attachment = '<img src=x onerror="window.mailXss=3">.xlsx';
const checks = [];
let child;
let browser;
let baseUrl;
let mailId;
let firstEvidenceId;
let persistedWork;

async function startService(revision = 1) {
  child = spawn(process.env.ARIADNE_PYTHON || 'python', [
    '-m', 'scripts.mail_test_server', '--db', database, '--revision', String(revision),
  ], {
    cwd: root,
    env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8', ARIADNE_DATA_DIR: join(temporary, 'settings') },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const lines = createInterface({ input: child.stdout });
  child.stderr.resume();
  baseUrl = await new Promise((accept, reject) => {
    const timer = setTimeout(() => finish(new Error('Mail HTTP fixture did not become ready')), 15_000);
    const finish = (error, value) => {
      clearTimeout(timer);
      lines.off('line', onLine);
      child.off('error', onError);
      child.off('exit', onExit);
      error ? reject(error) : accept(value);
    };
    const onError = () => finish(new Error('Could not start mail browser fixture'));
    const onExit = code => finish(new Error(`Mail fixture exited before readiness (${code})`));
    const onLine = line => {
      let result;
      try { result = JSON.parse(line); } catch { return; }
      if (typeof result.url === 'string' && /^http:\/\/127\.0\.0\.1:\d+$/.test(result.url)) finish(null, result.url);
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

async function api(method, path, payload, expected = 200) {
  const response = await fetch(baseUrl + path, {
    method, headers: { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  assert.equal(response.status, expected, `${method} ${path}: unexpected status`);
  return response.json();
}

function responseFor(page, path, method) {
  return page.waitForResponse(response => response.url() === baseUrl + path && response.request().method() === method);
}

async function verify(name, action) {
  await action();
  checks.push(name);
  process.stdout.write(`PASS ${name}\n`);
}

async function closeDialog(page, dialog, close) {
  if (await page.locator(dialog).isVisible()) await page.locator(close).click();
}

async function openOutlook(page) {
  await closeDialog(page, '#mail-dialog', '#mail-close');
  if (!(await page.locator('#settings-dialog').isVisible())) await page.locator('#settings-open').click();
  if (!(await page.locator('#outlook-dialog').isVisible())) await page.locator('#outlook-open').click();
  await page.waitForFunction(() => document.querySelector('#outlook-fetch')?.disabled === false);
}

async function readMail(page) {
  await openOutlook(page);
  await page.locator('#outlook-folder').selectOption('inbox');
  await page.locator('#outlook-start').fill('2026-09-01');
  await page.locator('#outlook-end').fill('2026-09-30');
  await page.locator('#outlook-limit').fill('10');
  const response = responseFor(page, outlookPath + '/messages', 'POST');
  await page.locator('#outlook-fetch').click();
  const loaded = await response;
  assert.equal(loaded.status(), 200);
  const result = await loaded.json();
  assert.equal(typeof result.previewId, 'string');
  assert(result.previewId.length > 20, 'The actual service must issue the preview grant');
  await page.locator('input[name="outlook-message"][value="synthetic-mail-001"]').waitFor();
  return result;
}

async function saveSelection(page) {
  await page.locator('input[name="outlook-message"][value="synthetic-mail-001"]').check();
  await page.locator('#outlook-import-actor').fill('synthetic-browser-reviewer');
  const response = responseFor(page, '/api/mail/import', 'POST');
  await page.locator('#outlook-import-submit').click();
  const saved = await response;
  assert.equal(saved.status(), 200);
  const request = saved.request().postDataJSON();
  assert.deepEqual(Object.keys(request).sort(), ['actorId', 'messageIds', 'previewId']);
  assert.deepEqual(request.messageIds, ['synthetic-mail-001']);
  await page.locator('#outlook-import-result').waitFor({ state: 'visible' });
  return saved.json();
}

async function openArchive(page, query = '수익자 요청') {
  await closeDialog(page, '#outlook-dialog', '#outlook-close');
  await closeDialog(page, '#settings-dialog', '#settings-close');
  if (!(await page.locator('#mail-dialog').isVisible())) await page.locator('#mail-open').click();
  await page.locator('#mail-query').fill(query);
  const response = responseFor(page, '/api/mail/search', 'POST');
  await page.locator('#mail-search-submit').click();
  assert.equal((await response).status(), 200);
  await page.locator(`[data-mail-id="${mailId}"]`).waitFor();
  const detail = responseFor(page, `/api/mail/${mailId}`, 'GET');
  await page.locator(`[data-mail-id="${mailId}"]`).click();
  const loaded = await detail;
  assert.equal(loaded.status(), 200);
  const selected = (await loaded.json()).mail;
  await page.locator('#mail-detail').waitFor({ state: 'visible' });
  await page.waitForFunction(body => document.querySelector('#mail-detail-body')?.textContent === body, selected.message.bodyText);
  await page.waitForFunction(() => Array.from(document.querySelector('#mail-work-item')?.options || []).some(option => option.value === 'synthetic-task-001'));
}

function assertBusinessFieldsUnchanged(before, after) {
  for (const field of ['title', 'status', 'ownerId', 'fundId', 'due', 'reviewRequired', 'confidence', 'completionConditions', 'completionEvidence', 'waitingReason']) {
    assert.deepEqual(after[field], before[field], `Mail link changed work field ${field}`);
  }
}

async function run() {
  await startService();
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
  page.on('dialog', dialog => dialog.accept());
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(baseUrl);
  await page.locator('#detail-title').waitFor();
  const originalWork = (await api('GET', taskPath)).item;

  await verify('selected Outlook preview is captured server-side and persisted without changing work', async () => {
    await readMail(page);
    const result = await saveSelection(page);
    assert.equal(result.created, 1);
    assert.equal(result.unchanged, 0);
    assert.equal(result.revised, 0);
    mailId = result.items[0].mailId;
    firstEvidenceId = result.items[0].evidenceId;
    assert.equal(result.items[0].revision, 1);
    assert.deepEqual((await api('GET', taskPath)).item, originalWork);
    const stored = (await api('GET', `/api/mail/${mailId}`)).mail;
    assert.equal(stored.message.subject, subject);
    assert.equal(stored.message.attachments[0].state, 'not_downloaded');
    assert.equal(stored.revisions.length, 1);
  });

  await verify('reimporting unchanged mail is idempotent and creates no revision or work audit', async () => {
    const beforeAudit = (await api('GET', taskPath + '/audit')).entries;
    await readMail(page);
    const result = await saveSelection(page);
    assert.equal(result.created, 0);
    assert.equal(result.revised, 0);
    assert.equal(result.unchanged, 1);
    assert.equal(result.items[0].mailId, mailId);
    assert.equal(result.items[0].evidenceId, firstEvidenceId);
    assert.equal((await api('GET', `/api/mail/${mailId}`)).mail.revisions.length, 1);
    assert.deepEqual((await api('GET', taskPath + '/audit')).entries, beforeAudit);
  });

  await verify('saved-mail search renders subject, body and attachment metadata as inert text', async () => {
    await openArchive(page);
    assert.equal(await page.locator('#mail-detail-subject').innerText(), subject);
    assert.match(await page.locator('#mail-detail-body').innerText(), /본문 버전 1/);
    assert((await page.locator('#mail-detail-body').innerText()).includes('<script>window.mailXss=2</script>'));
    assert((await page.locator('#mail-attachments').innerText()).includes(attachment));
    assert.match(await page.locator('#mail-attachments').innerText(), /미다운로드|미수집|다운로드하지|미저장|받지 않|내용 미/);
    assert.equal(await page.locator('#mail-dialog img, #mail-dialog script').count(), 0);
    assert.equal(await page.evaluate(() => window.mailXss), undefined);
    const searched = await api('POST', '/api/mail/search', { query: '분배일 검토', limit: 20, offset: 0 });
    assert.equal(searched.total, 1);
    assert.equal(searched.items[0].id, mailId);
  });

  await verify('stale work versions require explicit refresh before linking and preserve user edits', async () => {
    await page.locator('#mail-work-item').selectOption(taskId);
    await page.locator('#mail-link-actor').fill('synthetic-browser-reviewer');
    await page.locator('#mail-link-reason').fill('수익자 요청 근거 연결 검토');
    const before = (await api('GET', taskPath)).item;
    const edited = (await api('PATCH', taskPath, {
      patch: { title: '[가상] 다른 창에서 확정한 분배 검토', ownerId: '가상 담당자' },
      expectedVersion: before.version, actorId: 'synthetic-second-window', reason: '별도 창 업무 수정',
    })).item;
    const conflict = responseFor(page, `/api/mail/${mailId}/link`, 'POST');
    await page.locator('#mail-link-submit').click();
    assert.equal((await conflict).status(), 409);
    await page.locator('#mail-error').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#mail-link-reason').inputValue(), '수익자 요청 근거 연결 검토');
    assert.equal(await page.locator('#mail-link-submit').isDisabled(), true);
    assert.deepEqual((await api('GET', taskPath)).item, edited);
    const refreshed = responseFor(page, '/api/work-items', 'GET');
    await page.locator('#mail-work-refresh').click();
    assert.equal((await refreshed).status(), 200);
    await page.waitForFunction(() => document.querySelector('#mail-link-submit')?.disabled === false);
    const linked = responseFor(page, `/api/mail/${mailId}/link`, 'POST');
    await page.locator('#mail-link-submit').click();
    const response = await linked;
    assert.equal(response.status(), 200);
    const result = await response.json();
    assert.equal(result.alreadyLinked, false);
    assertBusinessFieldsUnchanged(edited, result.item);
    assert.equal(result.item.version, edited.version + 1);
    assert(result.item.sources.includes(firstEvidenceId));
    persistedWork = result.item;
    await page.waitForFunction(() => document.querySelector('#mail-linked-items')?.textContent.includes('다른 창에서 확정한 분배 검토'));
  });

  await verify('repeating a link creates no duplicate source, audit entry or work version', async () => {
    const audit = (await api('GET', taskPath + '/audit')).entries;
    const replay = responseFor(page, `/api/mail/${mailId}/link`, 'POST');
    await page.locator('#mail-link-submit').click();
    const response = await replay;
    assert.equal(response.status(), 200);
    assert.equal((await response.json()).alreadyLinked, true);
    await page.waitForFunction(() => document.querySelector('#mail-link-result')?.textContent.includes('이미 연결'));
    const duplicate = await api('POST', `/api/mail/${mailId}/link`, {
      revision: 1, workItemId: taskId, expectedVersion: 1,
      actorId: 'synthetic-browser-reviewer', reason: '동일 근거 연결 재시도',
    });
    assert.equal(duplicate.alreadyLinked, true);
    assert.deepEqual(duplicate.item, persistedWork);
    assert.equal(duplicate.item.sources.filter(value => value === firstEvidenceId).length, 1);
    assert.deepEqual((await api('GET', taskPath + '/audit')).entries, audit);
  });

  await verify('provider changes create a new revision while the linked original remains readable after restart', async () => {
    await page.goto('about:blank');
    await stopService();
    await startService(2);
    await page.goto(baseUrl);
    await page.locator('#detail-title').waitFor();
    assert.deepEqual((await api('GET', taskPath)).item, persistedWork);
    await openArchive(page);
    assert.match(await page.locator('#mail-detail-body').innerText(), /본문 버전 1/);
    assert.match(await page.locator('#mail-linked-items').innerText(), /다른 창에서 확정한 분배 검토/);
    await readMail(page);
    const result = await saveSelection(page);
    assert.equal(result.created, 0);
    assert.equal(result.unchanged, 0);
    assert.equal(result.revised, 1);
    assert.equal(result.items[0].mailId, mailId);
    assert.equal(result.items[0].revision, 2);
    assert.notEqual(result.items[0].evidenceId, firstEvidenceId);
    assert.deepEqual((await api('GET', taskPath)).item, persistedWork);
    await openArchive(page);
    assert.match(await page.locator('#mail-detail-body').innerText(), /본문 버전 2/);
    assert.equal((await api('GET', `/api/mail/${mailId}`)).mail.linkedWorkItems.length, 0);
    const oldRevision = responseFor(page, `/api/mail/${mailId}/revisions/1`, 'GET');
    await page.locator('#mail-revision').selectOption('1');
    assert.equal((await oldRevision).status(), 200);
    await page.waitForFunction(() => document.querySelector('#mail-detail-body')?.textContent.includes('본문 버전 1'));
    assert.match(await page.locator('#mail-linked-items').innerText(), /다른 창에서 확정한 분배 검토/);
    const original = (await api('GET', `/api/mail/${mailId}/revisions/1`)).mail;
    assert.equal(original.evidenceId, firstEvidenceId);
    assert.equal(original.linkedWorkItems[0].id, taskId);
    // The work evidence card must reopen its linked snapshot, not silently use v2.
    await closeDialog(page, '#mail-dialog', '#mail-close');
    await page.locator('#refresh').click();
    await page.waitForFunction(() => document.querySelector('#refresh')?.disabled === false);
    await page.locator('#tab-evidence').click();
    const sourceCard = page.locator('#evidence-list').getByRole('button', { name: '저장한 메일 보기' });
    await sourceCard.waitFor();
    const fromEvidence = responseFor(page, `/api/mail/${mailId}/revisions/1`, 'GET');
    await sourceCard.click();
    assert.equal((await fromEvidence).status(), 200);
    await page.locator('#mail-dialog').waitFor({ state: 'visible' });
    await page.waitForFunction(() => document.querySelector('#mail-detail-body')?.textContent.includes('본문 버전 1'));
    assert.equal(await page.locator('#mail-revision').inputValue(), '1');
  });

  await verify('disconnect invalidates a visible preview before import and leaves saved mail intact', async () => {
    await readMail(page);
    await page.locator('input[name="outlook-message"][value="synthetic-mail-001"]').check();
    await page.locator('#outlook-import-actor').fill('synthetic-browser-reviewer');
    // Another local window disconnects after this window rendered its preview.
    await api('DELETE', outlookPath);
    const rejected = responseFor(page, '/api/mail/import', 'POST');
    await page.locator('#outlook-import-submit').click();
    assert.equal((await rejected).status(), 410);
    await page.locator('#outlook-error').waitFor({ state: 'visible' });
    const stored = (await api('GET', `/api/mail/${mailId}`)).mail;
    assert.equal(stored.revisions.length, 2);
    assert.deepEqual((await api('GET', taskPath)).item, persistedWork);
  });

  await verify('both revisions, attachment metadata and original work link survive a second restart', async () => {
    await page.goto('about:blank');
    await stopService();
    await startService(2);
    await page.goto(baseUrl);
    await page.locator('#detail-title').waitFor();
    await openArchive(page);
    const stored = (await api('GET', `/api/mail/${mailId}`)).mail;
    assert.equal(stored.revisions.length, 2);
    assert.equal(stored.message.attachments[0].state, 'not_downloaded');
    assert.equal(await page.locator('#mail-revision option').count(), 2);
    assert.match(await page.locator('#mail-detail-body').innerText(), /본문 버전 2/);
    assert.deepEqual((await api('GET', taskPath)).item, persistedWork);
    const original = (await api('GET', `/api/mail/${mailId}/revisions/1`)).mail;
    assert.equal(original.linkedWorkItems[0].id, taskId);
    assert.equal(original.evidenceId, firstEvidenceId);
  });

  assert.deepEqual(errors, [], 'Browser raised an uncaught script error');
  assert.deepEqual(externalRequests, [], 'Synthetic tests must not contact a provider');
}

try {
  await run();
  process.stdout.write(`\n${checks.length} mail archive browser checks passed.\n`);
} catch (error) {
  process.stderr.write(`FAIL ${error.message}\n`);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  await stopService();
  await rm(temporary, { recursive: true, force: true });
}
