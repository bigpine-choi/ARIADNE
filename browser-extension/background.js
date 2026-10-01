'use strict';
const BASE='http://127.0.0.1:8767';
let busy=false;
chrome.runtime.onInstalled.addListener(()=>chrome.alarms.create('ariadne',{periodInMinutes:1}));
chrome.runtime.onStartup.addListener(()=>chrome.alarms.create('ariadne',{periodInMinutes:1}));
chrome.action.onClicked.addListener(()=>chrome.runtime.openOptionsPage());
async function api(path,token,value){
 const r=await fetch(BASE+path,{method:value?'POST':'GET',headers:{'X-Ariadne-Bridge':token,...(value?{'Content-Type':'application/json'}:{})},
  ...(value?{body:JSON.stringify(value)}:{}),signal:AbortSignal.timeout(15000)});
 if(!r.ok)throw new Error('Local collector rejected request');
 return r.json();
}
function permitted(url,company){
 const u=new URL(url);
 const root='/'+company+'/approval/document/';
 const suffix=u.pathname.startsWith(root)?u.pathname.slice(root.length):'';
 const query=[...u.searchParams.entries()];
 return u.origin==='https://approval.office.hiworks.com' && /^(department_box\/all|box\/(writer|approval|return)|lists\/A|view\/\d+(\/condition\/[A-Za-z0-9_=/-]+)?)$/.test(suffix)
  && query.every(([k,v])=>['start_date','end_date'].includes(k)&&/^20\d{2}-\d{2}-\d{2}$/.test(v))
  && new Set(query.map(([k])=>k)).size===query.length;
}
async function read(tabId,company){
 const tab=await chrome.tabs.get(tabId);
 if(!permitted(tab.url,company))throw new Error('Wrong origin or login required');
 await chrome.scripting.executeScript({target:{tabId},files:['extract.js']});
 const results=await chrome.scripting.executeScript({target:{tabId},func:()=>extractHiworksPage()});
 return results[0].result;
}
async function load(tabId,url,company){
 if(!permitted(url,company))throw new Error('Disallowed source');
 await chrome.tabs.update(tabId,{url});
 for(let i=0;i<50;i++){
  await new Promise(r=>setTimeout(r,200));
  try{const page=await read(tabId,company);if(page.url.split('/condition/')[0].split('?')[0]===url)return page;}catch{}
 }
 throw new Error('Page unavailable');
}
async function collect(job,token){
 const tab=await chrome.tabs.create({url:job.lists[0],active:false});
 const warnings=new Set();const documents=new Set();let pages=0;
 try {
  for(const list of [...job.lists.slice(1),job.lists[0]]){
   let page=await load(tab.id,list,job.company);
   if(!page.paginationVerified)warnings.add('pagination_scope_unverified');
   for(let p=1;p<=page.pages;p++){
    if(++pages>job.maxPages){warnings.add('page_limit_reached');break;}
    if(p>1){
     const switched=await chrome.scripting.executeScript({target:{tabId:tab.id},func:p=>{
      const select=document.querySelector('#pageApprovalDepartmentBox select,#pageApprovalDocumentBox select,#pageApprovalDocument select');
      if(!select||![...select.options].some(o=>Number(o.value)===p))return false;
      select.value=String(p);select.dispatchEvent(new Event('change',{bubbles:true}));return true;
     },args:[p]});
     if(!switched[0].result){warnings.add('pagination_unavailable');break;}
     let ready=false;
     for(let n=0;n<50;n++){
      await new Promise(r=>setTimeout(r,200));
      const next=await read(tab.id,job.company);
      if(next.page===p&&next.rows.map(r=>r.documentId).join()!==page.rows.map(r=>r.documentId).join()){page=next;ready=true;break;}
     }
     if(!ready){warnings.add('pagination_unavailable');break;}
    }
    for(const row of page.rows)documents.add(row.documentId);
   }
   if(pages>=job.maxPages)break;
  }
  if(documents.size>job.maxDocuments)warnings.add('document_limit_reached');
  const root='https://approval.office.hiworks.com/'+job.company+'/approval/document/view/';
  let count=0;
  const ordered=[...documents].sort((a,b)=>(job.previousChecks?.[a]||'').localeCompare(job.previousChecks?.[b]||''));
  for(const id of ordered){
   if(++count>job.maxDocuments)break;
   if(Date.parse(job.deadline)<=Date.now()){warnings.add('collection_deadline_reached');break;}
   try{
    await load(tab.id,root+id,job.company);
    const history=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>{
     const a=[...document.querySelectorAll('#approvalCommentsTab a')].filter(a=>a.textContent.trim()==='이력');
     if(a.length!==1)return false;a[0].click();return true;
    }});
    if(history[0].result){
     for(let n=0;n<25;n++){
      await new Promise(r=>setTimeout(r,100));
      const ready=await read(tab.id,job.company);if(ready.historyLoaded)break;
     }
    }
    const detail=await read(tab.id,job.company);
    if(!detail.historyLoaded)warnings.add('history_unavailable');
    await api('/bridge/page',token,{jobId:job.id,page:detail});
   }catch{warnings.add('document_read_failed');}
  }
  await api('/bridge/finish',token,{jobId:job.id,complete:warnings.size===0,warnings:[...warnings]});
 }catch{
  await api('/bridge/finish',token,{jobId:job.id,complete:false,warnings:['login_or_layout_unavailable']}).catch(()=>{});
 }finally{await chrome.tabs.remove(tab.id);}
}
chrome.alarms.onAlarm.addListener(async a=>{
 if(a.name!=='ariadne'||busy)return;
 busy=true;
 try{
  const {token}=await chrome.storage.local.get('token');
  if(!token)return;
  const {job}=await api('/bridge/next',token);
  if(job)await collect(job,token);
 }catch{/* Local worker intentionally stops between scheduled runs. */}
 finally{busy=false;}
});
