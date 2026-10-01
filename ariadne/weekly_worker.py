"""Durable Friday report pipeline; external delivery is at most one attempt.

COM Send acknowledges submission, not final delivery. An ambiguous outcome is
never retried automatically: review Outlook Outbox/Sent Items before releasing it.
"""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

from . import local_mail
from .config import data_directory
from .reporting import extract_report, report_window, write_report, validate_report


def run_weekly(store, settings, at=None, mail_collector=None, teams_collector=None,
               extractor=None, sender=None, extra_sources=None, identifier_suffix="", template_bytes=None):
    at = at or datetime.now(timezone.utc)
    if not settings['enabled']:
        return {'status':'disabled'}
    mailbox=settings['mailbox'].strip().casefold()
    if not mailbox:
        raise ValueError('Configure the owner mailbox first')
    start,end=report_window(at)
    suffix=hashlib.sha256(mailbox.encode()).hexdigest()[:12]
    identifier=f'{end:%Y-%m-%d}-{suffix}' + ('-'+identifier_suffix if identifier_suffix else '')
    if not store.claim_run(identifier,start.isoformat(),end.isoformat()):
        return {'id':identifier,'status':'already_claimed'}
    warnings=[]
    delivery_started=False
    output=None;output_version=None
    try:
        mail=(mail_collector or local_mail.collect)(mailbox,start,end)
        warnings += mail.get('warnings',[])
        if not mail.get('complete'):
            warnings.append('mail_collection_incomplete')
        messages=mail['messages']
        # Validate absolute mail timestamps; malformed data cannot silently enter a week.
        for message in messages:
            stamp=datetime.fromisoformat(message['timestamp'])
            if stamp.tzinfo is None or not start<=stamp<end:
                raise ValueError('Collected mail timestamp is outside the report period')
        store.ingest_messages('outlook:'+mailbox,messages)
        try:
            # Friday reporting never navigates Teams or simulates keyboard input.
            teams=teams_collector(settings['teams_chats']) if teams_collector else {
                'messages':[],'complete':not settings['teams_chats'],
                'warnings':['teams_archive_required'] if settings['teams_chats'] else []}
        except RuntimeError:
            teams={'messages':[],'complete':False,'warnings':['teams_local_collection_unavailable']}
        warnings += teams.get('warnings',[])
        if not teams.get('complete'):
            warnings.append('teams_collection_incomplete')
        store.ingest_messages('teams:'+mailbox,teams['messages'])
        messages=messages+teams['messages']
        if extra_sources:
            context,context_warnings=extra_sources(start,end)
            messages+=context;warnings+=context_warnings
        if warnings and not settings['allow_partial_report']:
            store.set_run(identifier,'failed',detail={'warnings':warnings,'reason':'incomplete_sources'})
            return {'id':identifier,'status':'incomplete_sources','warnings':warnings}
        if not messages:
            raise ValueError('No source messages found; no unsupported report will be generated')
        if all(m.get('contextOnly') for m in messages):
            raise ValueError('Only historical references or future calendar candidates available; current-period evidence required')
        report,sources=(extractor or extract_report)(messages,settings['model'],settings['max_input_chars'])
        if any(s.get('analysisChunks',1)>1 for s in sources):warnings.append('ai_input_processed_in_chunks')
        validate_report(report,{source['evidence_id'] for source in sources})
        output=data_directory()/'reports'/f'weekly-{identifier}.xlsx'
        write_report(output,report,sources,start,end,warnings)
        if settings.get('report_template'):
            from .meeting_report import write_meeting_report
            write_meeting_report(output,template_bytes if template_bytes is not None else settings['report_template'],report,sources,start,end,warnings)
        output_version=store.archive_bytes('reports:'+mailbox,output.name,output.read_bytes(),{'kind':'generated_report'})
        from .hiworks import DraftStore, prepare_weekly
        draft = DraftStore(data_directory() / "hiworks.sqlite3").save(
            prepare_weekly(report, sources, start, end, warnings))
        store.set_run(identifier,'prepared',output,{'warnings':warnings,'hiworksDraftId':draft['id'],'outputVersionId':output_version})
        if not settings['auto_send']:
            return {'id':identifier,'status':'prepared','warnings':warnings,'output':str(output),'hiworksDraftId':draft['id']}
        # Persist the uncertainty boundary BEFORE calling the external transport.
        store.set_run(identifier,'sending',output,{'warnings':warnings,'outputVersionId':output_version})
        delivery_started=True
        try:
            receipt=(sender or local_mail.send_to_self)(mailbox,output,identifier)
            if not receipt.get('submitted'):
                raise RuntimeError('Outlook submission was not acknowledged')
        except Exception:
            store.set_run(identifier,'delivery_unknown',output,{'warnings':warnings,'outputVersionId':output_version})
            return {'id':identifier,'status':'delivery_unknown','output':str(output)}
        store.set_run(identifier,'submitted',output,{'warnings':warnings,'delivered':False,'outputVersionId':output_version})
        return {'id':identifier,'status':'submitted','output':str(output)}
    except Exception as error:
        # No bodies, credentials, server responses or COM stderr in durable logs.
        # Even a database failure after Send must never make this run retryable.
        # The already committed 'sending' state remains protective if this write fails.
        store.set_run(identifier,'delivery_unknown' if delivery_started else 'failed',output,
                      detail={'error_type':type(error).__name__,'warnings':warnings,'outputVersionId':output_version})
        raise


def scheduled_tick(store, settings, at=None, **dependencies):
    at=at or datetime.now(timezone.utc)
    _,end=report_window(at)
    # Legacy callers follow the same 16:15 start as the unified scheduler.
    if at<end+timedelta(minutes=15) or (at-end).total_seconds()>3*24*3600:
        return {'status':'not_due'}
    return run_weekly(store,settings,at,**dependencies)
