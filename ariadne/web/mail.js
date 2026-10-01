// Saved mail is local evidence. Never execute message or attachment content.
const element = (id) => document.getElementById(id);
const dialog = element('mail-dialog');
const state = {
  epoch: 0,
  searchSequence: 0,
  detailSequence: 0,
  workSequence: 0,
  searching: false,
  loadingDetail: false,
  loadingWork: false,
  linking: false,
  conflicted: false,
  query: '',
  offset: 0,
  limit: 20,
  total: 0,
  selected: null,
  detail: null,
  workItems: []
};

function node(tag, text, className) {
  const value = document.createElement(tag);
  value.textContent = text;
  if (className) value.className = className;
  return value;
}

function showError(text = '') {
  element('mail-error').textContent = text;
  element('mail-error').hidden = !text;
}

function showLinkResult(text = '') {
  element('mail-link-result').textContent = text;
  element('mail-link-result').hidden = !text;
}

async function request(path, method = 'GET', body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch {
    throw new Error('서버에 연결하지 못했습니다. ARIADNE 실행 상태를 확인해 주세요.');
  }
  let value;
  try {
    value = await response.json();
  } catch {
    throw new Error('서버 응답을 확인하지 못했습니다. 목록을 새로 불러와 확인해 주세요.');
  }
  if (!response.ok) {
    const messages = {
      400: '입력 내용을 확인해 주세요.',
      403: '이 작업을 수행할 수 없습니다. 실행 주소와 권한을 확인해 주세요.',
      404: '선택한 메일 또는 업무를 찾을 수 없습니다. 목록을 다시 불러와 주세요.',
      409: '업무가 변경되었습니다. 최신 업무 목록을 불러와 내용을 확인한 후 다시 연결해 주세요.'
    };
    const error = new Error(messages[response.status] || '요청을 완료하지 못했습니다. 목록을 확인한 후 다시 시도해 주세요.');
    error.status = response.status;
    throw error;
  }
  return value;
}

function time(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return '일시 정보 없음';
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', dateStyle: 'medium', timeStyle: 'short'
  }).format(date) + ' KST';
}

function selectedWork() {
  return state.workItems.find((item) => item.id === element('mail-work-item').value);
}

function renderWorkContext() {
  const work = selectedWork();
  element('mail-work-context').textContent = work ? `${work.title} · 버전 ${work.version} · 상태 ${work.status || '미지정'}` : '';
}

function updateControls() {
  element('mail-close').disabled = state.linking;
  element('mail-query').disabled = state.linking;
  element('mail-search-submit').disabled = state.searching || state.linking;
  element('mail-prev').disabled = state.searching || state.linking || state.offset === 0;
  element('mail-next').disabled = state.searching || state.linking || state.offset + state.limit >= state.total;
  element('mail-revision').disabled = state.loadingDetail || state.linking;
  element('mail-work-item').disabled = state.loadingWork || state.loadingDetail || state.linking;
  element('mail-work-refresh').disabled = state.loadingWork || state.linking;
  element('mail-link-actor').disabled = state.linking;
  element('mail-link-reason').disabled = state.linking;
  element('mail-link-submit').disabled = state.linking || state.loadingWork || state.loadingDetail || state.conflicted || !state.detail || !selectedWork() || !element('mail-link-actor').value.trim() || !element('mail-link-reason').value.trim();
  element('mail-link-submit').textContent = state.linking ? '근거를 연결하고 있습니다…' : '이 버전을 업무에 연결';
  for (const button of element('mail-list').querySelectorAll('button')) button.disabled = state.linking;
}

function clearDetail() {
  state.detailSequence += 1;
  state.workSequence += 1;
  state.loadingDetail = false;
  state.loadingWork = false;
  state.selected = null;
  state.detail = null;
  state.workItems = [];
  state.conflicted = false;
  element('mail-detail').hidden = true;
  element('mail-detail-subject').textContent = '';
  element('mail-detail-body').textContent = '';
  element('mail-attachments').replaceChildren();
  element('mail-linked-items').replaceChildren();
  element('mail-work-item').replaceChildren(node('option', '업무를 선택해 주세요'));
  element('mail-work-item').firstChild.value = '';
  showLinkResult();
}

