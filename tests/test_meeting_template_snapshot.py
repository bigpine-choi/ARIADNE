"""Synthetic renderer regression: no collectors, accounts, AI, or mail."""
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook, load_workbook

from ariadne.meeting_report import write_meeting_report
from ariadne.reporting import KST, write_report


class MeetingTemplateSnapshotTests(unittest.TestCase):
    def test_path_and_frozen_bytes_preserve_auxiliary_formulas_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / 'company-template.xlsx'
            book = Workbook()
            meeting = book.active
            meeting.title = '주간회의'
            meeting['A4'] = '펀드'
            meeting['C3'] = '금주'
            meeting['D3'] = '차주'
            meeting['C6'] = 'Old meeting contents'
            auxiliary = book.create_sheet('기존 참고')
            auxiliary['A1'] = 'Frozen reference'
            auxiliary['B2'] = '=1+2'
            book.save(template)
            book.close()
            frozen = template.read_bytes()
            start = datetime(2026, 9, 25, 16, tzinfo=KST)
            end = start + timedelta(days=7)
            report = {'items': [{'project': 'Synthetic Fund', 'work': 'Review',
                'result': 'Checked', 'issues': '', 'next_week': 'Confirm',
                'category': 'fund_management', 'evidence_ids': ['S1']}]}
            sources = [{'source_id': 'one', 'provider': 'synthetic',
                        'body': 'Synthetic evidence', 'evidence_id': 'S1'}]
            labels = {'path': template.name,
                'bytes': '보관 양식 스냅샷 · SHA-256 ' + hashlib.sha256(frozen).hexdigest()}
            for mode, supplied in [('path', template), ('bytes', frozen)]:
                with self.subTest(mode=mode):
                    if mode == 'bytes':
                        # An edited live file must not replace the archived snapshot.
                        template.write_bytes(b'changed after the Friday cutoff')
                    before = template.read_bytes()
                    output = root / (mode + '.xlsx')
                    write_report(output, report, sources, start, end, [])
                    write_meeting_report(output, supplied, report, sources, start, end, [])
                    actual = load_workbook(output)
                    try:
                        self.assertEqual(datetime(2026, 10, 2), actual['주간회의']['D2'].value)
                        self.assertIn('Synthetic Fund', actual['주간회의']['C6'].value)
                        self.assertEqual('Frozen reference', actual['기존 참고']['A1'].value)
                        self.assertEqual('=1+2', actual['기존 참고']['B2'].value)
                        self.assertIn('근거', actual.sheetnames)
                        provenance = list(actual['수집 범위'].values)
                        self.assertIn(('회사 회의 양식', labels[mode]), provenance)
                        self.assertEqual(before, template.read_bytes())
                    finally:
                        actual.close()
