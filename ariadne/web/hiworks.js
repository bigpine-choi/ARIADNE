'use strict';
(() => {
  const el=id=>document.getElementById(id);
  let activeDraft=null, downloadUrl=null;
  async function api(path, body) {
    const r=await fetch(path, body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await r.json(); if(!r.ok) throw new Error(data.error?.message||'요청에 실패했습니다.'); return data;
  }
  function showKey(status) {
    el('key-status').textContent=status.saved?'키가 암호화 저장되어 있습니다. API 연결 확인은 아래 버튼으로 진행하세요.':status.environmentConfigured?'환경변수에 키가 있습니다. 한 번 등록하면 암호화 저장된 키를 우선 사용합니다.':'API 키 최초 등록이 필요합니다.';
    if(!status.persistentSupported) el('key-status').textContent+=' 이 OS에서는 환경변수를 사용하세요.';
    else if(status.storageReady===false) el('key-status').textContent+=' 현재 실행환경에서 키 암호화가 막혀 있습니다. 시제품 실행 파일을 Windows에서 직접 열어 다시 실행하세요.';
    el('key-save').disabled=!status.persistentSupported;
    el('key-remove').disabled=!status.saved;
    el('api-key').placeholder=status.saved?'키를 변경할 때만 입력':'최초 등록 시 입력';
  }
  el('key-form').addEventListener('submit',async event=>{
    event.preventDefault(); el('key-save').disabled=true;
    try {showKey(await api('/api/settings/openai',{apiKey:el('api-key').value}));}
    catch(error){el('key-status').textContent=error.message;}
    finally {el('api-key').value=''; el('key-save').disabled=false;}
    el('openai-check-status').textContent='키 저장 상태가 변경됐습니다. API 연결을 다시 확인하세요.';
  });
  el('key-remove').addEventListener('click',async()=>{
    if(!window.confirm('저장된 API 키를 삭제할까요? 다음 AI 실행에는 새 키 등록 또는 환경변수 설정이 필요합니다.')) return;
    try{showKey(await api('/api/settings/openai/remove',{}));}catch(error){el('key-status').textContent=error.message;}
    el('openai-check-status').textContent='API 연결 확인 전입니다.';
  });
  el('openai-model').addEventListener('input',()=>{el('openai-check-status').textContent='모델명이 변경됐습니다. 저장하고 API 연결을 확인하세요.';});
  el('openai-check').addEventListener('click',async()=>{
    const model=el('openai-model').value.trim();
    if(!model){el('openai-check-status').textContent='사용할 OpenAI 모델명을 입력하세요. 예: gpt-5-mini';return;}
    el('openai-check').disabled=true;
    el('openai-check-status').textContent='모델을 저장하고 OpenAI API 인증을 확인하고 있습니다.';
    try{
      const result=await api('/api/settings/openai/check',{model});
      el('openai-model').value=result.model||model;
      el('openai-check-status').textContent=result.message;
      window.dispatchEvent(new Event('ariadne-openai-model-saved'));
    }catch(error){el('openai-check-status').textContent=error.message;}
    finally{el('openai-check').disabled=false;}
  });
  async function listDrafts() {
    const result=await api('/api/hiworks/drafts'); const list=el('draft-list'); list.replaceChildren();
    if(!result.items.length){list.textContent='아직 보관된 초안이 없습니다.';return;}
    for(const item of result.items) {
      const button=document.createElement('button');button.textContent=(item.kind==='weekly'?'주간보고 · ':'품의서 · ')+item.title;
      button.addEventListener('click',async()=>{try{showDraft((await api('/api/hiworks/drafts/'+item.id)).draft);}catch(error){el('draft-status').textContent=error.message;}});
      const p=document.createElement('p'); p.append(button);list.append(p);
    }
  }
  function showDraft(draft) {
    activeDraft=draft;el('draft-preview').hidden=false;el('draft-title').textContent=draft.title;
    el('draft-warnings').textContent=draft.warnings.join(' · ');el('draft-text').value=draft.bodyText;
    // Only server-generated HTML is rendered. All user/source cells are escaped server-side.
    el('draft-body').innerHTML=draft.bodyHtml;
    if(downloadUrl) URL.revokeObjectURL(downloadUrl);
    const page='<!doctype html><html lang="ko"><meta charset="utf-8"><title>기안문 초안</title><body><h1></h1>'+draft.bodyHtml+'</body></html>';
    const documentCopy=new DOMParser().parseFromString(page,'text/html');documentCopy.querySelector('h1').textContent=draft.title;
    downloadUrl=URL.createObjectURL(new Blob(['<!doctype html>'+documentCopy.documentElement.outerHTML],{type:'text/html;charset=utf-8'}));
    el('draft-download').href=downloadUrl;el('copy-status').textContent='';
  }
  const lines=text=>text.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
  el('draft-form').addEventListener('submit',async event=>{
    event.preventDefault();el('draft-save').disabled=true;
    const form=new FormData(event.target); const evidenceIds=[form.get('evidence')];
    const draft={};for(const key of ['title','fund','asset','instructionDate','description','withdrawalAccount','depositAccount','stamp','documentBox','securityLevel','declaredTotal'])draft[key]=form.get(key);
    draft.retentionYears=Number(form.get('retentionYears'));draft.evidenceIds=evidenceIds;draft.approvalLine=[];
    draft.recipients=lines(form.get('recipients'));draft.attachments=lines(form.get('attachments')).map(name=>({name,evidenceIds}));
    draft.lines=[{label:form.get('lineLabel'),supply:form.get('supply'),vat:form.get('vat'),evidenceIds}];
    try{const result=await api('/api/hiworks/drafts',draft);await listDrafts();showDraft(result.draft);el('draft-status').textContent='초안을 보관했습니다. 본문과 금액·근거를 검토하세요.';}
    catch(error){el('draft-status').textContent=error.message;}
    finally{el('draft-save').disabled=false;}
  });
  el('draft-refresh').addEventListener('click',()=>listDrafts().catch(error=>{el('draft-status').textContent=error.message;}));
  for(const [id,type] of [['copy-title','title'],['copy-body','body']])el(id).addEventListener('click',async()=>{
    if(!activeDraft)return;
    try {
      if(type==='title') await navigator.clipboard.writeText(activeDraft.title);
      else if(window.ClipboardItem) await navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([activeDraft.bodyHtml],{type:'text/html'}),'text/plain':new Blob([activeDraft.bodyText],{type:'text/plain'})})]);
      else await navigator.clipboard.writeText(activeDraft.bodyText);
      el('copy-status').textContent='복사했습니다.';
    }catch(error){el('copy-status').textContent='복사가 허용되지 않았습니다. 본문 입력란에서 직접 복사하세요.';}
  });
  api('/api/settings/openai').then(showKey).catch(error=>{el('key-status').textContent=error.message;});
  function loadEvidence(){const chosen=el('draft-evidence').value;el('draft-evidence').replaceChildren(new Option('근거 선택',''));return api('/api/evidence').then(result=>{
    for(const proof of result.items){const option=document.createElement('option');option.value=proof.id;option.textContent=proof.id+' · '+proof.locator;el('draft-evidence').append(option);}
    el('draft-evidence').value=chosen;
    if(!result.items.length)el('draft-status').textContent='업무 자료 화면에서 보관 자료를 근거로 등록하세요.';
  }).catch(error=>{el('draft-status').textContent=error.message;});}
  loadEvidence();window.addEventListener('ariadne-evidence-changed',loadEvidence);
  listDrafts().catch(error=>{el('draft-status').textContent=error.message;});
})();