function renderList(result) {
  const target = element('mail-list');
  target.replaceChildren();
  state.total = result.total;
  element('mail-search-summary').textContent = result.total ? `전체 ${result.total}건 · ${state.offset + 1}–${state.offset + result.items.length}건 표시` : (state.query ? '검색 조건에 해당하는 메일이 없습니다.' : '저장된 메일이 없습니다. Outlook에서 필요한 메일을 선택해 저장해 주세요.');
  for (const mail of result.items) {
    const button = node('button', '', 'mail-list-item');
    button.type = 'button';
    button.dataset.mailId = mail.id;
    button.setAttribute('aria-pressed', 'false');
    button.append(node('strong', mail.subject || '(제목 없음)'));
    button.append(node('span', `${mail.mailboxUsername || '저장한 계정'} · 버전 ${mail.revision}`, 'help'));
    button.append(node('span', time(mail.receivedAt || mail.sentAt), 'help'));
    const notes = [];
    if (mail.bodyTruncated) notes.push('본문 일부');
    if (mail.attachmentCount) notes.push(`첨부 목록 ${mail.attachmentCount}개`);
    if (mail.attachmentsTruncated) notes.push('첨부 목록 일부');
    if (notes.length) button.append(node('span', notes.join(' · '), 'help'));
    button.addEventListener('click', () => {
      if (!state.linking) loadDetail(mail.id);
    });
    target.append(button);
  }
}

async function search(offset = 0) {
  if (state.linking) return;
  const epoch = state.epoch;
  const sequence = ++state.searchSequence;
  state.searching = true;
  state.offset = offset;
  clearDetail();
  showError();
  element('mail-search-summary').textContent = '저장된 메일을 찾고 있습니다.';
  element('mail-list').replaceChildren();
  updateControls();
  try {
    const result = await request('/api/mail/search', 'POST', { query: state.query, limit: state.limit, offset });
    if (!dialog.open || epoch !== state.epoch || sequence !== state.searchSequence) return;
    renderList(result);
  } catch (error) {
    if (!dialog.open || epoch !== state.epoch || sequence !== state.searchSequence) return;
    state.total = 0;
    element('mail-search-summary').textContent = '목록을 불러오지 못했습니다.';
    showError(error.message);
  } finally {
    if (epoch === state.epoch && sequence === state.searchSequence) {
      state.searching = false;
      updateControls();
    }
  }
}

function renderDetail(mail) {
  state.detail = mail;
  const message = mail.message;
  element('mail-detail').hidden = false;
  element('mail-detail-subject').textContent = message.subject || '(제목 없음)';
  const sender = [message.fromName, message.fromAddress].filter(Boolean).join(' · ') || '정보 없음';
  const recipients = (message.toRecipients || []).map((recipient) => [recipient.name, recipient.address].filter(Boolean).join(' ')).join(', ') || '정보 없음';
  element('mail-detail-meta').textContent = `계정: ${mail.mailboxUsername || '정보 없음'}\n보낸 사람: ${sender}\n받는 사람: ${recipients}${message.recipientsTruncated ? ' (일부만 보관)' : ''}\n수신: ${time(message.receivedAt)} · 발신: ${time(message.sentAt)}`;
  const revisions = element('mail-revision');
  revisions.replaceChildren();
  for (const revision of mail.revisions) {
    const option = node('option', `버전 ${revision.revision} · ${time(revision.capturedAt)}`);
    option.value = String(revision.revision);
    revisions.append(option);
  }
  revisions.value = String(mail.revision);
  const latest = Math.max(...mail.revisions.map((revision) => revision.revision));
  const versionNotes = [`저장 시각: ${time(mail.capturedAt)}`];
  if (mail.revision !== latest) versionNotes.push('이전 버전을 보고 있습니다. 연결 시 이 버전의 근거가 유지됩니다.');
  if (message.bodyTruncated) versionNotes.push('본문 일부만 보관되었습니다. 전체 내용은 Outlook에서 확인해 주세요.');
  element('mail-version-note').textContent = versionNotes.join(' · ');
  element('mail-detail-body').textContent = message.bodyText || '(본문 없음)';
  const attachments = element('mail-attachments');
  attachments.replaceChildren();
  for (const attachment of message.attachments || []) {
    const parts = [attachment.name || '(이름 없음)', attachment.contentType || '형식 정보 없음'];
    if (Number.isInteger(attachment.size)) parts.push(`${attachment.size.toLocaleString('ko-KR')}바이트`);
    if (attachment.isInline) parts.push('본문 포함 항목');
    parts.push('미다운로드');
    attachments.append(node('li', parts.join(' · ')));
  }
  if (!attachments.childElementCount) attachments.append(node('li', '보관된 첨부 목록이 없습니다.'));
  element('mail-attachments-note').textContent = message.attachmentsTruncated ? '첨부 목록이 일부만 보관되었습니다. 전체 목록은 Outlook에서 확인해 주세요.' : '';
  const links = element('mail-linked-items');
  links.replaceChildren();
  for (const item of mail.linkedWorkItems) links.append(node('li', `${item.title} · 업무 버전 ${item.version}`));
  if (!links.childElementCount) links.append(node('li', '이 버전에 연결된 업무가 없습니다.'));
  for (const button of element('mail-list').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.mailId === mail.id));
  updateControls();
}

