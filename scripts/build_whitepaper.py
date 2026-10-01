from pathlib import Path
from html import escape
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/WHITEPAPER.ko.md'
DESTINATION = ROOT / 'docs/Fund-Ops-Workbench-Whitepaper.ko.pdf'
pdfmetrics.registerFont(TTFont('Malgun', 'C:/Windows/Fonts/malgun.ttf'))
pdfmetrics.registerFont(TTFont('MalgunBold', 'C:/Windows/Fonts/malgunbd.ttf'))
pdfmetrics.registerFontFamily('Malgun', normal='Malgun', bold='MalgunBold')

styles = {
    'title': ParagraphStyle('title', fontName='MalgunBold', fontSize=23, leading=32, spaceAfter=12),
    'subtitle': ParagraphStyle('subtitle', fontName='Malgun', fontSize=10, leading=16, textColor=colors.HexColor('#555555'), spaceAfter=22),
    'h2': ParagraphStyle('h2', fontName='MalgunBold', fontSize=14, leading=21, spaceBefore=16, spaceAfter=9, keepWithNext=True),
    'body': ParagraphStyle('body', fontName='Malgun', fontSize=10.3, leading=17.2, spaceAfter=9, wordWrap='CJK', allowWidows=0, allowOrphans=0),
    'cell': ParagraphStyle('cell', fontName='Malgun', fontSize=9.1, leading=14.5, wordWrap='CJK'),
    'header': ParagraphStyle('header', fontName='MalgunBold', fontSize=9.1, leading=14.5, wordWrap='CJK', textColor=colors.white),
    'reference': ParagraphStyle('reference', fontName='Malgun', fontSize=8.8, leading=14.2, spaceAfter=6, wordWrap='CJK'),
}


def inline(text):
    value = escape(text)
    value = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', value)
    value = re.sub(r'`(.+?)`', r'\1', value)
    value = re.sub(r'(https://[^\s]+)', r'<link href="\1" color="#245B78">\1</link>', value)
    return value


def table_rows(lines):
    rows = [[part.strip() for part in line.strip().strip('|').split('|')] for line in lines]
    rows = [row for row in rows if not all(re.fullmatch(r'[-: ]+', cell) for cell in row)]
    count = len(rows[0])
    width = A4[0] - 88
    proportions = {3: [0.23, 0.30, 0.47], 4: [0.15, 0.28, 0.36, 0.21]}
    selected = proportions.get(count, [1 / count] * count)
    if count == 5:
        selected = [0.07, 0.11, 0.27, 0.30, 0.25]
    if count == 4 and rows[0][0] == '순위':
        selected = [0.07, 0.25, 0.45, 0.23]
    if count == 4 and rows[0][0] == '후보':
        selected = [0.21, 0.30, 0.31, 0.18]
    if count == 4 and rows[0][0] == '표현 유형':
        selected = [0.21, 0.24, 0.30, 0.25]
    if count == 2:
        selected = [0.22, 0.78]
    if count == 4 and rows[0][0] == '단계':
        selected = [0.09, 0.12, 0.39, 0.40]
    if count == 3 and rows[0][0] == '위험':
        selected = [0.31, 0.54, 0.15]
    data = [[Paragraph(inline(cell), styles['header' if index == 0 else 'cell']) for cell in row]
            for index, row in enumerate(rows)]
    result = Table(data, colWidths=[width * fraction for fraction in selected], repeatRows=1, hAlign='LEFT')
    result.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#343B41')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F4F6F7')]),
        ('GRID', (0, 0), (-1, -1), 0.45, colors.HexColor('#D9D9D9')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    return result


def page_footer(canvas, document):
    canvas.saveState()
    canvas.setFont('Malgun', 8)
    canvas.setFillColor(colors.HexColor('#666666'))
    canvas.drawString(44, 29, 'Fund Ops Workbench  |  2026-09-30')
    canvas.drawRightString(A4[0] - 44, 29, str(document.page))
    canvas.restoreState()


story = []
lines = SOURCE.read_text(encoding='utf-8').splitlines()
index = 0
while index < len(lines):
    line = lines[index].strip()
    if not line:
        index += 1
        continue
    if line.startswith('|'):
        table_lines = []
        while index < len(lines) and lines[index].strip().startswith('|'):
            table_lines.append(lines[index])
            index += 1
        story.extend([Spacer(1, 3), table_rows(table_lines), Spacer(1, 11)])
        continue
    if line.startswith('# '):
        story.append(Paragraph(inline(line[2:]), styles['title']))
    elif line.startswith('## '):
        story.append(Paragraph(inline(line[3:]), styles['h2']))
    elif line.startswith('Fund Ops Workbench |'):
        story.append(Paragraph(inline(line.replace(' | ', '<br/>')).replace('&lt;br/&gt;', '<br/>'), styles['subtitle']))
    elif line.startswith('- S'):
        story.append(Paragraph(inline(line[2:]), styles['reference']))
    else:
        story.append(Paragraph(inline(line), styles['body']))
    index += 1

doc = SimpleDocTemplate(str(DESTINATION), pagesize=A4, leftMargin=44, rightMargin=44,
                        topMargin=40, bottomMargin=48, title='펀드 운용 업무 자동화 개발계획 백서',
                        author='Fund Ops Workbench', pageCompression=1)
doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
reader = PdfReader(str(DESTINATION))
text = '\n'.join(page.extract_text() for page in reader.pages)
for section in range(1, 16):
    assert f'{section}.' in text, f'Missing section {section}'
print(f'Created {len(reader.pages)} pages: {DESTINATION}')
