// Outlook is deliberately separate from work-item editing and OpenAI settings.
// Preview text is never interpreted as HTML. Saving sends only server preview IDs.
const element = (id) => document.getElementById(id);
const dialog = element('outlook-dialog');
const endpoint = '/api/connections/outlook';
const labels = {
  unavailable: '이 환경에서는 Outlook 연결을 사용할 수 없습니다.',
  not_configured: '최초 연결 설정이 필요합니다.',
  disconnected: '로그인이 필요합니다.',
  connecting: 'Microsoft 로그인 대기 중',
  connected: '로그인 정보 저장됨 · 메일 읽기로 접근 확인',
  reauth_required: 'Microsoft 로그인을 다시 진행해 주세요.',
  error: '연결 상태를 확인해 주세요.'
};
const state = {
  status: null,
  dirty: false,
  busy: false,
  loading: false,
  reading: false,
  importing: false,
  previewId: null,
  selected: new Set(),
  epoch: 0,
  statusSequence: 0,
  mailSequence: 0,
  pollTimer: null
};

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function showError(message = '') {
  element('outlook-error').textContent = message;
  element('outlook-error').hidden = !message;
}

async function request(path = '', method = 'GET', body, base = endpoint) {
  let response;
  try {
    response = await fetch(base + path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch {
    throw new Error('서버에 연결하지 못했습니다. ARIADNE 실행 상태를 확인해 주세요.');
  }
  let value;
  try {
    value = await response.json();
  } catch {
    throw new Error('서버 응답을 확인하지 못했습니다. 상태 확인을 눌러 다시 확인해 주세요.');
  }
  if (!response.ok) {
    // Do not reflect provider response bodies, login URLs or token details.
    const messages = {
      400: '입력한 ID 또는 조회 조건을 확인해 주세요.',
      401: '로그인이 만료되었거나 승인이 필요합니다. Microsoft 로그인을 다시 진행해 주세요.',
      403: '메일 읽기 권한 또는 회사의 관리자 승인이 필요합니다.',
      409: '다른 연결 작업이 진행 중이거나 연결 상태가 바뀌었습니다. 상태를 다시 확인해 주세요.',
      429: '요청이 많아 일시적으로 제한되었습니다. 잠시 후 직접 다시 시도해 주세요.',
      503: 'Outlook 연결을 사용할 수 없습니다. 실행 환경과 연결 설정을 확인해 주세요.'
    };
    if (base === '/api/mail' && [404, 409, 410].includes(response.status)) {
      throw new Error('미리보기가 만료되었거나 연결 상태가 바뀌었습니다. 메일을 다시 읽고 선택해 주세요.');
    }
    throw new Error(messages[response.status] || '요청을 완료하지 못했습니다. 연결 상태를 확인한 후 다시 시도해 주세요.');
  }
  return value;
}

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function schedulePoll() {
  stopPolling();
  if (dialog.open && !state.busy && state.status?.state === 'connecting') {
    state.pollTimer = setTimeout(() => refreshStatus(), 1500);
  }
}

function clearPreview(message = '선택한 폴더와 기간으로 메일을 읽어 주세요.') {
  state.mailSequence += 1;
  state.reading = false;
  state.previewId = null;
  state.selected.clear();
  element('outlook-import-result').hidden = true;
  element('outlook-import-result').textContent = '';
  element('outlook-results').replaceChildren();
  element('outlook-results-summary').textContent = message;
  updateSelection();
}

function updateSelection() {
  element('outlook-selection-count').textContent = `${state.selected.size}건 선택 · 한 번에 최대 10건`;
  element('outlook-import-submit').disabled = state.importing || state.busy || state.loading || state.reading || !state.previewId || !state.selected.size || !element('outlook-import-actor').value.trim();
  element('outlook-import-submit').textContent = state.importing ? '메일을 저장하고 있습니다…' : '선택한 메일 저장';
  element('outlook-import-actor').disabled = state.importing;
  for (const checkbox of element('outlook-results').querySelectorAll('input[name="outlook-message"]')) {
    checkbox.disabled = state.importing || !state.previewId || (!checkbox.checked && state.selected.size >= 10);
  }
}

function updateControls() {
  const status = state.status;
  const supported = Boolean(status?.supported);
  const connecting = status?.state === 'connecting';
  const blocked = state.busy || state.loading || state.importing;
  element('outlook-client-id').disabled = !supported || state.busy || state.importing;
  element('outlook-tenant-id').disabled = !supported || state.busy || state.importing;
  element('outlook-save').disabled = !supported || blocked || connecting;
  element('outlook-login').disabled = !supported || !status?.configured || blocked || connecting || state.dirty;
  element('outlook-login').textContent = status?.account ? 'Microsoft 다시 로그인' : 'Microsoft 로그인';
  // Deleting cached credentials remains available when a dependency is missing.
  element('outlook-disconnect').disabled = !status?.configured || state.busy || state.importing;
  element('outlook-disconnect').textContent = connecting ? '로그인 취소' : '연결 해제';
  element('outlook-refresh').disabled = blocked;
  element('outlook-close').disabled = state.busy || state.importing;
  element('outlook-fetch').disabled = blocked || state.reading || !supported || status?.state !== 'connected' || state.dirty;
  element('outlook-fetch').textContent = state.reading ? '메일을 읽고 있습니다…' : '메일 읽기';
  element('outlook-unsaved').hidden = !state.dirty;
  element('outlook-login-note').hidden = !connecting;
  for (const id of ['outlook-folder', 'outlook-start', 'outlook-end', 'outlook-limit']) element(id).disabled = state.importing;
  updateSelection();
}

function renderStatus(status) {
  const previous = state.status;
  state.status = status;
  element('outlook-status').textContent = labels[status.state] || labels.error;
  element('outlook-message').textContent = status.message || '';
  const account = status.account;
  element('outlook-account').textContent = account ? [account.name, account.username].filter(Boolean).join(' · ') : '';
  element('outlook-account').hidden = !account;
  // A status poll must never replace an in-progress configuration edit.
  if (!state.dirty) {
    element('outlook-client-id').value = status.config?.clientId || '';
    element('outlook-tenant-id').value = status.config?.tenantId || '';
  }
  const accountChanged = previous?.account?.username !== account?.username;
  const configChanged = previous?.config?.clientId !== status.config?.clientId || previous?.config?.tenantId !== status.config?.tenantId;
  if (status.state !== 'connected' || accountChanged || configChanged) {
    clearPreview(status.state === 'connected' ? undefined : '로그인한 후 메일을 읽을 수 있습니다.');
  }
  updateControls();
}

async function refreshStatus() {
  if (!dialog.open || state.busy || state.importing) return;
  stopPolling();
  const epoch = state.epoch;
  const sequence = ++state.statusSequence;
  state.loading = true;
  updateControls();
  try {
    const status = await request();
    if (!dialog.open || epoch !== state.epoch || sequence !== state.statusSequence) return;
    renderStatus(status);
    schedulePoll();
  } catch (error) {
    if (!dialog.open || epoch !== state.epoch || sequence !== state.statusSequence) return;
    element('outlook-status').textContent = '연결 상태를 확인하지 못했습니다.';
    showError(error.message);
  } finally {
    if (epoch === state.epoch && sequence === state.statusSequence) {
      state.loading = false;
      updateControls();
    }
  }
}

function beginMutation() {
  stopPolling();
  state.epoch += 1;
  state.statusSequence += 1;
  state.loading = false;
  state.busy = true;
  clearPreview();
  showError();
  updateControls();
  return state.epoch;
}

async function mutate(path, method, body, { resetDraft = false } = {}) {
  const epoch = beginMutation();
  let succeeded = false;
  try {
    const status = await request(path, method, body);
    if (epoch !== state.epoch || !dialog.open) return;
    if (resetDraft) state.dirty = false;
    renderStatus(status);
    succeeded = true;
  } catch (error) {
    if (epoch !== state.epoch || !dialog.open) return;
    showError(error.message + ' 작업은 자동으로 재시도하지 않습니다.');
  } finally {
    if (epoch === state.epoch) {
      state.busy = false;
      updateControls();
      if (succeeded) schedulePoll();
      // A failed response can still have changed the server. Reconcile once.
      else if (dialog.open) await refreshStatus();
    }
  }
}

function kstDate(date) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit'
  }).format(date);
}

