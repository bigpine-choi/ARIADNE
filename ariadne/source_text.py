"""Read archived document bytes as bounded text; never evaluate formulas or macros."""
from io import BytesIO
from pathlib import Path
import json
import zipfile
import xml.etree.ElementTree as ET


def extract_text(content, filename, limit=60000):
    suffix=Path(filename).suffix.casefold()
    if suffix in {'.txt','.md','.csv'}:
        return content.decode('utf-8-sig')[:limit]
    if suffix=='.docx':
        with zipfile.ZipFile(BytesIO(content)) as archive:
            info=archive.getinfo('word/document.xml')
            if info.file_size>4*1024*1024: raise ValueError('Document expansion exceeds limit')
            root=ET.fromstring(archive.read(info))
            return '\n'.join(n.text or '' for n in root.iter() if n.tag.endswith('}t'))[:limit]
    if suffix=='.xlsx':
        from openpyxl import load_workbook
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if sum(i.file_size for i in archive.infolist())>64*1024*1024:
                raise ValueError('Workbook expansion exceeds limit')
        book=load_workbook(BytesIO(content),read_only=True,data_only=True)
        try:
            # Weekly meeting material comes from its current meeting sheet,
            # not hidden historical/capital tables in the same workbook.
            sheets=[book['주간회의']] if '주간회의' in book.sheetnames else list(book)[:6]
            result=[]
            for sheet in sheets:
                for row in sheet.iter_rows(max_row=min(sheet.max_row,500),max_col=min(sheet.max_column,20)):
                    parts=[f'{c.coordinate}: {c.value}' for c in row if c.value is not None]
                    if parts: result.append(sheet.title+' '+ ' | '.join(parts))
                    if sum(map(len,result))>limit: return '\n'.join(result)[:limit]
            return '\n'.join(result)[:limit]
        finally: book.close()
    raise ValueError('This file type has no text reader; original bytes remain archived')
