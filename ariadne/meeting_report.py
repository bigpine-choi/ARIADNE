"""Render into the provided meeting layout; keep auxiliary sheets as dated references."""
from copy import copy
from datetime import timedelta
from io import BytesIO
from pathlib import Path
import unicodedata

from .reporting import KST, excel_text


def meeting_summary(body, maximum=20, width=110):
    """Count wrapped Korean text so the appendix notice remains visible."""
    lines=[]
    for paragraph in body.splitlines():
        line='';units=0
        for char in paragraph:
            cost=2 if unicodedata.east_asian_width(char) in {'W','F'} else 1
            if units+cost>width:lines.append(line);line='';units=0
            line+=char;units+=cost
        lines.append(line)
    if len(lines)>maximum:lines=lines[:maximum-1]+['… 전체 내용은 주간업무보고 시트 참조']
    return '\n'.join(lines)


def write_meeting_report(destination, template, report, sources, start, end, warnings):
    from openpyxl import load_workbook
    from openpyxl.cell.cell import MergedCell
    from openpyxl.styles import Alignment, Font
    template=Path(template)
    before=template.stat();data=template.read_bytes();after=template.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise RuntimeError('Meeting template changed while reading')
    book=load_workbook(BytesIO(data))
    if '주간회의' not in book.sheetnames:
        raise ValueError('Configure a template with a 주간회의 sheet')
    meeting=book['주간회의']
    if meeting['C3'].value is None or meeting['D3'].value is None or meeting['A4'].value!='펀드':
        raise ValueError('Meeting template layout requires review')
    for row in meeting.iter_rows(min_row=4,max_row=13,min_col=3,max_col=4):
        for cell in row:
            if not isinstance(cell,MergedCell):cell.value=None
    local=end.astimezone(KST)
    monday=local.date()-timedelta(days=local.weekday())
    next_monday=monday+timedelta(days=7)
    meeting['D2']=local.date();meeting['D2'].number_format='yyyy-mm-dd'
    meeting['C3']=f'금주 ({monday:%m/%d}~{local:%m/%d})'
    meeting['D3']=f'차주 ({next_monday:%m/%d}~{next_monday+timedelta(days=4):%m/%d})'
    groups={r:[[],[]] for r in (4,6,10,11,12,13)}
    mapping={'investment':4,'fund_management':6,'ipo':10,'management':11,'compliance':12,'other':13}
    for item in report['items']:
        row=mapping.get(item.get('category','other'),13)
        proof=', '.join(item['evidence_ids'])
        groups[row][0].append(f"{item['project']} / {item['work']}\n{item['result']}\n확인: {item['issues']} [{proof}]")
        groups[row][1].append(f"{item['project']} / {item['next_week']} [{proof}]")
    for row,columns in groups.items():
        for column,parts in zip(('C','D'),columns):
            body='\n\n'.join(parts) if parts else '확인된 근거 없음'
            body=meeting_summary(body)
            meeting[f'{column}{row}']=excel_text(body)
            meeting[f'{column}{row}'].alignment=Alignment(wrap_text=True,vertical='top')
            meeting[f'{column}{row}'].font=Font(name='맑은 고딕',size=10)
        meeting.row_dimensions[row].height=min(409,max(65,14*max(len(meeting[f'{c}{row}'].value.splitlines()) for c in ('C','D'))))
    meeting.oddFooter.center.text='AI 작성 초안 · 근거 및 수집 범위 확인 · 보조 시트는 원본 기준값'
    meeting.print_options.horizontalCentered=True
    book.calculation.fullCalcOnLoad=True
    # Preserve the reference workbook's other sheets and formulas; attach all grounded rows.
    generated=load_workbook(destination)
    for source in generated:
        if source.title in book.sheetnames:raise ValueError('Meeting template conflicts with report sheet names')
        target=book.create_sheet(source.title)
        for row in source:
            for cell in row:
                dest=target.cell(cell.row,cell.column,cell.value)
                if cell.has_style:dest._style=copy(cell._style)
                dest.alignment=copy(cell.alignment)
                dest.number_format=cell.number_format
        for key,dimension in source.column_dimensions.items():target.column_dimensions[key]=copy(dimension)
        for key,dimension in source.row_dimensions.items():target.row_dimensions[key]=copy(dimension)
        target.freeze_panes=source.freeze_panes
        target.sheet_view.showGridLines=False
    generated.close()
    scope=book['수집 범위']
    scope.append(['회사 회의 양식',template.name])
    scope.append(['보조 시트','원본 양식의 값·수식을 보존했습니다. 이번 주 자료로 자동 갱신하지 않았습니다.'])
    scope.append(['회의 요약','긴 내용은 주간업무보고 시트에 전부 보관합니다. 근거 없는 결과·계획은 확인 필요입니다.'])
    destination=Path(destination);temporary=destination.with_suffix('.meeting.tmp.xlsx')
    book.save(temporary);book.close()
    check=load_workbook(temporary,read_only=True)
    if '근거' not in check.sheetnames or check['주간회의']['D2'].value.date()!=local.date():
        raise RuntimeError('Meeting workbook verification failed')
    check.close();temporary.replace(destination)
    return destination
