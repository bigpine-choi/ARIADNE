/* Read-only DOM extraction: no cookies, local storage, passwords or form values. */
function extractHiworksPage() {
  const url = new URL(location.href);
  const match = url.pathname.match(/^\/([^/]+)\/approval\/document\/view\/(\d+)/);
  const clean = e => (e?.innerText || e?.textContent || '').trim();
  if (url.origin !== 'https://approval.office.hiworks.com') throw new Error('Wrong source origin');
  if (match) {
    const meta = {};
    const table = document.querySelector('table.tableType02');
    if (!table || !document.querySelector('#contents') || !document.querySelector('h2'))
      throw new Error('Detail layout unavailable or login required');
    for (const row of table.querySelectorAll('tr')) {
      const cells = [...row.querySelectorAll(':scope > td,:scope > th')];
      for (let i=0; i+1<cells.length; i+=2) {
        const value = cells[i+1].querySelector('select');
        meta[clean(cells[i])] = value ? value.selectedOptions[0]?.textContent.trim() : clean(cells[i+1]);
      }
    }
    if (!meta['문서 번호'] || !meta['기안 일시']) throw new Error('Submitted document metadata missing');
    const history = document.querySelector('#approvalCommentsHistory');
    return {kind:'detail',url:url.origin+match[0],documentId:match[2],company:match[1],
      documentNumber:meta['문서 번호'],title:clean(document.querySelector('h2')).replace(/\n요약$/,''),
      metadata:meta,body:clean(document.querySelector('#contents')),
      approvals:[...document.querySelectorAll('.js-approval-line')].map(t=>({
        text:clean(t),marks:[...t.querySelectorAll('img')].map(i=>({label:i.alt||i.getAttribute('aria-label')||'',at:i.title||''}))
      })),history:history?[...history.children].map(clean):[],
      historyLoaded:!!history && getComputedStyle(document.querySelector('#divApprovalCommentsHistory')||history).display!=='none',
      attachments:[...document.querySelectorAll('a[href*="/download/documents/"]')].map(a=>({
        name:clean(a),url:a.href
      })),capturedAt:new Date().toISOString()};
  }
  const table = document.querySelector('#tableApprovalDepartmentBox,#tableApprovalDocumentBox,#tableApprovalDocument');
  if (!table) throw new Error('List layout unavailable or login required');
  const rows = [...table.querySelectorAll('tr')].map(r=>{
    const node = r.querySelector('td.docu-num');
    const action = node?.getAttribute('data-href')||'';
    const id = action.match(/getView\(['"]?(\d+)/)?.[1];
    if (!id) return null;
    const dates = [...r.querySelectorAll('td[title]')].map(c=>c.title).filter(t=>/^20\d{2}-\d{2}-\d{2}/.test(t));
    return {documentId:id,documentNumber:clean(node),title:clean(r.querySelector('td.title div'))||clean(r.querySelector('td.title')),
      author:clean(r.querySelector('.docu-register')),dates,form:clean(r.querySelector('.docu-form'))};
  }).filter(Boolean);
  if (!rows.length && !table.innerText.includes('문서가 존재하지 않습니다')) throw new Error('List still loading');
  const page = document.querySelector('#pageApprovalDepartmentBox select,#pageApprovalDocumentBox select,#pageApprovalDocument select');
  const paginationVerified=!!page||rows.length===0;
  return {kind:'list',url:url.href,rows,page:page?Number(page.value):1,
    pages:page?Math.max(...[...page.options].map(o=>Number(o.value))):1,
    pageControl:!!page,paginationVerified,capturedAt:new Date().toISOString()};
}
if (typeof module !== 'undefined') module.exports={extractHiworksPage};
