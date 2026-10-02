'use strict';
(() => {
 let settings=null,offset=0,messages=[],pollTimer=null;
 const form=el('connection-form');
 const labels={running:'진행 중',completed:'완료',attention:'범위 확인 필요',failed:'실패',interrupted:'중단됨',partial:'일부 수집',deferred:'Teams 사용 중 · 오늘 생략'};
 const actions={mail:'메일 수집',teams:'Teams 수동 수집',hiworks:'결재 자료 수집',folder:'회의 파일 보관',collect:'메일·파일·결재 수집',weekly:'회의 자료 미리 생성',archive:'회의 원본 목록 보관',tick:'예약 작업'};
 function jobSummary(result){
  if(result.message)return result.message;
  if(result.output)return result.status==='submitted'?'엑셀 생성 · Outlook 발송 접수 (배달 확인 필요)':result.status==='delivery_unknown'?'발송 결과 확인 필요 · 자동 재시도하지 않음':'엑셀 생성 완료 · 미리 생성 파일 보관';
  const parts=[];
  for(const [key,title] of [['folder','회의 파일'],['mail','메일'],['teams','Teams'],['hiworks','결재 자료']]){
    const value=result[key];if(!value)continue;
    if(value.status==='not_configured')parts.push(title+' 연결 전');
    else if(value.status==='failed')parts.push(title+' 수집 실패');
    else parts.push(title+' '+(value.read??value.documents??value.files??0)+'건 확인');
  }
  if(result.documents!==undefined)parts.push('결재 문서 '+result.documents+'건 · 새 보관 버전 '+result.versions+'건');
  if(result.files!==undefined)parts.push('파일 '+result.files+'건 확인');
  if(result.read!==undefined)parts.push('자료 '+result.read+'건 · 새 보관 '+result.stored+'건');
  if(result.warnings?.length)parts.push('수집 범위 확인사항 '+result.warnings.length+'건');
  if(result.handoff?.message)parts.push(result.handoff.message);
  else if(result.handoff?.cloud?.verified)parts.push('회사 웹 폴더 도착 확인');
  else if(result.handoff?.status==='imported')parts.push('회사 자료 '+result.handoff.packets+'개 전달 파일 수신·DB 반영');
  if(result.packets!==undefined&&result.lastReceived)parts.push('회사 자료 '+result.packets+'개 전달 파일 수신·DB 반영');
  if(result.pending)parts.push('남은 전달 파일 '+result.pending+'개 · 다음 실행에서 계속 수신');
  return parts.join(' · ')||'실행 결과를 확인하는 중입니다.';
 }
 function sourceKind(m){return m.provider?.startsWith('outlook')?'outlook':m.provider?.startsWith('teams')?'teams':m.provider;}
 function transportView(company,hybrid,mode){
  const web=hybrid&&mode==='web';
  el('handoff-transport-label').hidden=!hybrid;el('handoff-web-label').hidden=!web;el('handoff-web-panel').hidden=!web;
  el('handoff-folder-label').hidden=!hybrid||web;
  el('handoff-web-help').textContent=company?'회사 OneDrive 앱 없이 지정한 웹 폴더에 암호화된 회의 자료를 올립니다. 연결할 때만 Edge 창이 열리고 예약 전달은 화면·키보드·마우스 조작 없이 실행됩니다.':'회사 OneDrive 앱 없이 같은 웹 폴더의 회의 자료를 받아 이 오멘의 DB에 반영합니다. 웹 폴더의 자료를 수정하거나 삭제하지 않습니다. 연결할 때만 Edge 창이 열리고 예약 수신은 화면·키보드·마우스 조작 없이 실행됩니다.';
 }
 function deviceView(data){
  const role=settings.device_role||'single',company=role==='company-relay',omen=role==='omen',hybrid=company||omen;
  el('device-panel').hidden=!hybrid;el('device-role').textContent=company?'회사 PC · 공유폴더 보관과 전달만 실행합니다.':'오멘 · 메일·Teams·결재 DB와 주간보고를 운영합니다.';
  el('openai-panel').hidden=company;el('browser-panel').hidden=company;el('handoff-folder-label').hidden=!hybrid;
  transportView(company,hybrid,settings.handoff_transport);
  el('handoff-pair-panel').hidden=!hybrid;el('handoff-sender').hidden=!company;el('handoff-key-form').hidden=!omen;
  el('schedule-register').hidden=!hybrid;
  el('task-status').textContent=data.schedule?.registered?(data.schedule.enabled?'이 PC의 Windows 예약이 등록되어 있습니다. 실행 시점 '+data.schedule.triggers+'개.':'이 PC의 Windows 예약이 중지되어 있습니다.'):'이 PC의 Windows 예약은 아직 등록되지 않았습니다.';
  form.querySelector('p:not([id])').hidden=company;
  form.elements.namedItem('collection_enabled').closest('label').lastChild.textContent=company?' 평일 09~18시 공유폴더 보관·오멘 전달':' 평일 09~18시 메일·회의 자료·결재 수집 (Teams는 낮 12시에만)';
  for(const name of ['mailbox','teams_chats','hiworks_company','model','report_template','hiworks_enabled','enabled','auto_send','allow_partial_report']){
   const field=form.elements.namedItem(name);field.closest('label').hidden=company;
  }
  form.elements.namedItem('weekly_archive_enabled').closest('label').hidden=omen;
  form.elements.namedItem('folder').readOnly=omen;
  const received=data.handoff?.lastReceived,published=data.handoff?.lastPublished;
  const web=hybrid&&settings.handoff_transport==='web',uploaded=data.cloud?.lastUploaded;
  el('handoff-status').textContent=company?(web?(uploaded?'회사 웹 업로드 확인 시각: '+uploaded.verified_at:'회사 웹 폴더에 자동 전달한 자료는 아직 없습니다.'):!settings.handoff_dir?'회사 OneDrive 연결 대기 · 공유폴더의 DB 보관은 계속됩니다.':published?'마지막 자료 전달: '+published.sequence+'번 · '+(published.status==='published'?'로컬 전달 폴더 저장 완료 · 클라우드 도착은 별도 확인':'전달 재시도 대기'):'아직 전달한 회의 자료가 없습니다.'):(received?'회사 원본 확인 시각: '+received.source_at+' · 오멘 수신 시각: '+received.received_at:'회사 회의 자료 수신 전입니다.');
  const cloud=data.cloud?.connection||{},cloudLabels={not_connected:'회사 웹 연결 전입니다.',opening:'전용 Edge 창을 여는 중입니다.',login_required:'전용 Edge 창에서 회사 계정으로 로그인하세요.',connected:'회사 웹 폴더 연결 확인: '+(cloud.checkedAt||'')};
  const cloudErrors={login_required:'회사 웹 연결을 다시 열어 로그인하세요.',permission_denied:company?'회사 웹 폴더의 읽기·업로드 권한을 확인하세요.':'회사 웹 폴더의 읽기 권한을 확인하세요.',browser_unavailable:'회사 브라우저를 열지 못했습니다. Edge 또는 회사에서 허용한 연동 방법을 확인하세요.',connection_busy:'연결 창이나 다른 전달이 사용 중입니다. 창을 닫으면 다음 예약 때 재시도합니다.',packet_missing:'이전 순번의 전달 파일을 복구해야 합니다.',remote_conflict:'같은 이름의 웹 파일이 원본과 달라 덮어쓰지 않고 중지했습니다.',packet_invalid:'전달 파일의 형식·크기를 확인하세요.',sender_conflict:'다른 발신 PC의 전달 파일이 섞여 있습니다. 기존 회사 PC 자료만 연결하세요.',packet_authentication:'자료 연결 코드 또는 전달 파일의 무결성을 확인하세요. 확인되지 않은 자료는 DB에 반영하지 않습니다.'};
  el('handoff-web-status').textContent=cloud.status==='attention'?(cloudErrors[cloud.code]||'회사 웹 연결에 실패했습니다. DB와 전달 파일은 유지됩니다.'):(cloudLabels[cloud.status]||cloudLabels.not_connected);
  el('handoff-key-status').textContent=data.handoff?.keySaved?'자료 연결 코드가 이 PC에 암호화 저장되어 있습니다.':'자료 연결 코드를 등록하세요.';
  for(const b of document.querySelectorAll('[data-collect]')){
   b.hidden=company&&!['folder','archive','collect'].includes(b.dataset.collect)||omen&&b.dataset.collect==='archive';
   if(b.dataset.collect==='folder')b.textContent=omen?'회사 회의 자료 받기':'변경 파일 저장';
   if(b.dataset.collect==='collect')b.textContent=company?'공유폴더 보관·오멘 전달':'메일·회의 자료·결재 수집 (Teams 제외)';
  }
 }
 function errorMessage(error){for(const id of ['notice','approval-status','connection-status'])el(id).textContent=error.message;}
 function detail(row,target){const host=el(target);host.hidden=false;host.replaceChildren();const h=document.createElement('h3');h.textContent=row.subject||row.message.chat||'보관 자료';const p=document.createElement('p');p.textContent='버전 '+row.version+' · 원본 시각 '+(row.timestamp||'확인 필요')+' · 보관 시각 '+row.observedAt;const pre=document.createElement('div');pre.className='source-body';pre.textContent=row.body;host.append(h,p,pre);
  if(row.provider==='hiworks'){const info=document.createElement('p');info.textContent='결재 상태: '+(row.message.source_status==='completion_time_present'?'완료 시각 표시 있음 (실제 집행 완료는 별도 확인)':'완료 여부 확인 필요');host.append(info);const origin=row.message.source_url;if(origin?.startsWith('https://approval.office.hiworks.com/')){const a=link('하이웍스 원본 열람',origin);a.target='_blank';a.rel='noopener noreferrer';host.append(a);}for(const section of ['metadata','approvals','history','attachments']){const block=document.createElement('div');block.className='source-body';const titles={metadata:'문서 정보',approvals:'결재선',history:'처리 이력',attachments:'첨부 목록'}; const value=row.message[section]||[]; const text=section==='metadata'?Object.entries(value).map(([k,v])=>k+': '+v).join('\n'):section==='approvals'?value.map(v=>v.text+'\n'+v.marks.map(m=>m.label+' '+m.at).join(' / ')).join('\n\n'):section==='attachments'?value.map(v=>v.name+'\n'+v.url).join('\n\n'):value.join('\n\n');block.textContent=titles[section]+'\n'+(text||'확인된 자료 없음');host.append(block);}}
  host.append(button('업무·보고 초안의 근거로 등록',async()=>{try{await post('/api/workspace/evidence',{versionId:row.id});await loadEvidence();window.dispatchEvent(new Event('ariadne-evidence-changed'));el('notice').textContent='근거를 등록했습니다. 업무 홈과 보관 초안에서 이어서 사용할 수 있습니다.';host.append(link('업무 등록으로 이동','#tasks'));}catch(e){errorMessage(e);}}));
 }
 async function loadMessages(){const data=await request('/api/workspace/messages/'+(el('message-filter').value==='all'?'':el('message-filter').value+'/')+offset);messages=data.items;const filter=el('message-filter').value;table('messages',['자료','원본 시각','제목·대화','검토'],messages.filter(m=>filter==='all'||sourceKind(m)===filter).map(m=>[sourceKind(m),m.timestamp||'확인 필요',m.subject||m.message.chat||'대화',button('열람',()=>detail(m,'message-detail'))]));el('message-prev').disabled=offset===0;el('message-next').disabled=!data.hasMore;table('approval-records',['문서번호','제목','버전','검토'],(await request('/api/workspace/messages/hiworks/0')).items.map(m=>[m.message.document_number||m.message.documentNumber||m.message.metadata?.['문서 번호']||'',m.subject,m.version,button('본문·이력',()=>detail(m,'approval-detail'))]));}
 async function loadEvidence(){const data=await request('/api/evidence');const host=el('work-evidence');const old=host.value;host.replaceChildren(new Option('등록한 근거 선택',''));for(const row of data.items)host.append(new Option(row.locator+' · '+row.evidence.slice(0,65),row.id));host.value=old;}
 async function loadState(fill=false){const data=await request('/api/workspace/status');settings=data.settings;if(fill){for(const [name,value] of Object.entries(settings)){const field=form.elements.namedItem(name);if(!field)continue;if(field.type==='checkbox')field.checked=value;else field.value=Array.isArray(value)?value.join('\n'):value;}}
  deviceView(data);
  const pairing=data.browser;el('browser-status').textContent=pairing.lastSeen?'확장 연결 마지막 응답: '+pairing.lastSeen:pairing.paired?'연결 코드 발급됨 · 확장의 응답은 아직 확인되지 않았습니다.':'수집 확장 연결 전입니다.';
  table('jobs',['작업','상태','시작','결과'],data.jobs.map(j=>[actions[j.action]||j.action,labels[j.status]||j.status,j.started,jobSummary(j.result)]));table('archive-runs',['보고 기간','상태','실제 보관 시각'],data.archiveRuns.map(r=>[r.week_end,labels[r.status]||r.status,r.observed_at]));
  const metrics=settings.device_role==='company-relay'?[['공유폴더 보관',settings.collection_enabled?'평일 매시간':'꺼짐'],['원본 목록',settings.weekly_archive_enabled?'금요일 16:00':'꺼짐'],['오멘 전달',settings.handoff_transport==='web'?'회사 웹 OneDrive':settings.handoff_dir?'회사 OneDrive':'폴더 연결 전']]:[['자료 수집',settings.collection_enabled?'평일 매시간':'꺼짐'],['Teams',settings.collection_enabled?'평일 낮 12시 1회':'꺼짐'],['주간회의 자료',settings.enabled?'금요일 16:15':'꺼짐']];
  el('home-summary').replaceChildren();for(const [title,value] of metrics){const card=document.createElement('article');const strong=document.createElement('strong');strong.textContent=value;const p=document.createElement('p');p.textContent=title;card.append(strong,p);el('home-summary').append(card);}
  if(data.jobs.some(j=>j.status==='running')||settings.handoff_transport==='web'&&['opening','login_required'].includes(data.cloud?.connection?.status)){if(!pollTimer)pollTimer=setTimeout(async()=>{pollTimer=null;try{await loadState();await loadMessages();await refresh();}catch(e){errorMessage(e);}},2000);}else{if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}document.querySelectorAll('[data-collect]').forEach(b=>b.disabled=false);}
  return data;
 }
 form.addEventListener('submit',async event=>{event.preventDefault();if(!settings)return;const updated={...settings};for(const [key] of Object.entries(settings)){const field=form.elements.namedItem(key);if(!field)continue;updated[key]=field.type==='checkbox'?field.checked:key==='teams_chats'?field.value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean):field.value;}try{await post('/api/workspace/settings',updated);await loadState();await refresh();el('connection-status').textContent=settings.device_role==='company-relay'?'회사 공유폴더 보관·전달 설정을 저장했습니다.':'연결 설정을 저장했습니다. Teams 예약은 평일 낮 12시 1회입니다.';}catch(e){errorMessage(e);}});
 form.elements.namedItem('handoff_transport').addEventListener('change',event=>transportView(settings?.device_role==='company-relay',['omen','company-relay'].includes(settings?.device_role),event.target.value));
 el('handoff-web-connect').addEventListener('click',async()=>{try{await post('/api/workspace/handoff-web-connect',{});await loadState();}catch(e){el('handoff-web-status').textContent=e.message;}});
 for(const b of document.querySelectorAll('[data-collect]'))b.addEventListener('click',async()=>{try{document.querySelectorAll('[data-collect]').forEach(x=>x.disabled=true);await post('/api/workspace/start',{action:b.dataset.collect});el('notice').textContent=actions[b.dataset.collect]+'을 시작했습니다. 실행 기록에서 결과를 확인하세요.';await loadState();}catch(e){document.querySelectorAll('[data-collect]').forEach(x=>x.disabled=false);errorMessage(e);}});
 el('message-filter').addEventListener('change',()=>{offset=0;loadMessages().catch(errorMessage);});el('message-prev').addEventListener('click',()=>{offset=Math.max(0,offset-50);loadMessages().catch(errorMessage);});el('message-next').addEventListener('click',()=>{offset+=50;loadMessages().catch(errorMessage);});
 el('browser-pair').addEventListener('click',async()=>{try{const data=await post('/api/workspace/browser-pair',{});el('browser-code-label').hidden=false;el('browser-code').value=data.connectionCode;await loadState();}catch(e){errorMessage(e);}});
 el('browser-code-copy').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(el('browser-code').value);el('browser-status').textContent='연결 코드를 복사했습니다. Whale의 ARIADNE 확장 화면에 붙여넣고 연결 저장을 누르세요.';}catch{el('browser-code').select();el('browser-status').textContent='코드를 선택했습니다. Ctrl+C로 복사하세요.';}});
 el('handoff-pair').addEventListener('click',async()=>{try{const data=await post('/api/workspace/handoff-pair',{});el('handoff-code-label').hidden=false;el('handoff-code').value=data.connectionCode;await loadState();}catch(e){el('handoff-key-status').textContent=e.message;}});
 el('handoff-copy').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(el('handoff-code').value);el('handoff-key-status').textContent='복사했습니다. 오멘 ARIADNE의 회사 PC와 오멘 자료 연결 칸에 붙여넣으세요.';}catch{el('handoff-code').select();el('handoff-key-status').textContent='코드를 선택했습니다. Ctrl+C로 복사하세요.';}});
 el('handoff-key-form').addEventListener('submit',async event=>{event.preventDefault();try{await post('/api/workspace/handoff-key',{connectionCode:el('handoff-key').value.trim()});el('handoff-key').value='';await loadState();}catch(e){el('handoff-key-status').textContent=e.message;}});
 el('schedule-register').addEventListener('click',async()=>{try{await post('/api/workspace/schedule-register',{});await loadState();el('connection-status').textContent='Windows 예약을 등록했습니다. 위 설정의 실행 스위치를 확인하세요.';}catch(e){el('connection-status').textContent=e.message;}});
 el('new-work-toggle').addEventListener('click',()=>{el('work-form').hidden=!el('work-form').hidden;});
 el('work-form').addEventListener('submit',async event=>{event.preventDefault();const data=new FormData(event.target),proof=data.get('evidenceId');const item={id:crypto.randomUUID(),title:data.get('title'),fundId:data.get('fundId')||null,ownerId:null,status:'new',reviewRequired:true,confidence:'low',due:{kind:'unknown',rawText:'기한 확인 필요',date:null,timezone:'Asia/Seoul',anchorTaskId:null,confirmed:false,evidenceIds:[proof]},sources:[proof],completionConditions:[{id:'condition-1',label:data.get('condition')}],completionEvidence:[]};try{await post('/api/work-items',{item,actorId:'user',reason:'보관 근거를 검토하고 업무 등록'});await showWork();event.target.reset();el('work-status').textContent='업무를 등록했습니다.';}catch(e){el('work-status').textContent=e.message;}});
 request('/api/funds').then(data=>{for(const f of data.items)el('work-fund').append(new Option(f.name,f.id));}).catch(errorMessage);
 Promise.all([loadState(true),loadMessages(),loadEvidence()]).catch(errorMessage);
 window.addEventListener('ariadne-data-refresh',()=>{loadMessages().catch(errorMessage);loadState().catch(errorMessage);});
})();
