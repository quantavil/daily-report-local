"""Synthetic Gmail discovery: exact sender, identity, and revision preservation."""
import base64
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from daily_report.gmail import GmailClient, GmailError
from daily_report.models import BillIdentity

FILENAME = '754640407102026INMBD6SB22081020260915.pdf'
IDENTITY = BillIdentity(sb_no='7546404', sb_date=date(2026, 10, 7), port_code='INMBD6')


def message(sender='ICEGATE <noreply@icegate.gov.in>', labels=None, filename=FILENAME):
    return {'labelIds': labels or [], 'payload': {'headers': [{'name': 'From', 'value': sender}],
        'parts': [{'mimeType': 'multipart/mixed', 'parts': [
            {'partId': '1.0', 'filename': filename, 'body': {'attachmentId': 'blob'}},
            {'partId': '1.1', 'filename': filename, 'body': {'data': base64.urlsafe_b64encode(b'%PDF-inline').decode()}}
        ]}]}}


def client(pages, messages):
    service = Mock()
    api = service.users.return_value.messages.return_value
    api.list.return_value.execute.side_effect = pages
    api.get.return_value.execute.side_effect = lambda: None
    api.get.side_effect = lambda **kwargs: Mock(execute=lambda: messages[kwargs['id']])
    return GmailClient(Path('credentials.json'), Path('token.json'), service=service), api


def test_discover_paginates_inspects_nested_inline_and_keeps_revisions():
    gmail, api = client([{'messages': [{'id': 'one'}], 'nextPageToken': 'next'},
                         {'messages': [{'id': 'two'}]}], {'one': message(), 'two': message()})
    found = gmail.discover(IDENTITY, {'SB22'})
    assert len(found) == 4
    assert {a.message_id for a in found} == {'one', 'two'}
    inline = next(a for a in found if a.inline_data)
    assert inline.part_id == '1.1'
    assert gmail.download(inline) == b'%PDF-inline'
    calls = api.list.call_args_list
    assert all('from:noreply@icegate.gov.in' in call.kwargs['q'] for call in calls)
    assert all('-in:spam' in call.kwargs['q'] and '-in:trash' in call.kwargs['q'] for call in calls)
    assert '"7546404"' in calls[0].kwargs['q']
    assert 'filename:' not in calls[0].kwargs['q']
    assert calls[1].kwargs['pageToken'] == 'next'
    assert calls[0].kwargs['includeSpamTrash'] is False


@pytest.mark.parametrize('sender', ['noreply@icegate.gov.in.evil.test',
    'attacker@example.com (noreply@icegate.gov.in)',
    'noreply@icegate.gov.in, attacker@example.com', 'attacker@example.com', ''])
def test_sender_metadata_must_be_exact_single_mailbox(sender):
    gmail, _ = client([{'messages': [{'id': 'one'}]}], {'one': message(sender=sender)})
    assert gmail.discover(IDENTITY, {'SB22'}) == []


@pytest.mark.parametrize('labels', [['SPAM'], ['TRASH']])
def test_discovery_rejects_spam_and_trash_metadata(labels):
    gmail, _ = client([{'messages': [{'id': 'one'}]}], {'one': message(labels=labels)})
    assert gmail.discover(IDENTITY, {'SB22'}) == []


@pytest.mark.parametrize('filename', ['754640407102026INMBD6SB21081020260915.pdf',
    '754640507102026INMBD6SB22081020260915.pdf',
    '754640408102026INMBD6SB22081020260915.pdf',
    '754640407102026INMBD7SB22081020260915.pdf', '7546404broken.pdf'])
def test_attachment_filename_determines_match(filename):
    gmail, _ = client([{'messages': [{'id': 'one'}]}], {'one': message(filename=filename)})
    assert gmail.discover(IDENTITY, {'SB22'}) == []


def test_explicit_document_date_narrows_local_attachment_match_not_server_search():
    gmail, api = client([{'messages': [{'id': 'one'}, {'id': 'two'}]}],
        {'one': message(), 'two': message(filename='754640407102026INMBD6SB22091020260915.pdf')})
    found = gmail.discover(IDENTITY.model_copy(update={'document_date': date(2026, 10, 8)}), {'SB22'})
    assert len(found) == 2
    assert all(a.metadata.document_at.date() == date(2026, 10, 8) for a in found)
    assert '"7546404"' in api.list.call_args.kwargs['q']
    assert 'filename:' not in api.list.call_args.kwargs['q']
    assert '08102026' not in api.list.call_args.kwargs['q']