async function loadWorkItems({ review = false } = {}) {
  const epoch = state.epoch;
  const sequence = ++state.workSequence;
  const selected = element('mail-work-item').value;
  state.loadingWork = true;
  updateControls();
  try {
    const result = await request('/api/work-items');
    if (!dialog.open || epoch !== state.epoch || sequence !== state.workSequence) return;
    state.workItems = result.items;
    const select = element('mail-work-item');
    select.replaceChildren(node('option', '업무를 선택해 주세요'));
    select.firstChild.value = '';
    for (const item of state.workItems) {
      const option = node('option', `${item.title} · 버전 ${item.version}`);
      option.value = item.id;
      select.append(option);
    }
    select.value = state.workItems.some((item) => item.id === selected) ? selected : '';
    state.conflicted = false;
    renderWorkContext();
    if (review) {
      showError();
      showLinkResult('최신 업무 목록을 불러왔습니다. 선택한 업무와 버전을 확인한 후 연결해 주세요.');
    }
  } catch (error) {
    if (!dialog.open || epoch !== state.epoch || sequence !== state.workSequence) return;
    state.conflicted = true;
    showError(error.message);
  } finally {
    if (epoch === state.epoch && sequence === state.workSequence) {
      state.loadingWork = false;
      updateControls();
    }
  }
}

async function loadDetail(id, revision) {
  if (state.linking) return;
  const epoch = state.epoch;
  const sequence = ++state.detailSequence;
  const changedMail = state.selected !== id;
  state.selected = id;
  state.detail = null;
  state.loadingDetail = true;
  element('mail-detail').hidden = true;
  showError();
  showLinkResult();
  if (changedMail) {
    element('mail-link-reason').value = '';
    element('mail-work-item').value = '';
  }
  updateControls();
  const path = `/api/mail/${encodeURIComponent(id)}${revision === undefined ? '' : `/revisions/${revision}`}`;
  try {
    const result = await request(path);
    if (!dialog.open || epoch !== state.epoch || sequence !== state.detailSequence) return;
    renderDetail(result.mail);
    if (changedMail || !state.workItems.length) await loadWorkItems();
  } catch (error) {
    if (!dialog.open || epoch !== state.epoch || sequence !== state.detailSequence) return;
    showError(error.message);
  } finally {
    if (epoch === state.epoch && sequence === state.detailSequence) {
      state.loadingDetail = false;
      updateControls();
    }
  }
}

element('mail-open').addEventListener('click', () => {
  state.epoch += 1;
  if (!element('mail-link-actor').value) element('mail-link-actor').value = element('outlook-import-actor').value || element('edit-actor').value || element('create-actor').value;
  state.query = element('mail-query').value.trim();
  dialog.showModal();
  search();
});

