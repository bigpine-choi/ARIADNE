"""The noon screen collector must never run because of hourly retries or catch-up."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ariadne.automation_store import AutomationStore
from ariadne.local_settings import DEFAULTS
from ariadne.repository import Repository
from ariadne.reporting import KST
from ariadne.workspace import Workspace, collection_slot, teams_slot


class WorkspaceScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.store=AutomationStore(self.base/'archive.sqlite3')
        self.repo=Repository(self.base/'domain.sqlite3')
        self.mail=Mock(return_value={'messages':[],'complete':True,'warnings':[]})
        self.teams=Mock(return_value={'messages':[],'complete':False,'warnings':['teams_screen_scope_only']})
        self.ws=Workspace(self.store,self.base/'settings.json',self.repo,mail=self.mail,teams=self.teams)
        self.ws.save_settings({**DEFAULTS,'collection_enabled':True,'mailbox':'owner@example.invalid','teams_chats':['allowed-1','allowed-2']})
        self.noon=datetime(2026,10,1,12,tzinfo=KST)

    def tearDown(self):
        self.ws.close();self.store.close();self.repo.close();self.tmp.cleanup()

    def test_hourly_9_through_18_does_not_collect_teams_except_noon(self):
        for hour in range(9,19):self.ws.tick(self.noon.replace(hour=hour))
        self.assertEqual(10,self.mail.call_count)
        self.assertEqual(1,self.teams.call_count)
        self.assertEqual(['allowed-1','allowed-2'],self.teams.call_args.args[0])

    def test_manual_combined_collection_does_not_touch_teams(self):
        result=self.ws.collect(self.ws.settings(),self.noon)
        self.teams.assert_not_called();self.assertNotIn('teams',result)

    def test_noon_claim_survives_restart_and_duplicate_tick(self):
        self.ws.tick(self.noon)
        other=Workspace(self.store,self.base/'settings.json',self.repo,mail=self.mail,teams=self.teams)
        try:other.tick(self.noon+timedelta(minutes=1))
        finally:other.close()
        self.teams.assert_called_once()

    def test_busy_noon_is_skipped_without_later_retry(self):
        self.teams.return_value={'messages':[],'complete':False,'warnings':['teams_in_use_collection_deferred']}
        self.assertEqual('deferred',self.ws.tick(self.noon)['teams']['status'])
        for at in [self.noon+timedelta(minutes=1),self.noon.replace(hour=13),self.noon.replace(hour=16)]:self.ws.tick(at)
        self.teams.assert_called_once()
        row=self.store.connection.execute('SELECT * FROM teams_daily_runs').fetchone()
        self.assertEqual('deferred',row['status'])

    def test_failure_is_not_retried_same_day(self):
        self.teams.side_effect=RuntimeError('synthetic failure')
        self.assertEqual('failed',self.ws.tick(self.noon)['teams']['status'])
        self.ws.tick(self.noon+timedelta(minutes=1))
        self.teams.assert_called_once()
        self.assertEqual('collected',self.ws.tick(self.noon.replace(hour=13))['collection']['mail']['status'])

    def test_missed_noon_does_not_catch_up_on_afternoon_resume(self):
        self.ws.tick(self.noon+timedelta(minutes=5))
        self.ws.tick(self.noon.replace(hour=16))
        self.teams.assert_not_called()

    def test_next_weekday_collects_again_and_looks_back_seven_days(self):
        self.ws.tick(self.noon);self.ws.tick(self.noon+timedelta(days=1))
        self.assertEqual(2,self.teams.call_count)
        chats,start,end=self.teams.call_args.args
        self.assertEqual(timedelta(days=7),end-start)

    def test_weekend_or_disabled_never_collects(self):
        self.ws.tick(self.noon+timedelta(days=2))
        self.ws.save_settings({**self.ws.settings(),'collection_enabled':False})
        self.ws.tick(self.noon)
        self.mail.assert_not_called();self.teams.assert_not_called()

    def test_source_scope_escape_is_rejected_before_db_write(self):
        self.teams.return_value={'messages':[{'chat':'forbidden','source_id':'one','body':'synthetic'}]}
        result=self.ws.tick(self.noon)
        self.assertEqual('failed',result['teams']['status'])
        self.assertEqual([],self.store.history('teams:owner@example.invalid'))

    def test_korea_noon_is_not_utc_noon(self):
        from datetime import timezone
        self.assertIsNotNone(teams_slot(self.noon.astimezone(timezone.utc)))
        self.assertIsNone(teams_slot(datetime(2026,10,1,12,tzinfo=timezone.utc)))
        self.assertIsNone(collection_slot(self.noon.replace(hour=19)))
        with self.assertRaises(ValueError):teams_slot(datetime(2026,10,1,12))

    def test_gui_and_scheduled_worker_cannot_collect_concurrently(self):
        self.store.connection.execute("INSERT INTO workspace_jobs VALUES('manual','mail','running',?,NULL,'{}')",(datetime.now().astimezone().isoformat(),))
        with self.assertRaises(ValueError):self.ws.start('tick')
        self.teams.assert_not_called()

    def test_settings_cannot_store_api_key(self):
        with self.assertRaises(ValueError):self.ws.save_settings({'apiKey':'synthetic-never-store'})

    def test_old_but_live_collector_still_blocks_report(self):
        import os
        old=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        self.store.connection.execute("INSERT INTO workspace_jobs VALUES('old','mail','running',?,NULL,'{}')",(old,))
        self.store.connection.execute("INSERT INTO workspace_job_owners VALUES('old',?)",(os.getpid(),))
        with self.assertRaises(ValueError):self.ws.start('weekly')

    def test_dead_old_job_with_kst_timestamp_is_recovered(self):
        old=(datetime.now(KST)-timedelta(hours=1)).isoformat()
        self.store.connection.execute("INSERT INTO workspace_jobs VALUES('old-kst','mail','running',?,NULL,'{}')",(old,))
        self.store.connection.execute("INSERT INTO workspace_job_owners VALUES('old-kst',123)")
        with patch('ariadne.workspace.process_alive',return_value=False):self.ws.start('mail')
        self.ws.worker.join()
        row=self.store.connection.execute("SELECT status FROM workspace_jobs WHERE id='old-kst'").fetchone()
        self.assertEqual('interrupted',row['status'])

    def test_recent_history_on_old_hiworks_document_is_current_evidence(self):
        friday=datetime(2026,10,2,16,tzinfo=KST)
        self.ws.save_settings({**self.ws.settings(),'hiworks_company':'company.invalid'})
        self.store.ingest_messages('hiworks:company.invalid',[{'provider':'hiworks','source_id':'123',
            'subject':'Synthetic old document','body':'Historical source','timestamp':'2026-08-01T12:00:00+09:00',
            'history':['2026-09-30 14:00:00 Synthetic approval event']}])
        def runner(*args, **kwargs):return kwargs['extra_sources'](friday-timedelta(days=7),friday)[0]
        with patch('ariadne.workspace.run_weekly',side_effect=runner):
            result=self.ws.prepare_weekly(self.ws.settings(),friday+timedelta(minutes=15))
        self.assertEqual(1,len(result))
        self.assertEqual(['2026-09-30 14:00:00 Synthetic approval event'],result[0]['periodHistory'])
        self.assertFalse(result[0].get('contextOnly',False))

    def test_abandoned_dead_collector_can_be_recovered(self):
        old=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        self.store.connection.execute("INSERT INTO workspace_jobs VALUES('old','mail','running',?,NULL,'{}')",(old,))
        self.store.connection.execute("INSERT INTO workspace_job_owners VALUES('old',123)")
        with patch('ariadne.workspace.process_alive',return_value=False):self.ws.start('mail')
        self.ws.worker.join()
        row=self.store.connection.execute("SELECT status FROM workspace_jobs WHERE id='old'").fetchone()
        self.assertEqual('interrupted',row['status'])

    def test_friday_report_starts_1615_and_never_recollects_at_1615(self):
        self.ws.save_settings({**self.ws.settings(),'enabled':True})
        self.ws.prepare_weekly=Mock(return_value={'status':'prepared'})
        friday=self.noon.replace(day=2,hour=16)
        self.ws.tick(friday);self.ws.prepare_weekly.assert_not_called()
        self.ws.tick(friday+timedelta(minutes=14));self.ws.prepare_weekly.assert_not_called()
        self.ws.tick(friday+timedelta(minutes=15));self.ws.prepare_weekly.assert_called_once()
        self.mail.assert_called_once();self.teams.assert_not_called()

    def test_report_worker_gets_completed_collection_before_generating(self):
        self.ws.save_settings({**self.ws.settings(),'enabled':True})
        ordering=[]
        self.mail.side_effect=lambda *a:(ordering.append('collection') or {'messages':[],'complete':True,'warnings':[]})
        self.ws.prepare_weekly=lambda *a:(ordering.append('report') or {'status':'prepared'})
        friday=self.noon.replace(day=2,hour=16)
        self.ws.tick(friday);self.ws.tick(friday+timedelta(minutes=15))
        self.assertEqual(['collection','report'],ordering)

    def test_report_period_cutoff_is_not_shifted_by_delayed_generation(self):
        from ariadne.reporting import report_window
        friday=self.noon.replace(day=2,hour=16)
        self.assertEqual(friday,report_window(friday+timedelta(minutes=15))[1])

    def test_legacy_report_scheduler_also_waits_until_1615(self):
        from ariadne.weekly_worker import scheduled_tick
        friday=self.noon.replace(day=2,hour=16)
        with patch('ariadne.weekly_worker.run_weekly',return_value={'status':'prepared'}) as runner:
            self.assertEqual('not_due',scheduled_tick(self.store,self.ws.settings(),friday)['status'])
            runner.assert_not_called()
            self.assertEqual('prepared',scheduled_tick(self.store,self.ws.settings(),friday+timedelta(minutes=15))['status'])