function displayTime(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return '일시 정보 없음';
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', dateStyle: 'medium', timeStyle: 'short'
  }).format(date) + ' KST';
}

function renderMessages(result) {
  const target = element('outlook-results');
  target.replaceChildren();
  state.previewId = typeof result.previewId === 'string' && result.previewId ? result.previewId : null;
  state.selected.clear();
  const folder = result.folder === 'sentitems' ? '보낸 편지함' : '받은 편지함';
  const summary = `${folder} · ${result.startDate} ~ ${result.endDate} (한국시간, 종료일 포함) · ${result.items.length}건`;
  const truncation = result.truncated ? ' · 조회 한도에 도달했습니다. 일부 메일만 표시될 수 있으므로 기간을 좁혀 확인해 주세요.' : '';
  element('outlook-results-summary').textContent = summary + truncation + (result.message ? ` ${result.message}` : '');
  if (!result.items.length) {
    target.append(textNode('p', '지정한 조건에 해당하는 메일이 없습니다.', 'help'));
    return;
  }
  for (const mail of result.items) {
    const row = document.createElement('div');
    row.className = 'outlook-mail-row';
    const selectLabel = textNode('label', '근거 저장 선택', 'outlook-select');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.name = 'outlook-message';
    checkbox.value = mail.id;
    checkbox.setAttribute('aria-label', `근거로 저장할 메일 선택: ${mail.subject || '(제목 없음)'}`);
    checkbox.disabled = !state.previewId;
    checkbox.addEventListener('change', () => {
      if (checkbox.checked && state.selected.size < 10) state.selected.add(mail.id);
      else {
        checkbox.checked = false;
        state.selected.delete(mail.id);
      }
      element('outlook-import-result').hidden = true;
      updateSelection();
    });
    selectLabel.prepend(checkbox);
    const card = document.createElement('details');
    card.className = 'outlook-mail';
    card.append(textNode('summary', mail.subject || '(제목 없음)'));
    card.append(textNode('p', `보낸 사람: ${[mail.fromName, mail.fromAddress].filter(Boolean).join(' · ') || '정보 없음'}`, 'help'));
    const date = result.folder === 'sentitems' ? mail.sentAt : mail.receivedAt;
    card.append(textNode('p', `${result.folder === 'sentitems' ? '발신' : '수신'}: ${displayTime(date)}`, 'help'));
    if (mail.hasAttachments) card.append(textNode('p', '첨부파일 있음 · 첨부파일은 내려받지 않았습니다.', 'help'));
    if (mail.bodyTruncated) card.append(textNode('p', '본문이 길어 일부만 표시합니다. 전체 내용은 Outlook에서 확인해 주세요.', 'help'));
    card.append(textNode('p', mail.bodyText || '(본문 없음)', 'outlook-body'));
    row.append(selectLabel, card);
    target.append(row);
  }
  updateSelection();
}