// The work-item evidence panel can open the exact saved revision without
// refreshing or replacing any unsaved work-item edits underneath this dialog.
window.addEventListener('ariadne:open-mail', async (event) => {
  const { mailId, revision } = event.detail || {};
  if (typeof mailId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(mailId) || !Number.isSafeInteger(revision) || revision < 1) return;
  if (dialog.open) {
    if (!state.linking) showError('현재 메일 근거함의 작업을 마치고 닫은 후 다시 열어 주세요. 입력 중인 연결 내용은 유지했습니다.');
    return;
  }
  state.epoch += 1;
  const epoch = state.epoch;
  if (!element('mail-link-actor').value) element('mail-link-actor').value = element('outlook-import-actor').value || element('edit-actor').value || element('create-actor').value;
  state.query = '';
  element('mail-query').value = '';
  dialog.showModal();
  const searchSequence = state.searchSequence + 1;
  await search();
  if (dialog.open && epoch === state.epoch && searchSequence === state.searchSequence && !state.selected) await loadDetail(mailId, revision);
});

function close() {
  if (!state.linking) dialog.close();
}
element('mail-close').addEventListener('click', close);
dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
dialog.addEventListener('close', () => {
  state.epoch += 1;
  state.searchSequence += 1;
  state.searching = false;
  clearDetail();
  element('mail-list').replaceChildren();
  showError();
});
element('mail-search-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (state.linking) return;
  state.query = element('mail-query').value.trim();
  search();
});
element('mail-prev').addEventListener('click', () => { if (!element('mail-prev').disabled) search(Math.max(0, state.offset - state.limit)); });
element('mail-next').addEventListener('click', () => { if (!element('mail-next').disabled) search(state.offset + state.limit); });
element('mail-revision').addEventListener('change', () => { if (state.selected && !state.linking) loadDetail(state.selected, Number(element('mail-revision').value)); });
element('mail-work-refresh').addEventListener('click', () => { if (!element('mail-work-refresh').disabled) loadWorkItems({ review: true }); });
element('mail-work-item').addEventListener('change', () => { renderWorkContext(); updateControls(); });
for (const id of ['mail-link-actor', 'mail-link-reason']) element(id).addEventListener('input', updateControls);

element('mail-link-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (element('mail-link-submit').disabled) return;
  const epoch = state.epoch;
  const detail = state.detail;
  const work = selectedWork();
  state.linking = true;
  showError();
  showLinkResult();
  updateControls();
  try {
    const result = await request(`/api/mail/${encodeURIComponent(detail.id)}/link`, 'POST', {
      revision: detail.revision,
      workItemId: work.id,
      expectedVersion: work.version,
      actorId: element('mail-link-actor').value.trim(),
      reason: element('mail-link-reason').value.trim()
    });
    if (!dialog.open || epoch !== state.epoch) return;
    const index = state.workItems.findIndex((item) => item.id === result.item.id);
    if (index >= 0) state.workItems[index] = result.item;
    const option = [...element('mail-work-item').options].find((item) => item.value === result.item.id);
    if (option) option.textContent = `${result.item.title} · 버전 ${result.item.version}`;
    renderWorkContext();
    const links = detail.linkedWorkItems.filter((item) => item.id !== result.item.id);
    links.push({ id: result.item.id, title: result.item.title, version: result.item.version });
    renderDetail({ ...detail, linkedWorkItems: links });
    showLinkResult(`${result.alreadyLinked ? '이미 연결된 근거입니다.' : '선택한 버전을 업무 근거로 연결했습니다.'} 업무 화면에서는 새로고침 후 확인해 주세요.`);
  } catch (error) {
    if (!dialog.open || epoch !== state.epoch) return;
    if (error.status === 409) state.conflicted = true;
    showError(error.message + ' 연결은 자동으로 재시도하지 않습니다.');
  } finally {
    if (epoch === state.epoch) {
      state.linking = false;
      updateControls();
    }
  }
});
window.addEventListener('beforeunload', (event) => {
  if (state.linking) {
    event.preventDefault();
    event.returnValue = '';
  }
});
