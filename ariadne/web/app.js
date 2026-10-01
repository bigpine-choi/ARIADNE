const $ = (id) => document.getElementById(id);
const state = {
  items: [],
  funds: [],
  statuses: [],
  selected: null,
  detail: null,
  evidence: [],
  audit: [],
  proposals: [],
  dirty: false,
  busy: false,
  loadToken: 0,
  editRevision: 0,
  create: null,
  ready: false
};
const fieldLabels = {
  title: '업무명',
  fundId: '펀드',
  ownerId: '담당자',
  status: '상태',
  waitingReason: '대기 사유',
  due: '기한',
  completionConditions: '완료조건',
  completionEvidence: '완료 근거',
  reviewRequired: '검토 필요',
  confidence: '신뢰도',
  sources: '연결 근거',
  assetId: '자산'
};
const roleLabels = {
  context: '참고 자료',
  request: '요청',
  email_sent: '메일 발송',
  attachment_received: '첨부 수령',
  completion: '완료 증빙',
  review_completion: '완료 검토'
};
const finalityLabels = {
  draft: '초안',
  signed: '날인본',
  final: '최종본',
  unknown: '최종 여부 미확인'
};
const providerLabels = {
  manual: '수기 입력',
  outlook: 'Outlook',
  document: '문서',
  meeting: '회의록',
  synthetic: '가상 자료'
};
const eventLabels = {
  work_item_created: '업무 등록',
  work_item_updated: '업무 수정',
  proposal_created: '수정 제안 등록',
  proposal_resolved: '수정 제안 검토',
  activity_recorded: '활동 기록'
};
const confidenceLabels = {
  high: '높음',
  medium: '보통',
  low: '낮음'
};
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function stableJson(value) {
  if (Array.isArray(value)) return '[' + value.map(stableJson).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + stableJson(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
function lockEditor(locked) {
  for (const field of $('edit-form').querySelectorAll('input,select,textarea,button')) field.disabled = locked;
}
function lockCreate(locked) {
  for (const field of $('create-form').querySelectorAll('input,select,textarea,button')) field.disabled = locked;
}
function notice(text, error = false) {
  $('notice').textContent = text;
  $('notice').className = error ? 'notice error' : 'notice';
  $('notice').hidden = !text;
}
function formError(id, text) {
  $(id).textContent = text;
  $(id).hidden = !text;
}
function fundName(id) {
  return state.funds.find((f) => f.id === id)?.name || id || '펀드 미지정';
}
function statusLabel(id) {
  return state.statuses.find((s) => s.id === id)?.label || id;
}
function badge(text, kind = '') {
  return node('span', text, `badge ${kind}`);
}
function statusKind(id) {
  return id.startsWith('waiting') ? 'waiting' : id === 'completed' ? 'done' : ['needs_review', 'overdue'].includes(id) ? 'review' : ['in_progress', 'reopened'].includes(id) ? 'progress' : '';
}
function dateTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul',
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(date);
}
function dueLabel(due) {
  if (due.kind === 'date') return due.date;
  if (due.kind === 'window') return `${due.startDate} ~ ${due.endDate}`;
  return due.rawText || ({
    unknown: '기한 미정',
    event: '선행 업무 이후',
    recurrence: '반복 일정'
  }
  [due.kind] || '확인 필요');
}
async function api(path, options = {
}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers
      },
      cache: 'no-store'
    });
  } catch {
    throw new Error('서버에 연결할 수 없습니다. 입력 내용은 유지됩니다. 연결 상태를 확인한 후 다시 시도해 주세요.');
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || '요청을 처리하지 못했습니다.');
    error.status = response.status;
    throw error;
  }
  return data;
}
function userError(error) {
  if (error.status === 409) return '다른 변경사항이 먼저 저장되었거나 같은 식별자가 이미 존재합니다. 현재 입력을 확인한 후 최신 내용을 불러와 주세요.';
  if (error.status === 400) return `입력값을 확인해 주세요. ${error.message}`;
  return error.message;
}
function setOptions(id, options, first) {
  const el = $(id),
  previous = el.value;
  el.replaceChildren();
  if (first) el.add(new Option(first, ''));
  for (const option of options) el.add(new Option(option.label, option.id));
  el.value = [...el.options].some((o) => o.value === previous) ? previous : '';
}
function filteredItems() {
  const q = $('search').value.trim().toLocaleLowerCase();
  return state.items.filter((i) => (!$('fund-filter').value || i.fundId === $('fund-filter').value) && (!$('status-filter').value || i.status === $('status-filter').value) && (!q || [i.title, i.ownerId, fundName(i.fundId)].filter(Boolean).join(' ').toLocaleLowerCase().includes(q)));
}
function renderList() {
  const items = filteredItems();
  $('result-count').textContent = `${items.length} / ${state.items.length}`;
  const list = $('work-list');
  list.replaceChildren();
  $('count-all').textContent = state.items.length;
  $('count-active').textContent = state.items.filter((i) => !['completed', 'cancelled'].includes(i.status)).length;
  $('count-waiting').textContent = state.items.filter((i) => ['waiting_external', 'waiting_internal'].includes(i.status)).length;
  $('count-review').textContent = state.items.filter((i) => i.reviewRequired || i.status === 'needs_review' || i.dueRisk === 'overdue').length;
  if (!items.length) {
    const empty = node('div', undefined, 'empty');
    empty.append(node('p', state.items.length ? '검색 조건에 맞는 업무가 없습니다.' : '아직 등록된 업무가 없습니다.\n첫 업무와 근거를 추가해 보세요.'));
    const button = node('button', state.items.length ? '필터 초기화' : '＋ 첫 업무 추가', 'button secondary');
    button.type = 'button';
    button.addEventListener('click', () => {
      if (state.items.length) {
        $('search').value = '';
        $('fund-filter').value = '';
        $('status-filter').value = '';
        renderList();
      } else openCreate();
    });
    empty.append(button);
    list.append(empty);
  }
  for (const item of items) {
    const button = node('button', undefined, `work-card${item.id === state.selected ? ' selected' : ''}`);
    button.type = 'button';
    button.dataset.workId = item.id;
    button.setAttribute('aria-pressed', String(item.id === state.selected));
    const top = node('div', undefined, 'card-top');
    top.append(node('span', fundName(item.fundId), 'card-fund'), badge(statusLabel(item.status), statusKind(item.status)));
    const bottom = node('div', undefined, 'card-bottom');
    bottom.append(node('span', item.ownerId || '담당자 미정'), node('span', `${dueLabel(item.due)}${item.due.confirmed ? '' : ' · 미확정'}`, item.dueRisk === 'overdue' ? 'risk' : ''));
    button.append(top, node('div', item.title, 'card-title'), bottom);
    button.addEventListener('click', () => selectItem(item.id));
    list.append(button);
  }
  $('filtered-banner').hidden = !state.selected || items.some((i) => i.id === state.selected);
}
function editSnapshot() {
  return {
    title: $('edit-title').value,
    ownerId: $('edit-owner').value,
    status: $('edit-status').value,
    waitingReason: $('edit-waiting').value,
    actorId: $('edit-actor').value,
    reason: $('edit-reason').value
  };
}
function updateDirty() {
  const current = editSnapshot(),
  item = state.detail;
  state.dirty = !!item && (current.title !== item.title || current.ownerId !== (item.ownerId || '') || current.status !== item.status || current.waitingReason !== (item.waitingReason || '') || current.reason !== '');
  $('dirty-indicator').textContent = state.dirty ? '저장되지 않은 변경사항' : '저장된 내용';
}
function canLeave() {
  if (state.busy) return false;
  return !state.dirty || window.confirm('저장하지 않은 입력을 버리고 다른 업무로 이동할까요?');
}
function switchTab(name) {
  for (const button of document.querySelectorAll('[data-tab]')) {
    const active = button.dataset.tab === name;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    $(`panel-${button.dataset.tab}`).hidden = !active;
  }
}
async function loadDetails(id, {
  resetDraft = true
}
= {
}) {
  const token = ++state.loadToken,
  editRevision = state.editRevision;
  try {
    const [detail, evidence, audit, proposals] = await Promise.all([api(`/api/work-items/${encodeURIComponent(id)}`), api('/api/evidence'), api(`/api/work-items/${encodeURIComponent(id)}/audit`), api(`/api/work-items/${encodeURIComponent(id)}/proposals`)]);
    if (token !== state.loadToken) return false;
    // A slow read must never replace input entered after the request began.
    if (state.editRevision !== editRevision && state.dirty) {
      notice('불러오는 동안 입력한 변경사항을 유지했습니다. 저장 후 다시 새로고침해 주세요.');
      return false;
    }
    state.detail = detail.item;
    state.selected = id;
    state.evidence = evidence.items;
    state.audit = audit.entries;
    state.proposals = proposals.items;
    const position = state.items.findIndex((i) => i.id === id);
    if (position !== -1) state.items[position] = detail.item;
    renderDetail(resetDraft);
    renderList();
    return true;
  } catch (error) {
    if (token === state.loadToken) notice(userError(error), true);
    return false;
  }
}
async function selectItem(id) {
  if (id === state.selected || !canLeave()) return;
  if (await loadDetails(id)) {
    state.dirty = false;
    $('retained-draft').hidden = true;
    switchTab('overview');
  }
}
function renderDetail(resetDraft = true) {
  const item = state.detail;
  if (!item) return;
  $('detail-empty').hidden = true;
  $('detail-content').hidden = false;
  $('detail-fund').textContent = fundName(item.fundId);
  $('detail-version').textContent = `버전 ${item.version}`;
  $('detail-title').textContent = item.title;
  const badges = $('detail-badges');
  badges.replaceChildren(badge(statusLabel(item.status), statusKind(item.status)));
  if (item.reviewRequired) badges.append(badge('검토 필요', 'review'));
  if (item.dueRisk === 'overdue') badges.append(badge('확정 기한 초과', 'review'));
  if (item.id.startsWith('synthetic-')) badges.append(badge('가상 시연 데이터', 'neutral'));
  $('detail-due').textContent = dueLabel(item.due);
  $('detail-due-note').textContent = `${item.due.confirmed ? '근거에 따라 확정된 기한' : '확인 필요 · 확정되지 않은 기한'}${item.due.rawText && item.due.rawText !== dueLabel(item.due) ? ` / ${item.due.rawText}` : ''}`;
  $('detail-owner').textContent = item.ownerId || '담당자 미정';
  $('detail-confidence').textContent = `추출 신뢰도: ${confidenceLabels[item.confidence] || item.confidence}`;
  $('conditions').replaceChildren();
  for (const condition of item.completionConditions) {
    const covered = item.completionEvidence.some((e) => e.conditionId === condition.id);
    const li = node('li');
    li.append(node('span', covered ? '✓' : '○', 'condition-state'), node('span', condition.label), node('span', covered ? ' · 검토 근거 연결됨' : ' · 완료 근거 미확인', 'small-muted'));
    $('conditions').append(li);
  }
  if (!item.completionConditions.length) $('conditions').append(node('li', '완료조건이 등록되지 않았습니다.'));
  $('confirmed-fields').textContent = item.confirmedFields.map((f) => fieldLabels[f] || f).join(' · ') || '사용자가 확정한 항목이 없습니다.';
  if (resetDraft) {
    $('edit-title').value = item.title;
    $('edit-owner').value = item.ownerId || '';
    $('edit-status').value = item.status;
    $('edit-waiting').value = item.waitingReason || '';
    $('edit-reason').value = '';
    formError('edit-error', '');
    $('conflict-reload').hidden = true;
    state.dirty = false;
    updateDirty();
  }
  renderEvidence();
  renderAudit();
  renderProposals();
}
function renderEvidence() {
  const ids = new Set(state.detail.sources);
  const container = $('evidence-list');
  container.replaceChildren();
  $('evidence-count').textContent = ids.size;
  if (!ids.size) container.append(node('p', '연결된 근거가 없습니다. 완료나 기한을 판단하기 전에 자료를 확인해 주세요.', 'empty'));
  for (const id of ids) {
    const evidence = state.evidence.find((e) => e.id === id);
    const record = node('section', undefined, 'record');
    if (!evidence) {
      record.append(node('p', '근거를 찾을 수 없습니다. 새로고침 후 다시 확인해 주세요.'));
      container.append(record);
      continue;
    }
    record.append(node('h3', `${providerLabels[evidence.provider] || evidence.provider} · ${roleLabels[evidence.role] || evidence.role}`), badge(finalityLabels[evidence.finality] || evidence.finality, 'neutral'), node('p', evidence.evidence));
    const dl = node('dl');
    for (const [label, value] of [['근거 위치', evidence.locator], ['원본 식별자', evidence.sourceId], ['수집 시각', dateTime(evidence.capturedAt)], ...(evidence.page ? [['페이지', String(evidence.page)]] : []), ...(evidence.clause ? [['조항', evidence.clause]] : [])]) dl.append(node('dt', label), node('dd', value));
    record.append(dl);
    const savedMail = evidence.provider === 'outlook' && /^ariadne:\/\/mail\/([0-9a-f-]{36})\/revisions\/([1-9][0-9]{0,8})$/.exec(evidence.locator);
    if (savedMail) {
      const openMail = node('button', '저장한 메일 보기', 'button secondary');
      openMail.type = 'button';
      openMail.addEventListener('click', () => window.dispatchEvent(new CustomEvent('ariadne:open-mail', {
        detail: { mailId: savedMail[1], revision: Number(savedMail[2]) }
      })));
      record.append(openMail);
    }
    container.append(record);
  }
}
function valueText(key, value) {
  if (value === null || value === undefined || value === '') return '미지정';
  if (key === 'status') return statusLabel(value);
  if (key === 'fundId') return fundName(value);
  if (key === 'due' && typeof value === 'object') return `${dueLabel(value)} (${value.confirmed ? '확정' : '미확정'})`;
  if (key === 'confidence') return confidenceLabels[value] || value;
  if (typeof value === 'boolean') return value ? '예' : '아니오';
  if (Array.isArray(value)) return value.map((v) => typeof v === 'object' ? v.label || [v.conditionId, v.evidenceId, v.reviewedBy].filter(Boolean).join(' / ') || JSON.stringify(v) : v).join(', ') || '없음';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function diffText(before, after) {
  const fields = Object.keys(after || {
  }).filter((key) => fieldLabels[key] && JSON.stringify(before?.[key]) !== JSON.stringify(after[key]));
  return fields.map((key) => `${fieldLabels[key]}: ${before ? `${valueText(key, before[key])} → ` : ''}${valueText(key, after[key])}`).join('\n') || '검토 기록이 저장되었습니다.';
}
function renderAudit() {
  const container = $('audit-list');
  container.replaceChildren();
  if (!state.audit.length) container.append(node('p', '아직 변경 이력이 없습니다.', 'empty'));
  for (const entry of [...state.audit].reverse()) {
    const record = node('section', undefined, 'record');
    record.append(node('h3', eventLabels[entry.eventType] || '업무 활동'), node('p', `${dateTime(entry.timestamp)} · ${entry.actorId}`, 'record-meta'), node('p', entry.reason), node('div', diffText(entry.before, entry.after), 'diff'));
    container.append(record);
  }
}
function renderProposals() {
  const container = $('proposals-list');
  container.replaceChildren();
  $('proposal-count').textContent = state.proposals.filter((p) => p.status === 'pending').length || '';
  if (!state.proposals.length) container.append(node('p', '검토할 수정 제안이 없습니다.\n현재 AI 자동 분석은 연결되지 않았습니다.', 'empty'));
  for (const proposal of [...state.proposals].reverse()) {
    const record = node('section', undefined, 'record');
    record.append(node('h3', ({
      pending: '검토 대기',
      accepted: '수락한 제안',
      rejected: '거절한 제안'
    }
    [proposal.status] || proposal.status)), node('p', proposal.reason), node('div', diffText(state.detail, proposal.patch), 'diff'), node('p', `제안 기준 버전 ${proposal.baseVersion} · 현재 버전 ${state.detail.version}`, 'record-meta'));
    if (proposal.evidenceIds?.length) record.append(node('p', `연결 근거: ${proposal.evidenceIds.join(', ')}`, 'record-meta'));
    if (proposal.status === 'pending') {
      const form = node('form'),
      row = node('div', undefined, 'form-row');
      const actorLabel = node('label', '검토자 이름'),
      actor = node('input');
      actor.required = true;
      actor.maxLength = 100;
      actorLabel.append(actor);
      const reasonLabel = node('label', '검토 사유'),
      reason = node('input');
      reason.required = true;
      reason.maxLength = 1000;
      reasonLabel.append(reason);
      row.append(actorLabel, reasonLabel);
      const actions = node('div', undefined, 'form-actions'),
      reject = node('button', '거절', 'button secondary'),
      accept = node('button', '수락하여 적용', 'button primary');
      reject.type = 'submit';
      reject.value = 'rejected';
      accept.type = 'submit';
      accept.value = 'accepted';
      if (proposal.baseVersion !== state.detail.version) {
        accept.disabled = true;
        record.append(node('p', '이전 버전의 제안입니다. 최신 업무 기준으로 새 제안이 필요합니다.', 'help'));
      }
      const errorBox = node('div', undefined, 'form-error');
      errorBox.hidden = true;
      errorBox.setAttribute('role', 'alert');
      actions.append(reject, accept);
      form.append(row, errorBox, actions);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (state.busy) return;
        if (state.dirty) {
          errorBox.textContent = '업무 상세의 미저장 변경사항을 먼저 저장하거나 취소해 주세요.';
          errorBox.hidden = false;
          return;
        }
        const decision = event.submitter?.value;
        if (decision === 'accepted' && ({
          ...state.detail,
          ...proposal.patch
        }).status === 'completed' && !reviewedCompletionAvailable({
          ...state.detail,
          ...proposal.patch
        })) {
          errorBox.textContent = '완료 제안을 적용하려면 모든 완료조건에 검토된 최종 완료 증빙이 연결되어야 합니다.';
          errorBox.hidden = false;
          return;
        }
        if (!decision || !window.confirm(decision === 'accepted' ? '제안 내용을 업무에 적용할까요? 변경 이력이 기록됩니다.' : '이 제안을 거절할까요?')) return;
        state.busy = true;
        lockEditor(true);
        accept.disabled = true;
        reject.disabled = true;
        try {
          await api(`/api/proposals/${encodeURIComponent(proposal.id)}/resolve`, {
            method: 'POST',
            body: JSON.stringify({
              decision,
              expectedVersion: state.detail.version,
              actorId: actor.value.trim(),
              reason: reason.value.trim()
            })
          });
          await loadDetails(state.selected);
          notice(decision === 'accepted' ? '제안을 적용하고 변경 이력을 기록했습니다.' : '제안을 거절했습니다.');
        } catch (error) {
          errorBox.textContent = userError(error);
          errorBox.hidden = false;
        } finally {
          state.busy = false;
          lockEditor(false);
          accept.disabled = proposal.baseVersion !== state.detail.version;
          reject.disabled = false;
        }
      });
      record.append(form);
    }
    container.append(record);
  }
}
function reviewedCompletionAvailable(item = state.detail) {
  return item.completionConditions.length > 0 && item.completionConditions.every((condition) => item.completionEvidence.some((entry) => entry.conditionId === condition.id && entry.reviewedBy && entry.reviewedAt && state.evidence.some((e) => e.id === entry.evidenceId && ['completion', 'review_completion'].includes(e.role) && ['signed', 'final'].includes(e.finality))));
}
async function saveTask(event) {
  event.preventDefault();
  if (state.busy || !state.detail) return;
  formError('edit-error', '');
  const draft = editSnapshot();
  if (draft.status === 'completed' && !reviewedCompletionAvailable()) {
    formError('edit-error', '완료로 변경할 수 없습니다. 모든 완료조건에 최종본 또는 날인본인 완료 증빙과 검토 이력이 연결되어야 합니다. 현재 화면에서는 완료 증빙의 등록·검토를 지원하지 않습니다.');
    return;
  }
  const patch = {
  };
  for (const key of ['title', 'ownerId', 'status', 'waitingReason']) {
    const value = key === 'ownerId' ? draft[key].trim() || null : draft[key].trim();
    if (value !== (state.detail[key] ?? (key === 'ownerId' ? null : ''))) patch[key] = value;
  }
  if (!Object.keys(patch).length) {
    formError('edit-error', '변경된 업무 내용이 없습니다.');
    return;
  }
  state.busy = true;
  lockEditor(true);
  try {
    const result = await api(`/api/work-items/${encodeURIComponent(state.selected)}`, {
      method: 'PATCH',
      body: JSON.stringify({
        patch,
        expectedVersion: state.detail.version,
        actorId: draft.actorId.trim(),
        reason: draft.reason.trim(),
        evidenceIds: []
      })
    });
    state.detail = result.item;
    state.dirty = false;
    $('retained-draft').hidden = true;
    renderDetail();
    const position = state.items.findIndex((item) => item.id === result.item.id);
    if (position !== -1) state.items[position] = result.item;
    renderList();
    if (await loadDetails(state.selected)) notice('변경사항과 사유를 저장했습니다.');
    else notice('변경사항은 저장됐지만 최신 근거와 이력을 불러오지 못했습니다. 새로고침해 주세요.', true);
  } catch (error) {
    formError('edit-error', userError(error));
    if (error.status === 409) $('conflict-reload').hidden = false;
  } finally {
    state.busy = false;
    lockEditor(false);
  }
}
async function refresh() {
  if (state.busy) return;
  $('refresh').disabled = true;
  $('sync-state').textContent = '불러오는 중';
  try {
    const [items, funds, statuses] = await Promise.all([api('/api/work-items'), api('/api/funds'), api('/api/statuses')]);
    state.items = items.items;
    state.funds = funds.items;
    state.statuses = statuses.items;
    setOptions('fund-filter', state.funds.map((f) => ({
      id: f.id,
      label: f.name
    })), '모든 펀드');
    setOptions('status-filter', state.statuses, '모든 상태');
    if (!state.ready) setOptions('edit-status', state.statuses);
    setOptions('create-fund', state.funds.map((f) => ({
      id: f.id,
      label: f.name
    })), '미지정');
    state.ready = true;
    renderList();
    $('sync-state').textContent = '불러오기 완료';
    if (state.dirty) notice('업무 목록을 새로고침했습니다. 수정 중인 상세 내용과 기준 버전은 유지됩니다.');
    else if (state.selected) await loadDetails(state.selected);
    else if (state.items.length) await selectItem(state.items[0].id);
  } catch (error) {
    notice(userError(error), true);
    $('sync-state').textContent = '연결 확인 필요';
    if (!state.ready) {
      const empty = node('p', '업무를 불러오지 못했습니다. 서버가 실행 중인지 확인한 후 새로고침해 주세요.', 'empty');
      $('work-list').replaceChildren(empty);
    }
  } finally {
    $('refresh').disabled = false;
  }
}
function openCreate() {
  if (state.busy) return;
  if (!state.ready) {
    notice('서버 연결 후 업무를 추가할 수 있습니다. 먼저 새로고침해 주세요.', true);
    return;
  }
  if (!state.create) {
    $('create-form').reset();
    state.create = {
      taskId: `manual-task-${crypto.randomUUID()}`,
      evidenceId: `manual-evidence-${crypto.randomUUID()}`,
      capturedAt: new Date().toISOString(),
      evidence: null,
      evidenceCreated: false
    };
    $('create-source').readOnly = false;
    $('create-locator').readOnly = false;
    $('create-partial').hidden = true;
    formError('create-error', '');
  }
  $('create-dialog').showModal();
  $('create-task-title').focus();
}
function closeCreate() {
  if (state.busy) return;
  const filled = [...$('create-form').querySelectorAll('input,textarea')].some((el) => el.value);
  if (filled && !window.confirm('업무 등록을 닫을까요? 입력 내용은 다시 열었을 때 유지됩니다.')) return;
  $('create-dialog').close();
}
async function createTask(event) {
  event.preventDefault();
  if (state.busy) return;
  const draft = state.create;
  const actor = $('create-actor').value.trim();
  formError('create-error', '');
  if (['create-task-title', 'create-condition', 'create-actor', 'create-source', 'create-locator'].some((id) => !$(id).value.trim())) {
    formError('create-error', '업무명, 완료조건, 작성자, 근거 내용과 위치를 공백 없이 입력해 주세요.');
    return;
  }
  state.busy = true;
  lockCreate(true);
  try {
    if (!draft.evidence) draft.evidence = {
      id: draft.evidenceId,
      provider: 'manual',
      sourceId: draft.evidenceId,
      capturedAt: draft.capturedAt,
      evidence: $('create-source').value.trim(),
      locator: $('create-locator').value.trim(),
      sha256: null,
      threadId: null,
      finality: 'unknown',
      role: 'request'
    };
    $('create-source').readOnly = true;
    $('create-locator').readOnly = true;
    $('create-partial').hidden = false;
    // The immutable payload and its identity survive uncertain network results.
    if (!draft.evidenceCreated) {
      const result = await api('/api/evidence', {
        method: 'POST',
        body: JSON.stringify({
          evidence: draft.evidence,
          actorId: actor
        })
      });
      draft.evidenceId = result.evidence.id;
      draft.evidenceCreated = true;
    }
    const date = $('create-due').value || null;
    const item = {
      id: draft.taskId,
      title: $('create-task-title').value.trim(),
      fundId: $('create-fund').value || null,
      ownerId: $('create-owner').value.trim() || null,
      status: 'new',
      reviewRequired: true,
      confidence: 'low',
      due: {
        kind: date ? 'date' : 'unknown',
        rawText: date ? `사용자 입력 예상 기한 ${date}` : '기한 미정',
        date,
        timezone: 'Asia/Seoul',
        anchorTaskId: null,
        confirmed: false,
        evidenceIds: date ? [draft.evidenceId] : []
      },
      sources: [draft.evidenceId],
      completionConditions: [{
        id: `${draft.taskId}-condition`,
        label: $('create-condition').value.trim()
      }],
      completionEvidence: [],
      waitingReason: ''
    };
    try {
      await api('/api/work-items', {
        method: 'POST',
        body: JSON.stringify({
          item,
          actorId: actor,
          reason: '확인한 요청과 근거를 수기로 등록'
        })
      });
    } catch (error) {
      if (error.status !== 409) throw error;
      const existing = (await api(`/api/work-items/${encodeURIComponent(item.id)}`)).item;
      if (!Object.keys(item).every((key) => stableJson(item[key]) === stableJson(existing[key]))) throw error;
    }
    $('create-dialog').close();
    state.create = null;
    notice('새 업무를 등록했습니다. 입력한 기한은 확인이 필요한 상태로 저장했습니다.');
    state.busy = false;
    const previousDirty = state.dirty;
    await refresh();
    if (!previousDirty) await selectItem(item.id);
  } catch (error) {
    formError('create-error', `${userError(error)} 등록을 다시 시도해도 같은 근거와 업무를 사용합니다.`);
  } finally {
    state.busy = false;
    lockCreate(false);
  }
}
$('edit-form').addEventListener('submit', saveTask);
for (const eventName of ['input', 'change']) $('edit-form').addEventListener(eventName, () => {
  state.editRevision += 1;
  updateDirty();
});
$('conflict-reload').addEventListener('click', async () => {
  if (state.busy) return;
  const draft = editSnapshot();
  $('retained-text').value = Object.entries(draft).map(([key, value]) => `${fieldLabels[key] || ({ actorId: '변경자', reason: '변경 사유' }[key])}: ${valueText(key, value)}`).join('\n');
  $('retained-draft').hidden = false;
  $('retained-draft').open = true;
  if (await loadDetails(state.selected)) notice('최신 내용을 불러왔습니다. 기존 입력은 아래에 보관했습니다. 비교한 후 필요한 내용을 다시 입력해 주세요.');
});
$('refresh').addEventListener('click', refresh);
$('new-task').addEventListener('click', openCreate);
for (const id of ['search', 'fund-filter', 'status-filter']) $(id).addEventListener(id === 'search' ? 'input' : 'change', renderList);
for (const button of document.querySelectorAll('[data-tab]')) {
  button.addEventListener('click', () => switchTab(button.dataset.tab));
  button.addEventListener('keydown', (event) => {
    const tabs = [...document.querySelectorAll('[data-tab]')],
    index = tabs.indexOf(button);
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next !== undefined) {
      event.preventDefault();
      switchTab(tabs[next].dataset.tab);
      tabs[next].focus();
    }
  });
}
$('create-form').addEventListener('submit', createTask);
$('create-close').addEventListener('click', closeCreate);
$('create-cancel').addEventListener('click', closeCreate);
$('create-dialog').addEventListener('cancel', (event) => {
  event.preventDefault();
  closeCreate();
});
let connectionState = null, connectionBusy = false, connectionLoadToken = 0;
function renderConnection(value) {
  connectionState = value;
  $('settings-status').textContent = value.configured ? '등록됨 · API 연결 검증 전' : value.supported ? '등록된 API 키가 없습니다.' : '이 실행 환경에서는 보안 저장을 지원하지 않습니다.';
  $('settings-message').textContent = value.message || '';
  $('settings-key').disabled = !value.supported;
  $('settings-save').disabled = !value.supported;
  $('settings-save').textContent = value.configured ? 'API 키 교체 저장' : 'API 키 저장';
  $('settings-remove').hidden = !value.configured || !value.supported;
}
async function openSettings() {
  const token = ++connectionLoadToken;
  connectionState = null;
  $('settings-key').value = '';
  $('settings-key').disabled = true;
  $('settings-remove').hidden = true;
  formError('settings-error', '');
  $('settings-status').textContent = '등록 상태를 확인하고 있습니다.';
  $('settings-message').textContent = '';
  $('settings-dialog').showModal();
  $('settings-save').disabled = true;
  try {
    const status = await api('/api/connections/openai');
    if (token === connectionLoadToken && $('settings-dialog').open) renderConnection(status);
  } catch {
    if (token !== connectionLoadToken || !$('settings-dialog').open) return;
    $('settings-status').textContent = '등록 상태를 확인하지 못했습니다.';
    formError('settings-error', '서버 연결을 확인한 후 연결 설정을 다시 열어 주세요.');
  }
}
function closeSettings() {
  if (connectionBusy) return;
  connectionLoadToken += 1;
  $('settings-key').value = '';
  $('settings-dialog').close();
}
$('settings-open').addEventListener('click', openSettings);
$('settings-close').addEventListener('click', closeSettings);
$('settings-dialog').addEventListener('cancel', (event) => {
  event.preventDefault();
  closeSettings();
});
$('settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (connectionBusy || !connectionState?.supported) return;
  const apiKey = $('settings-key').value.trim();
  $('settings-key').value = '';
  if (!/^[\x21-\x7e]{16,2560}$/.test(apiKey)) {
    formError('settings-error', 'API 키는 공백 없는 ASCII 문자 16~2560자로 입력해 주세요.');
    return;
  }
  connectionBusy = true;
  $('settings-key').disabled = true;
  $('settings-save').disabled = true;
  $('settings-remove').disabled = true;
  formError('settings-error', '');
  try {
    const status = await api('/api/connections/openai', {
      method: 'POST',
      body: JSON.stringify({
        apiKey
      })
    });
    renderConnection(status);
    if (status.supported && status.configured) $('settings-message').textContent = 'API 키를 저장했습니다. 다음 실행부터 자동으로 불러옵니다.';
  } catch (error) {
    formError('settings-error', error.status === 400
      ? 'API 키 형식을 확인해 주세요. 공백 없는 ASCII 문자 16~2560자가 필요합니다.'
      : 'API 키 저장 결과를 확인하지 못했습니다. 입력한 키는 화면에서 지웠습니다. 연결 설정을 다시 열어 등록 상태를 확인해 주세요.');
  } finally {
    connectionBusy = false;
    $('settings-key').disabled = !connectionState?.supported;
    $('settings-save').disabled = !connectionState?.supported;
    $('settings-remove').disabled = false;
  }
});
$('settings-remove').addEventListener('click', async () => {
  if (connectionBusy || !window.confirm('등록된 OpenAI API 키를 삭제할까요? 다음 사용 시 다시 등록해야 합니다.')) return;
  connectionBusy = true;
  $('settings-key').value = '';
  $('settings-save').disabled = true;
  $('settings-remove').disabled = true;
  try {
    renderConnection(await api('/api/connections/openai', {
      method: 'DELETE'
    }));
    formError('settings-error', '');
  } catch {
    formError('settings-error', '등록된 키를 삭제하지 못했습니다. 연결 설정을 다시 열어 현재 상태를 확인해 주세요.');
  } finally {
    connectionBusy = false;
    $('settings-save').disabled = !connectionState?.supported;
    $('settings-remove').disabled = false;
  }
});
window.addEventListener('beforeunload', (event) => {
  if (state.dirty || state.busy || connectionBusy || (state.create && [...$('create-form').querySelectorAll('input,textarea')].some((el) => el.value))) {
    event.preventDefault();
    event.returnValue = '';
  }
});
refresh();