element('outlook-open').addEventListener('click', () => {
  state.epoch += 1;
  showError();
  clearPreview('로그인한 후 메일을 읽을 수 있습니다.');
  if (!element('outlook-import-actor').value) element('outlook-import-actor').value = element('edit-actor').value || element('create-actor').value;
  dialog.showModal();
  refreshStatus();
});

function closeDialog() {
  if (state.busy || state.importing) return;
  dialog.close();
}

element('outlook-close').addEventListener('click', closeDialog);
dialog.addEventListener('cancel', (event) => {
  event.preventDefault();
  closeDialog();
});
dialog.addEventListener('close', () => {
  state.epoch += 1;
  state.statusSequence += 1;
  state.loading = false;
  stopPolling();
  clearPreview();
  // Closing the panel stops polling, but never silently cancels an OS login.
});

for (const id of ['outlook-client-id', 'outlook-tenant-id']) {
  element(id).addEventListener('input', () => {
    state.dirty = true;
    clearPreview('연결 설정을 저장하고 로그인한 후 메일을 읽어 주세요.');
    updateControls();
  });
}

element('outlook-refresh').addEventListener('click', () => {
  showError();
  refreshStatus();
});

element('outlook-config-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.busy || state.loading || state.importing || !state.status?.supported || state.status.state === 'connecting') return;
  const clientId = element('outlook-client-id').value.trim();
  const tenantId = element('outlook-tenant-id').value.trim();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuid.test(clientId) || !uuid.test(tenantId)) {
    showError('두 ID 모두 Microsoft 앱 등록에 표시된 UUID 형식으로 입력해 주세요.');
    return;
  }
  await mutate('', 'POST', { clientId, tenantId }, { resetDraft: true });
});

