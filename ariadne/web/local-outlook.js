// User-triggered local COM collection. No mailbox data is supplied by the UI.
const el = (id) => document.getElementById(`local-outlook-${id}`);
const dialog = el('dialog');
const state = {supported: false, busy: false, pending: false, folders: [], job: null, timer: null, request: null, epoch: 0};

function error(message = '') {
  el('error').textContent = message;
  el('error').hidden = !message;
}
async function request(path = '', body) {
  let response;
  try {
    response = await fetch(`/api/local-outlook${path}`, {
      method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch { throw new Error('서버 응답을 확인하지 못했습니다. 진행 상태 확인을 눌러 주세요.'); }
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || '작업을 완료하지 못했습니다.');
  return value;
}
function controls() {
  const locked = state.busy || state.pending;
  el('discover').disabled = locked || !state.supported;
  for (const id of ['account', 'folder', 'basis', 'start', 'end', 'limit', 'actor']) el(id).disabled = locked || !state.supported;
  el('account').disabled ||= !state.folders.length;
  el('folder').disabled ||= !state.folders.length;
  el('collect').disabled = locked || !state.supported || !el('folder').value || !el('actor').value.trim();
  el('recheck').disabled = state.pending;
}
function option(value, label) {
  const o = document.createElement('option'); o.value = value; o.textContent = label; return o;
}
function folders() {
  const current = el('folder').value;
  el('folder').replaceChildren(option('', '폴더 선택'));
  for (const f of state.folders.filter(f => f.accountKey === el('account').value)) el('folder').append(option(f.key, f.path));
  if ([...el('folder').options].some(o => o.value === current)) el('folder').value = current;
  controls();
}
function render(job) {
  state.job = job.id;
  state.busy = job.state === 'running';
  el('progress').textContent = job.state === 'running'
    ? (job.stage === 'saving' ? `근거함에 저장 중 · ${job.processed}/${job.total}건` : 'Outlook에서 읽는 중입니다. 최대 3분 정도 걸릴 수 있습니다.')
    : job.message;
  el('result').textContent = job.kind === 'collect'
    ? `신규 ${job.created}건 · 기존과 동일 ${job.unchanged}건 · 변경 버전 ${job.revised}건 · 실패 ${job.failed}건`
    : '';
  el('warning').textContent = job.truncated ? '지정한 상한으로 일부만 읽었습니다. 전체 수집 완료가 아닙니다. 날짜 범위를 나누어 다시 실행해 주세요.' : '';
  if (job.state === 'failed') error(job.message);
  if (job.kind === 'discover' && job.state === 'completed') {
    const account = el('account').value;
    state.folders = job.folders;
    el('account').replaceChildren(option('', '사서함 선택'));
    const accounts = new Map(state.folders.map(f => [f.accountKey, f.account]));
    let number = 0;
    for (const [key, name] of accounts) {
      number++;
      const duplicate = [...accounts.values()].filter(v => v === name).length > 1;
      el('account').append(option(key, duplicate ? `${name} · 사서함 ${number}` : name));
    }
    if ([...el('account').options].some(o => o.value === account)) el('account').value = account;
    else if (el('account').options.length === 2) el('account').selectedIndex = 1;
    folders();
    el('catalog-note').textContent = `${job.message}${job.warnings ? ` 접근하지 못한 항목 ${job.warnings}개가 있습니다.` : ''}`;
  }
  controls();
}
function schedule() {
  clearTimeout(state.timer);
  if (dialog.open && state.busy) state.timer = setTimeout(poll, 900);
}
async function poll() {
  const epoch = state.epoch;
  try {
    const job = await request(`/jobs/${encodeURIComponent(state.job)}`);
    if (epoch !== state.epoch || !dialog.open) return;
    render(job); schedule();
  } catch (e) { if (epoch === state.epoch) { error(e.message); controls(); } }
}
async function refresh() {
  if (state.pending) return;
  state.pending = true; controls(); error();
  const epoch = state.epoch;
  try {
    const status = await request();
    if (epoch !== state.epoch || !dialog.open) return;
    state.supported = status.supported;
    el('status').textContent = status.message;
    const id = status.activeJobId || status.lastJobId;
    state.busy = Boolean(status.activeJobId);
    if (id) { state.job = id; await poll(); }
  } catch (e) { if (epoch === state.epoch) error(e.message); }
  finally { state.pending = false; controls(); }
}
async function launch(kind, values = {}) {
  if (state.busy || state.pending) return;
  state.pending = true; controls(); error();
  const fingerprint = JSON.stringify([kind, values]);
  // Keep request identity on uncertain network response; never auto-retry writes.
  if (state.request?.fingerprint !== fingerprint) state.request = {fingerprint, id: crypto.randomUUID()};
  try {
    const job = await request(`/${kind}`, {requestId: state.request.id, ...values});
    state.request = null;
    render(job); schedule();
  } catch (e) { error(e.message); }
  finally { state.pending = false; controls(); }
}
function close() { dialog.close(); }
el('open').addEventListener('click', () => {
  dialog.showModal(); state.epoch++;
  if (!el('start').value) {
    const now = new Date(Date.now() + 9 * 3600000);
    el('end').value = now.toISOString().slice(0, 10);
    now.setUTCDate(now.getUTCDate() - 6); el('start').value = now.toISOString().slice(0, 10);
  }
  refresh();
});
el('close').addEventListener('click', close);
dialog.addEventListener('close', () => {state.epoch++; clearTimeout(state.timer);});
el('recheck').addEventListener('click', refresh);
el('discover').addEventListener('click', () => launch('discover'));
el('account').addEventListener('change', folders);
el('folder').addEventListener('change', controls);
el('actor').addEventListener('input', controls);
el('form').addEventListener('submit', event => {
  event.preventDefault();
  launch('collect', {folderKey: el('folder').value, basis: el('basis').value,
    startDate: el('start').value, endDate: el('end').value,
    limit: Number(el('limit').value), actorId: el('actor').value.trim()});
});
el('archive').addEventListener('click', () => {close(); document.getElementById('mail-open').click();});
