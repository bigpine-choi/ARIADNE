"""Synthetic draft workflows and Windows current-user credential persistence."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ariadne.credentials import CredentialStore, CredentialError, resolve_openai_key
from ariadne.demo import seed_demo
from ariadne.hiworks import DraftStore, prepare_approval, prepare_weekly, build_page_link_payload
from ariadne.models import ValidationError
from ariadne.reporting import KST, extract_report
from ariadne.repository import Repository
from ariadne.server import create_server

def sample():
    return {"title":"[가상] 수수료 납부", "fund":"가상 펀드", "asset":"가상 자산",
            "instructionDate":"2026-10-02","description":"가상 청구 내역 검토 후 집행 준비",
            "withdrawalAccount":"가상 출금계좌 / 예금주","depositAccount":"가상 입금계좌 / 예금주",
            "stamp":"가상 펀드 인감","recipients":["가상 수탁기관"],
            "lines":[{"label":"가상 수수료","supply":"100000","vat":"10000","evidenceIds":["synthetic-request-001"]}],
            "declaredTotal":"110000","attachments":[{"name":"가상증빙.pdf","evidenceIds":["synthetic-request-001"]}],
            "evidenceIds":["synthetic-request-001"],"approvalLine":[],"retentionYears":5,
            "securityLevel":"A","documentBox":"가상 문서함"}

class DraftTests(unittest.TestCase):
    def prepare(self, value=None):
        return prepare_approval(value or sample(), {"synthetic-request-001"})

    def test_amount_sections_and_evidence(self):
        draft=self.prepare()
        self.assertIn("110,000원",draft["bodyText"])
        for label in ("운용지시 개요","날인 및 송부","3. 첨부"):
            self.assertIn(label,draft["bodyText"])
        self.assertFalse(draft["externalSubmission"])
        self.assertEqual(["synthetic-request-001"],draft["evidenceIds"])
        self.assertIn("결재선 확인 필요",draft["warnings"])

    def test_invalid_total_decimal_negative_and_boolean_are_rejected(self):
        for supply, total in (("100001","110000"),("1.5","11"),("-1","9"),(True,"11")):
            with self.subTest(supply=supply):
                value=sample();value["lines"][0]["supply"]=supply;value["declaredTotal"]=total
                with self.assertRaises(ValidationError):self.prepare(value)

    def test_multi_line_totals_are_exact(self):
        value=sample();value["lines"].append({"label":"추가","supply":"1","vat":"0","evidenceIds":value["evidenceIds"]})
        value["declaredTotal"]="110001"
        self.assertIn("110,001원",self.prepare(value)["bodyText"])

    def test_no_fabricated_or_unlinked_line_evidence(self):
        for where in ("evidenceIds","line","attachment"):
            value=sample()
            if where=="line":value["lines"][0]["evidenceIds"]=["not-linked"]
            elif where=="attachment":value["attachments"][0]["evidenceIds"]=["not-linked"]
            else:value[where]=["not-registered"]
            with self.subTest(where=where),self.assertRaises(ValidationError):self.prepare(value)

    def test_invalid_calendar_and_metadata(self):
        for key,value in (("instructionDate","2026-02-29"),("retentionYears",True),
                          ("securityLevel","unknown"),("approvalLine",[{"role":"pay","name":"test"}])):
            item=sample();item[key]=value
            with self.subTest(key=key),self.assertRaises(ValidationError):self.prepare(item)

    def test_html_never_executes_source_markup(self):
        value=sample();value["description"]='<script>alert(1)</script><img src=x onerror="fail">'
        value["lines"][0]["label"]="<svg/onload=alert(2)>"
        draft=self.prepare(value)
        self.assertNotIn("<script",draft["bodyHtml"]);self.assertNotIn("<img",draft["bodyHtml"])
        self.assertIn("&lt;script&gt;",draft["bodyHtml"])

    def test_attachment_paths_duplicate_and_unknown_fields_rejected(self):
        for name in ("../secret.pdf","C:\\private\\file.pdf","test\nfile.pdf"):
            value=sample();value["attachments"][0]["name"]=name
            with self.subTest(name=name),self.assertRaises(ValidationError):self.prepare(value)
        value=sample();value["unexpected"]="execute"
        with self.assertRaises(ValidationError):self.prepare(value)

    def test_immutable_deduplication_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"drafts.sqlite3";store=DraftStore(path)
            first=store.save(self.prepare());store.save(self.prepare())
            self.assertEqual(1,len(DraftStore(path).list()))
            self.assertEqual(first,DraftStore(path).get(first["id"]))
            value=sample();value["description"]="수정한 내용"
            second=store.save(self.prepare(value))
            self.assertNotEqual(first["id"],second["id"])
            self.assertEqual(first,store.get(first["id"]))
            self.assertEqual(2,len(store.list()))

    def weekly(self, stamp="2026-10-01T00:00:00+09:00"):
        report={"items":[{"project":"가상","work":"자료 전달","result":"완료 확인 필요",
                          "issues":"확인 필요","next_week":"확인 필요","evidence_ids":["S1"]}]}
        sources=[{"evidence_id":"S1","timestamp":stamp}]
        return prepare_weekly(report,sources,datetime(2026,9,25,16,tzinfo=KST),
                              datetime(2026,10,2,16,tzinfo=KST))

    def test_weekly_grounding_period_and_unknown_time(self):
        draft=self.weekly()
        self.assertIn("종료시각 미포함",draft["bodyText"])
        self.assertFalse(draft["metadata"]["formConfirmed"])
        self.assertTrue(any("시각 미확인" in x for x in self.weekly(None)["warnings"]))

    def test_weekly_excludes_end_boundary_and_naive_timestamp(self):
        for timestamp in ("2026-10-02T16:00:00+09:00","2026-09-25T15:59:59+09:00","2026-10-01T00:00:00"):
            with self.subTest(timestamp=timestamp),self.assertRaises(ValidationError):self.weekly(timestamp)

    def test_page_payload_has_no_transport_and_attachment_omission_blocked(self):
        with self.assertRaises(ValidationError):
            build_page_link_payload(self.prepare(),"API-form","https://callback.example.invalid/event","request-1")
        value=sample();value["attachments"]=[]
        draft=self.prepare(value)
        body=build_page_link_payload(draft,"API-form","https://callback.example.invalid/event","request-1")
        self.assertEqual("Y",body["modify_contents_flag"])
        self.assertNotIn("Authorization",body)
        for callback in ("http://example.invalid","https://user:pass@example.invalid"):
            with self.assertRaises(ValidationError):build_page_link_payload(draft,"form",callback,"one")

class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=CredentialStore(Path(self.tmp.name)/"credentials")
        self.key="synthetic-api-credential-00001"

    def test_input_errors_never_echo_key(self):
        for value in ("short","synthetic-api-credential\nleak",None):
            with self.assertRaises(CredentialError) as error:self.store.save(value)
            self.assertNotIn(str(value),str(error.exception))

    def test_no_plaintext_fallback_on_unsupported_os(self):
        with patch("ariadne.credentials.os.name","posix"):
            with self.assertRaises(CredentialError):self.store.save(self.key)
        self.assertFalse(self.store.path.exists())

    def test_environment_fallback_and_missing_registration(self):
        with patch.dict(os.environ,{"OPENAI_API_KEY":self.key}):
            self.assertEqual(self.key,resolve_openai_key(self.store))
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(CredentialError):resolve_openai_key(self.store)

    @unittest.skipUnless(os.name=="nt","Windows DPAPI")
    def test_dpapi_restart_replace_remove_and_ciphertext(self):
        self.store.save(self.key)
        self.assertNotIn(self.key.encode(),self.store.path.read_bytes())
        self.assertNotIn(self.key,json.dumps(self.store.status()))
        restarted=CredentialStore(self.store.directory)
        self.assertEqual(self.key,restarted.read())
        new_key=self.key+"-replaced"
        restarted.save(new_key)
        self.assertEqual(new_key,self.store.read())
        self.assertEqual([],list(self.store.directory.glob(".key-*")))
        restarted.remove();self.assertIsNone(self.store.read())

    @unittest.skipUnless(os.name=="nt","Windows DPAPI")
    def test_saved_key_automatically_used_in_new_process(self):
        self.store.save(self.key)
        script="from ariadne.credentials import CredentialStore; import sys; print(bool(CredentialStore(sys.argv[1]).read()))"
        result=subprocess.run([sys.executable,"-c",script,str(self.store.directory)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode);self.assertEqual("True",result.stdout.strip())
        self.assertNotIn(self.key,result.stdout+result.stderr)

    @unittest.skipUnless(os.name=="nt","Windows DPAPI")
    def test_corruption_blocks_call_and_does_not_echo_key(self):
        self.store.save(self.key);self.store.path.write_bytes(b"corrupt")
        with patch.dict(os.environ,{"OPENAI_API_KEY":"fallback-should-not-hide-corruption"}):
            with self.assertRaises(CredentialError):resolve_openai_key(self.store)

    @unittest.skipUnless(os.name=="nt","Windows DPAPI")
    def test_reporting_uses_saved_key_without_environment_and_without_real_call(self):
        self.store.save(self.key)
        report={"items":[{"project":"가상","work":"검토","result":"확인 필요","issues":"확인 필요",
                          "next_week":"확인 필요","evidence_ids":["S1"]}]}
        response={"status":"completed","output":[{"content":[{"type":"output_text","text":json.dumps(report)}]}]}
        def fake(request,timeout):
            self.assertEqual("Bearer "+self.key,request.get_header("Authorization"))
            self.assertEqual(180,timeout)
            return io.BytesIO(json.dumps(response).encode())
        with patch.dict(os.environ,{},clear=True),patch("ariadne.credentials.CredentialStore",return_value=self.store):
            result,sources=extract_report([{"body":"가상","source_id":"one"}],"configured-model",opener=fake)
        self.assertEqual(report,result)

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.repo=Repository(self.base/"work.sqlite3");seed_demo(self.repo)
        self.vault=CredentialStore(self.base/"credentials")
        self.drafts=DraftStore(self.base/"drafts.sqlite3")
        self.server=create_server(self.repo,port=0,credential_store=self.vault,draft_store=self.drafts)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url="http://127.0.0.1:"+str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.repo.close();self.tmp.cleanup()

    def request(self,path,body=None,headers=None):
        request=Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,
                        headers={"Content-Type":"application/json",**(headers or {})})
        try:
            with urlopen(request,timeout=5) as response:return response.status,json.load(response)
        except HTTPError as error:
            with closing(error):return error.code,json.load(error)

    def test_create_list_get_and_restart_storage(self):
        status,result=self.request("/api/hiworks/drafts",sample())
        self.assertEqual(201,status);identifier=result["draft"]["id"]
        self.assertEqual(identifier,self.request("/api/hiworks/drafts")[1]["items"][0]["id"])
        self.assertEqual(result,self.request("/api/hiworks/drafts/"+identifier)[1])
        self.assertFalse(result["draft"]["externalSubmission"])

    def test_fabricated_evidence_and_amount_fail_without_persistence(self):
        for key,value in (("evidenceIds",["fake"]),("declaredTotal","110001")):
            body=sample();body[key]=value
            self.assertEqual(400,self.request("/api/hiworks/drafts",body)[0])
        self.assertEqual([],self.drafts.list())

    def test_origin_cross_site_and_extra_secret_fields_blocked(self):
        self.assertEqual(403,self.request("/api/settings/openai",{"apiKey":"synthetic-key-000000"},
                                          {"Origin":"https://attacker.example.invalid"})[0])
        self.assertEqual(403,self.request("/api/hiworks/drafts",sample(),{"Sec-Fetch-Site":"cross-site"})[0])
        self.assertEqual(400,self.request("/api/settings/openai",{"apiKey":"synthetic-key-000000","echo":True})[0])
        self.assertFalse(self.vault.path.exists())

    def test_status_never_returns_key_or_claims_verification(self):
        code,result=self.request("/api/settings/openai")
        self.assertEqual(200,code);self.assertFalse(result["connectionVerified"])
        self.assertNotIn("apiKey",result)

    @unittest.skipUnless(os.name=="nt","Windows DPAPI")
    def test_http_key_saved_once_reused_and_deleted(self):
        key="synthetic-credential-http-000001"
        code,result=self.request("/api/settings/openai",{"apiKey":key})
        self.assertEqual(200,code);self.assertTrue(result["saved"]);self.assertNotIn(key,json.dumps(result))
        self.assertEqual(key,CredentialStore(self.vault.directory).read())
        self.assertEqual(200,self.request("/api/settings/openai/remove",{})[0])
        self.assertIsNone(self.vault.read())
