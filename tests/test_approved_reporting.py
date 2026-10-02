"""Approved reports are immutable, sent once, and completed only by receipt.

All mail, extraction and delivery dependencies are synthetic. No COM or network
call is permitted in these regressions.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from ariadne.automation_store import AutomationStore
from ariadne.local_settings import DEFAULTS
from ariadne.reporting import KST
from ariadne import weekly_worker as worker


class ApprovedReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'ARIADNE_DATA_DIR': str(self.base)})
        self.environment.start()
        self.store = AutomationStore(self.base / 'archive.sqlite3')
        self.settings = {**DEFAULTS, 'mailbox': 'owner@example.invalid', 'enabled': True,
                         'auto_send': False, 'teams_chats': [], 'model': 'synthetic-model'}
        self.at = datetime(2026, 10, 2, 16, tzinfo=KST)
        self.message = {'source_id': 'synthetic-mail', 'provider': 'synthetic', 'direction': 'sent',
                        'timestamp': '2026-10-01T02:00:00Z', 'subject': 'Synthetic report evidence',
                        'body': 'Sent the materials; confirmation is still required.'}
        self.report = {'items': [{'project': 'Synthetic fund', 'work': 'Material delivery',
                                 'result': 'Materials sent, confirmation required',
                                 'issues': 'Confirmation required', 'next_week': 'Check receipt',
                                 'evidence_ids': ['S1']}]}
        self.calls = []
        self.extractor = Mock(return_value=(self.report, [{**self.message, 'evidence_id': 'S1'}]))

    def tearDown(self):
        self.store.close()
        self.environment.stop()
        self.temporary.cleanup()

    def row(self, identifier):
        row = next(row for row in self.store.runs() if row['id'] == identifier)
        return {**row, 'detail': json.loads(row['detail'])}

    def prepare(self, *, mail=None, settings=None, scope_acknowledged=False, strict_sources=True,
                identifier_suffix=None, sender=None):
        if mail is None:
            mail = {'messages': [self.message], 'complete': True, 'warnings': [], 'limitations': []}
        return worker.run_weekly(
            self.store, settings or self.settings, self.at,
            mail_collector=lambda *args: mail,
            teams_collector=lambda *args: {'messages': [], 'complete': True, 'warnings': []},
            extractor=self.extractor, sender=sender or self.sender,
            identifier_suffix=identifier_suffix if identifier_suffix is not None else 'validation-' + str(uuid4()),
            strict_sources=strict_sources, scope_acknowledged=scope_acknowledged)

    def sender(self, recipient, attachment, identifier):
        row = self.row(identifier)
        self.assertEqual(row['status'], 'sending')
        self.assertEqual(row['detail']['approvedHash'], row['detail']['outputHash'])
        self.assertIsNotNone(datetime.fromisoformat(row['detail']['sendingStartedAt']).tzinfo)
        self.calls.append({'recipient': recipient, 'bytes': Path(attachment).read_bytes(),
                           'name': Path(attachment).name, 'path': Path(attachment), 'id': identifier})
        return {'submitted': True, 'delivered': False}

    def send(self, prepared, **kwargs):
        return worker.send_prepared(self.store, self.settings, prepared['id'],
                                    prepared['outputHash'], sender=kwargs.get('sender', self.sender))

    @staticmethod
    def matched(since, **changes):
        return {'status': 'confirmed', 'delivered': True, 'reason': 'matched',
                'receivedAt': (since + timedelta(seconds=1)).isoformat(), 'id': 'a' * 64, **changes}

    def test_explicit_hash_approval_is_required_and_does_not_consume_bad_requests(self):
        prepared = self.prepare()
        for digest in (None, '', 'g' * 64, '0' * 63, '0' * 64, 42):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                worker.send_prepared(self.store, self.settings, prepared['id'], digest, sender=self.sender)
            self.assertEqual(self.row(prepared['id'])['status'], 'prepared')
        self.assertFalse(self.calls)
        self.assertEqual(self.send(prepared)['status'], 'submitted')
        self.assertEqual(len(self.calls), 1)

    def test_owner_run_identifier_and_kind_are_pinned(self):
        prepared = self.prepare()
        wrong_owner = {**self.settings, 'mailbox': 'other@example.invalid'}
        with self.assertRaises(ValueError):
            worker.send_prepared(self.store, wrong_owner, prepared['id'], prepared['outputHash'], sender=self.sender)
        for identifier in (None, 42, 'missing', prepared['id'].replace('-validation-', '-preview-')):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                worker.send_prepared(self.store, self.settings, identifier, prepared['outputHash'], sender=self.sender)
        row = self.row(prepared['id'])
        self.store.set_run(prepared['id'], 'prepared', row['output'], {**row['detail'], 'kind': 'preview'})
        with self.assertRaises(ValueError):
            self.send(prepared)
        self.assertFalse(self.calls)
        self.assertEqual(self.row(prepared['id'])['status'], 'prepared')

    def test_deleted_or_changed_local_report_invalidates_approval(self):
        for operation in ('deleted', 'changed'):
            with self.subTest(operation=operation):
                prepared = self.prepare()
                output = Path(prepared['output'])
                if operation == 'deleted':
                    output.unlink()
                else:
                    original = output.read_bytes()
                    output.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                with self.assertRaises(ValueError):
                    self.send(prepared)
                self.assertEqual(self.row(prepared['id'])['status'], 'prepared')
        self.assertFalse(self.calls)

    def test_sender_receives_pinned_archived_bytes_despite_later_local_edit(self):
        prepared = self.prepare()
        output = Path(prepared['output'])
        approved = self.store.read_version(prepared['outputVersionId'])

        def edit_during_send(recipient, attachment, identifier):
            self.assertNotEqual(Path(attachment).resolve(), output.resolve())
            output.write_bytes(b'later edit')
            return self.sender(recipient, attachment, identifier)

        result = self.send(prepared, sender=edit_during_send)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(self.calls[0]['bytes'], approved)
        self.assertEqual(self.calls[0]['name'], output.name)
        self.assertEqual(hashlib.sha256(self.calls[0]['bytes']).hexdigest(), prepared['outputHash'])
        self.assertFalse(self.calls[0]['path'].exists())
        self.assertEqual(self.calls[0]['recipient'], self.settings['mailbox'])

    def test_two_store_connections_and_duplicate_click_only_send_once(self):
        prepared = self.prepare()
        other_store = AutomationStore(self.base / 'archive.sqlite3')
        barrier = threading.Barrier(2)

        def attempt(store):
            barrier.wait(timeout=5)
            try:
                return worker.send_prepared(store, self.settings, prepared['id'],
                                            prepared['outputHash'], sender=self.sender)['status']
            except ValueError:
                return 'blocked'

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(attempt, store) for store in (self.store, other_store)]
                results = [future.result(timeout=10) for future in futures]
        finally:
            other_store.close()
        self.assertCountEqual(results, ['submitted', 'blocked'])
        with self.assertRaises(ValueError):
            self.send(prepared)
        self.assertEqual(len(self.calls), 1)

    def test_uncertain_sender_outcome_preserves_the_send_claim(self):
        prepared = self.prepare()

        def uncertain(*args):
            self.calls.append('attempted')
            raise TimeoutError('private mail or transport response')

        self.assertEqual(self.send(prepared, sender=uncertain)['status'], 'delivery_unknown')
        with self.assertRaises(ValueError):
            self.send(prepared)
        self.assertFalse(self.store.claim_run(prepared['id'], 'start', 'end'))
        with self.assertRaises(ValueError):
            self.store.release_failed_collection(prepared['id'])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('private', json.dumps(self.row(prepared['id'])['detail']))

    def test_database_failure_after_send_cannot_make_the_report_retryable(self):
        prepared = self.prepare()
        original = self.store.set_run

        def fail_after_submit(identifier, status, *args, **kwargs):
            if status == 'submitted':
                raise OSError('Synthetic post-send database failure')
            return original(identifier, status, *args, **kwargs)

        with patch.object(self.store, 'set_run', side_effect=fail_after_submit):
            try:
                self.send(prepared)
            except OSError:
                pass
        self.assertIn(self.row(prepared['id'])['status'], {'sending', 'delivery_unknown'})
        with self.assertRaises(ValueError):
            self.send(prepared)
        self.assertFalse(self.store.claim_run(prepared['id'], 'start', 'end'))
        self.assertEqual(len(self.calls), 1)

    def test_pending_or_unavailable_receipt_never_releases_the_send_claim(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                prepared = self.prepare()
                self.send(prepared)

                def lookup(*args):
                    if unavailable:
                        raise RuntimeError('private Outlook error body')
                    return {'status': 'pending', 'delivered': False, 'reason': 'not_found',
                            'receivedAt': None, 'id': None}

                result = worker.confirm_delivery_run(self.store, self.settings, prepared['id'], confirmer=lookup)
                self.assertEqual(result['status'], 'delivery_review_required' if unavailable else 'delivery_pending')
                self.assertEqual(self.row(prepared['id'])['status'], 'submitted')
                with self.assertRaises(ValueError):
                    self.send(prepared)
                self.assertFalse(self.store.claim_run(prepared['id'], 'start', 'end'))
                self.assertNotIn('private', json.dumps(self.row(prepared['id'])['detail']))
        self.assertEqual(len(self.calls), 2)

    def test_receipt_uses_archived_attachment_and_floored_aware_submission_time(self):
        prepared = self.prepare()
        approved = self.store.read_version(prepared['outputVersionId'])
        self.send(prepared)
        row = self.row(prepared['id'])
        started = datetime.fromisoformat(row['detail']['sendingStartedAt']).replace(microsecond=456789)
        self.store.set_run(prepared['id'], row['status'], row['output'],
                           {**row['detail'], 'sendingStartedAt': started.isoformat()})
        Path(prepared['output']).unlink()

        def lookup(recipient, attachment, identifier, since):
            self.assertEqual(since, started.astimezone(timezone.utc).replace(microsecond=0))
            self.assertEqual(since.microsecond, 0)
            self.assertEqual(Path(attachment).read_bytes(), approved)
            self.assertEqual(Path(attachment).name, Path(prepared['output']).name)
            self.assertEqual(identifier, prepared['id'])
            self.assertEqual(recipient, self.settings['mailbox'])
            return self.matched(since)

        result = worker.confirm_delivery_run(self.store, self.settings, prepared['id'], confirmer=lookup)
        self.assertEqual(result['status'], 'delivered')
        self.assertIsNotNone(datetime.fromisoformat(result['receivedAt']).tzinfo)
        self.assertTrue(self.row(prepared['id'])['detail']['delivered'])
        with self.assertRaises(ValueError):
            self.send(prepared)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_unaware_old_or_future_receipt_cannot_complete_the_report(self):
        prepared = self.prepare()
        self.send(prepared)
        since = datetime.fromisoformat(self.row(prepared['id'])['detail']['sendingStartedAt']).replace(microsecond=0)
        dates = (None, 'not-an-ISO-date', since.replace(tzinfo=None).isoformat(),
                 (since - timedelta(seconds=1)).isoformat(),
                 (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
        for received in dates:
            with self.subTest(received=received):
                result = worker.confirm_delivery_run(
                    self.store, self.settings, prepared['id'],
                    confirmer=lambda *args: self.matched(since, receivedAt=received))
                self.assertEqual(result['status'], 'delivery_review_required')
                self.assertEqual(self.row(prepared['id'])['status'], 'submitted')
                self.assertFalse(self.row(prepared['id'])['detail'].get('delivered'))
        self.assertEqual(len(self.calls), 1)

    def test_receipt_confirmed_while_sender_returns_cannot_be_overwritten(self):
        prepared = self.prepare()

        def delayed_submission(recipient, attachment, identifier):
            result = worker.confirm_delivery_run(
                self.store, self.settings, identifier,
                confirmer=lambda owner, file, run_id, since: self.matched(since))
            self.assertEqual(result['status'], 'delivered')
            self.calls.append({'bytes': Path(attachment).read_bytes()})
            return {'submitted': True}

        result = self.send(prepared, sender=delayed_submission)
        self.assertEqual(result['status'], 'delivered')
        self.assertEqual(self.row(prepared['id'])['status'], 'delivered')
        self.assertEqual(len(self.calls), 1)

    def test_confirmed_receipt_survives_later_ambiguous_sender_exception(self):
        prepared = self.prepare()

        def timeout_after_receipt(recipient, attachment, identifier):
            result = worker.confirm_delivery_run(
                self.store, self.settings, identifier,
                confirmer=lambda owner, file, run_id, since: self.matched(since))
            self.assertEqual(result['status'], 'delivered')
            self.calls.append('attempted')
            raise TimeoutError('Synthetic timeout after receiver already verified delivery')

        result = self.send(prepared, sender=timeout_after_receipt)
        self.assertEqual(result['status'], 'delivered')
        self.assertEqual(self.row(prepared['id'])['status'], 'delivered')
        self.assertTrue(self.row(prepared['id'])['detail']['delivered'])
        with self.assertRaises(ValueError):
            self.send(prepared)
        self.assertEqual(len(self.calls), 1)

    def test_malformed_lookup_output_is_review_required_without_releasing_send(self):
        prepared = self.prepare()
        self.send(prepared)
        for payload in (None, True, 'private raw response', ['private raw response']):
            with self.subTest(payload=payload):
                result = worker.confirm_delivery_run(
                    self.store, self.settings, prepared['id'], confirmer=lambda *args: payload)
                self.assertEqual(result['status'], 'delivery_review_required')
                self.assertEqual(self.row(prepared['id'])['status'], 'submitted')
                self.assertNotIn('private', json.dumps(self.row(prepared['id'])['detail']))
                with self.assertRaises(ValueError):
                    self.send(prepared)
        self.assertEqual(len(self.calls), 1)

    def test_strict_collection_failures_cannot_be_bypassed_by_ack_or_global_partial_mode(self):
        settings = {**self.settings, 'allow_partial_report': True}
        mail = {'messages': [self.message], 'complete': False, 'warnings': ['scan_failed'],
                'limitations': ['Limited archive scope']}
        result = self.prepare(mail=mail, settings=settings, scope_acknowledged=True)
        self.assertEqual(result['status'], 'incomplete_sources')
        self.assertEqual(self.row(result['id'])['status'], 'failed')
        self.extractor.assert_not_called()
        self.assertFalse(self.calls)

    def test_known_scope_limit_requires_per_run_ack_and_keeps_global_setting_unchanged(self):
        settings = {**self.settings, 'allow_partial_report': True}
        mail = {'messages': [self.message], 'complete': True, 'warnings': [],
                'limitations': ['Configured archive scope only']}
        blocked = self.prepare(mail=mail, settings=settings)
        self.assertEqual(blocked['status'], 'scope_confirmation_required')
        self.extractor.assert_not_called()
        acknowledged = self.prepare(mail=mail, settings=settings, scope_acknowledged=True)
        self.assertEqual(acknowledged['status'], 'prepared')
        self.assertEqual(acknowledged['limitations'], mail['limitations'])
        self.assertTrue(self.row(acknowledged['id'])['detail']['scopeAcknowledged'])
        self.assertEqual(settings['allow_partial_report'], True)
        again = self.prepare(mail=mail, settings=settings)
        self.assertEqual(again['status'], 'scope_confirmation_required')
        self.assertEqual(self.extractor.call_count, 1)
        self.assertFalse(self.calls)

    def test_validation_preparation_cannot_send_and_does_not_claim_official_run(self):
        prepared = self.prepare()
        self.assertEqual(prepared['status'], 'prepared')
        self.assertFalse(self.calls)
        with self.assertRaises(ValueError):
            self.prepare(settings={**self.settings, 'auto_send': True})
        self.assertFalse(self.calls)
        official = self.prepare(identifier_suffix='', strict_sources=False)
        self.assertEqual(official['status'], 'prepared')
        self.assertNotEqual(official['id'], prepared['id'])
        self.assertNotIn('-validation-', official['id'])
        self.assertEqual(self.row(prepared['id'])['status'], 'prepared')
        self.assertEqual(self.row(official['id'])['detail']['kind'], 'weekly')


if __name__ == '__main__':
    unittest.main()