def test_service_failure_is_error_not_missing_documents():
    gmail, api = client([], {})
    api.list.return_value.execute.side_effect = RuntimeError('unavailable')
    with pytest.raises(GmailError, match='discovery'):
        gmail.discover(IDENTITY, {'SB22'})


def test_remote_attachment_download_and_api_failure():
    gmail, api = client([{'messages': [{'id': 'one'}]}], {'one': message()})
    attachment = next(a for a in gmail.discover(IDENTITY, {'SB22'}) if a.attachment_id)
    endpoint = api.attachments.return_value.get.return_value
    endpoint.execute.return_value = {'data': base64.urlsafe_b64encode(b'%PDF-remote').decode()}
    assert gmail.download(attachment) == b'%PDF-remote'
    endpoint.execute.side_effect = RuntimeError('connection failed')
    with pytest.raises(GmailError, match='download'):
        gmail.download(attachment)


def test_credential_automatic_refresh_is_serialized(tmp_path, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta

    from google.oauth2.credentials import Credentials

    token = tmp_path / 'token.json'
    token.write_text(Credentials(token='synthetic', refresh_token='synthetic',
        token_uri='https://oauth2.googleapis.com/token', client_id='synthetic',
        client_secret='synthetic', expiry=datetime.now() + timedelta(hours=1),
        scopes=['https://www.googleapis.com/auth/gmail.readonly']).to_json())
    gmail = GmailClient(tmp_path / 'credentials.json', token)
    gmail.authenticate()
    gmail._credentials.expiry = datetime.now() - timedelta(hours=1)
    guard = threading.Lock()
    active = 0
    peak = 0
    def refresh(credentials, request):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        credentials.expiry = datetime.now() + timedelta(hours=1)
        with guard:
            active -= 1
    monkeypatch.setattr(Credentials, 'refresh', refresh)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: gmail._credentials.refresh(None), range(8)))
    assert peak == 1


@pytest.mark.parametrize('header', ['X-Forwarded-For', 'X-Forwarded-To', 'Resent-From', 'Resent-To'])
def test_forwarded_transport_metadata_is_rejected_even_with_original_sender(header):
    forwarded = message()
    forwarded['payload']['headers'].append({'name': header, 'value': 'forwarder@example.com'})
    gmail, _ = client([{'messages': [{'id': 'one'}]}], {'one': forwarded})
    assert gmail.discover(IDENTITY, {'SB22'}) == []


def test_known_date_search_includes_canonical_combined_subject_token():
    gmail, api = client([{'messages': [{'id': 'one'}]}], {'one': message()})
    assert len(gmail.discover(IDENTITY, {'SB22'})) == 2
    query = api.list.call_args.kwargs['q']
    assert '{"7546404" "No7546404Dt07102026"}' in query
    assert 'filename:' not in query


def test_unknown_date_searches_approved_sender_without_sb_word_restriction():
    unknown = BillIdentity(sb_no='7546404', port_code='INMBD6')
    gmail, api = client([{'messages': [{'id': 'one'}]}], {'one': message()})
    assert len(gmail.discover(unknown, {'SB22'})) == 2
    query = api.list.call_args.kwargs['q']
    assert 'from:noreply@icegate.gov.in' in query
    assert '7546404' not in query
    assert 'filename:' not in query


def test_missing_mime_part_ids_use_stable_distinct_structural_ordinals():
    first = message()
    second = message()
    for data in (first, second):
        for part in data['payload']['parts'][0]['parts']:
            part['partId'] = ''
    first['payload']['parts'][0]['parts'][1]['body'] = {'attachmentId': 'other-v1'}
    second['payload']['parts'][0]['parts'][0]['body']['attachmentId'] = 'blob-v2'
    second['payload']['parts'][0]['parts'][1]['body'] = {'attachmentId': 'other-v2'}
    gmail, api = client([{'messages': [{'id': 'one'}]}, {'messages': [{'id': 'one'}]}], {'one': first})
    initial = gmail.discover(IDENTITY, {'SB22'})
    api.get.side_effect = lambda **kwargs: Mock(execute=lambda: second)
    refreshed = gmail.discover(IDENTITY, {'SB22'})
    assert len(initial) == len(refreshed) == 2
    assert len({a.part_id for a in initial}) == 2
    assert all(a.part_id for a in initial)
    assert {a.key for a in initial} == {a.key for a in refreshed}
    assert {a.attachment_id for a in refreshed} == {'blob-v2', 'other-v2'}
