"""Bounded analysis must keep every source and reject cross-chunk evidence."""
import io
import json
import unittest
from unittest.mock import patch

from ariadne.reporting import extract_report


class ReportChunkTests(unittest.TestCase):
    def sources(self, count=3):
        return [{'source_id':str(i),'provider':'synthetic','body':'a'*500} for i in range(count)]

    def response(self, ids):
        row={key:'synthetic' for key in ['project','work','result','issues','next_week']}
        row.update(category='other',evidence_ids=ids)
        return io.BytesIO(json.dumps({'status':'completed','output':[{'content':[
            {'type':'output_text','text':json.dumps({'items':[row]})}]}]}).encode())

    def test_all_sources_analyzed_once_and_identical_rows_merge_evidence(self):
        requests=[]
        def opener(request, **kwargs):
            payload=json.loads(request.data)
            self.assertFalse(payload['store'])
            self.assertLessEqual(len(payload['input']),1000)
            group=json.loads(payload['input']);requests.extend(group)
            return self.response([s['evidence_id'] for s in group])
        with patch('ariadne.credentials.resolve_openai_key',return_value='synthetic-key'):
            report,sources=extract_report(self.sources(),'synthetic-model',1000,opener)
        self.assertEqual(['S1','S2','S3'],[s['evidence_id'] for s in requests])
        self.assertEqual(['S1','S2','S3'],report['items'][0]['evidence_ids'])
        self.assertEqual([1,2,3],[s['analysisChunk'] for s in sources])
        self.assertTrue(all(s['analysisChunks']==3 for s in sources))

    def test_evidence_from_other_chunk_fails_entire_analysis(self):
        calls=[]
        def opener(request, **kwargs):
            calls.append(request)
            return self.response(['S1'])
        with patch('ariadne.credentials.resolve_openai_key',return_value='synthetic-key'):
            with self.assertRaises(ValueError):extract_report(self.sources(),'synthetic-model',1000,opener)
        self.assertEqual(2,len(calls))

    def test_oversized_source_and_nine_chunks_fail_before_network(self):
        def forbidden(*a,**k):raise AssertionError('No request should be sent')
        with patch('ariadne.credentials.resolve_openai_key',return_value='synthetic-key'):
            with self.assertRaises(ValueError):extract_report(self.sources(),'synthetic-model',300,forbidden)
            with self.assertRaises(ValueError):extract_report(self.sources(9),'synthetic-model',1000,forbidden)

    def test_later_incomplete_response_does_not_return_partial_report(self):
        calls=[]
        def opener(request, **kwargs):
            calls.append(request)
            return self.response(['S1']) if len(calls)==1 else io.BytesIO(b'{"status":"incomplete"}')
        with patch('ariadne.credentials.resolve_openai_key',return_value='synthetic-key'):
            with self.assertRaises(RuntimeError):extract_report(self.sources(),'synthetic-model',1000,opener)