element('outlook-login').addEventListener('click', async () => {
  if (element('outlook-login').disabled) return;
  await mutate('/login', 'POST', {});
});

element('outlook-disconnect').addEventListener('click', async () => {
  if (element('outlook-disconnect').disabled) return;
  await mutate('', 'DELETE');
});

for (const id of ['outlook-folder', 'outlook-start', 'outlook-end', 'outlook-limit']) {
  element(id).addEventListener('input', () => {
    clearPreview('조회 조건이 변경되었습니다. 메일 읽기를 눌러 새로 확인해 주세요.');
    updateControls();
  });
}

element('outlook-mail-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (element('outlook-fetch').disabled) return;
  const query = {
    folder: element('outlook-folder').value,
    startDate: element('outlook-start').value,
    endDate: element('outlook-end').value,
    limit: Number(element('outlook-limit').value)
  };
  clearPreview();
  if (!query.startDate || !query.endDate || query.startDate > query.endDate || !Number.isInteger(query.limit) || query.limit < 1 || query.limit > 50) {
    showError('시작일·종료일과 조회 수(1~50건)를 확인해 주세요.');
    updateControls();
    return;
  }
  const epoch = state.epoch;
  const sequence = state.mailSequence;
  state.reading = true;
  showError();
  updateControls();
  element('outlook-results-summary').textContent = 'Microsoft Graph에서 메일을 읽고 있습니다.';
  try {
    const result = await request('/messages', 'POST', query);
    if (!dialog.open || epoch !== state.epoch || sequence !== state.mailSequence) return;
    renderMessages(result);
  } catch (error) {
    if (!dialog.open || epoch !== state.epoch || sequence !== state.mailSequence) return;
    clearPreview('메일을 읽지 못했습니다. 이전 조회 결과는 지웠습니다.');
    showError(error.message);
    await refreshStatus();
  } finally {
    if (epoch === state.epoch && sequence === state.mailSequence) {
      state.reading = false;
      updateControls();
    }
  }
});

const now = new Date();
element('outlook-end').value = kstDate(now);
element('outlook-start').value = kstDate(new Date(now.getTime() - 6 * 24 * 60 * 60 * 1000));
window.addEventListener('beforeunload', (event) => {
  if (state.busy || state.importing) {
    event.preventDefault();
    event.returnValue = '';
  }
});

element('outlook-import-actor').addEventListener('input', updateSelection);
element('outlook-import-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (element('outlook-import-submit').disabled || state.selected.size > 10) return;
  const epoch = state.epoch;
  const sequence = state.mailSequence;
  const body = {
    previewId: state.previewId,
    messageIds: [...state.selected],
    actorId: element('outlook-import-actor').value.trim()
  };
  state.importing = true;
  stopPolling();
  showError();
  element('outlook-import-result').hidden = true;
  updateControls();
  try {
    const result = await request('/import', 'POST', body, '/api/mail');
    if (!dialog.open || epoch !== state.epoch || sequence !== state.mailSequence) return;
    state.selected.clear();
    for (const checkbox of element('outlook-results').querySelectorAll('input[name="outlook-message"]')) checkbox.checked = false;
    element('outlook-import-result').textContent = `신규 ${result.created}건 · 변경 버전 ${result.revised}건 · 기존과 동일 ${result.unchanged}건. 상단의 메일 근거함에서 확인하고 업무에 연결할 수 있습니다.`;
    element('outlook-import-result').hidden = false;
  } catch (error) {
    if (!dialog.open || epoch !== state.epoch || sequence !== state.mailSequence) return;
    showError(error.message + ' 저장은 자동으로 재시도하지 않습니다.');
  } finally {
    state.importing = false;
    updateControls();
  }
});
