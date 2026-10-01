"""Validated snapshots of submitted Hiworks documents, immutable by content."""
from datetime import datetime
import json
import re
from urllib.parse import urlsplit
from .models import ValidationError
from .reporting import KST

META = {'문서 종류','문서 번호','기안 부서','기안자','보존 연한/보안 등급','문서함','기안 일시','완료 일시'}


def safe_url(url, company, *, detail=False):
    if not isinstance(url,str): raise ValidationError('하이웍스 주소를 확인하세요.')
    value=urlsplit(url)
    base=r'/'+re.escape(company)+r'/approval/document/'
    suffix=r'view/\d+(?:/condition/[A-Za-z0-9_=/-]+)?' if detail else r'(?:department_box/all|box/(?:all|writer|approval|refer|read|return)|lists/[AWVEP])'
    if value.scheme!='https' or value.netloc!='approval.office.hiworks.com' or value.query or not re.fullmatch(base+suffix,value.path):
        raise ValidationError('읽기 전용 하이웍스 문서함·열람 주소만 허용됩니다.')
    return value.scheme+'://'+value.netloc+value.path.split('/condition/')[0]


def stamp(value):
    if not isinstance(value,str) or not re.fullmatch(r'20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}',value):
        return None
    try: return datetime.strptime(value,'%Y-%m-%d %H:%M:%S').replace(tzinfo=KST).isoformat()
    except ValueError: return None


def archive_detail(store, value, company):
    keys={'kind','url','documentId','company','documentNumber','title','metadata','body',
          'approvals','history','historyLoaded','attachments','capturedAt'}
    if not isinstance(value,dict) or set(value)!=keys or value['kind']!='detail' or value['company']!=company:
        raise ValidationError('상신 문서 수집 형식을 확인하세요.')
    url=safe_url(value['url'],company,detail=True)
    if not isinstance(value['documentId'],str) or not value['documentId'].isdigit() or not url.endswith('/'+value['documentId']):
        raise ValidationError('문서 식별자와 원본 주소가 다릅니다.')
    for field,maximum in [('documentNumber',100),('title',1000),('body',150000),('capturedAt',100)]:
        if not isinstance(value[field],str) or not value[field].strip() or len(value[field])>maximum:
            raise ValidationError('문서 내용의 크기·형식을 확인하세요.')
    try:
        if datetime.fromisoformat(value['capturedAt']).tzinfo is None: raise ValueError()
    except ValueError: raise ValidationError('문서 수집 시각을 확인하세요.') from None
    if not isinstance(value['metadata'],dict) or set(value['metadata'])-META or any(not isinstance(v,str) or len(v)>2000 for v in value['metadata'].values()):
        raise ValidationError('문서 메타데이터를 확인하세요.')
    if value['documentNumber']!=value['metadata'].get('문서 번호') or not stamp(value['metadata'].get('기안 일시')):
        raise ValidationError('상신 문서번호·기안 일시를 확인하세요.')
    if type(value['historyLoaded']) is not bool or not isinstance(value['history'],list) or len(value['history'])>500 or any(not isinstance(h,str) or len(h)>4000 for h in value['history']):
        raise ValidationError('결재 이력 형식을 확인하세요.')
    if not isinstance(value['approvals'],list) or len(value['approvals'])>20:
        raise ValidationError('결재선 형식을 확인하세요.')
    for line in value['approvals']:
        if not isinstance(line,dict) or set(line)!={'text','marks'} or not isinstance(line['text'],str) or len(line['text'])>12000 or not isinstance(line['marks'],list) or len(line['marks'])>100:
            raise ValidationError('결재선 형식을 확인하세요.')
        if any(not isinstance(m,dict) or set(m)!={'label','at'} or any(not isinstance(v,str) or len(v)>500 for v in m.values()) for m in line['marks']):
            raise ValidationError('결재 처리 표시를 확인하세요.')
    if not isinstance(value['attachments'],list) or len(value['attachments'])>200: raise ValidationError('첨부 목록을 확인하세요.')
    for attachment in value['attachments']:
        if not isinstance(attachment,dict) or set(attachment)!={'name','url'} or not isinstance(attachment['name'],str) or len(attachment['name'])>1000 or not isinstance(attachment['url'],str):
            raise ValidationError('첨부 목록을 확인하세요.')
        parsed=urlsplit(attachment['url'])
        if parsed.scheme!='https' or parsed.netloc!='approval-storage-api.office.hiworks.com' or not re.fullmatch(r'/download/documents/'+value['documentId']+r'/files/\d+',parsed.path) or parsed.query:
            raise ValidationError('첨부 원본 주소를 확인하세요.')
    # Raw HTML, browser storage and authentication material are deliberately absent.
    completed=stamp(value['metadata'].get('완료 일시'))
    message={'source_id':value['documentId'],'provider':'hiworks','direction':'approval',
      'timestamp':completed or stamp(value['metadata']['기안 일시']),
      'subject':value['title'],'body':value['body'],'sender':value['metadata'].get('기안자',''),
      'document_number':value['documentNumber'],'source_url':url,'metadata':value['metadata'],
      'approvals':value['approvals'],'history':value['history'],'attachments':value['attachments'],
      'history_loaded':value['historyLoaded'],'source_status':'completion_time_present' if completed else 'completion_unverified',
      'captured_at':value['capturedAt']}
    count=store.ingest_messages('hiworks:'+company,[message])
    return {'stored':count,'complete':value['historyLoaded'],'documentNumber':value['documentNumber']}
