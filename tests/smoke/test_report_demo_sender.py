"""Offline single-use demo sender tests; no real services or Telegram calls."""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.report_bot import demo_sender as sender

RUN = 'b' * 32
JOB = {'run_id':RUN, 'families':sorted(sender.FAMILIES)}


class Redis:
    def __init__(self): self.values = {}; self.calls = []
    async def get(self, key): return self.values.get(key)
    async def set(self, key, value, **kwargs):
        self.calls.append((key, kwargs))
        if kwargs.get('nx') and key in self.values: return None
        if kwargs.get('xx') and key not in self.values: return None
        self.values[key] = value
        return True


class SenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = Redis()
        self.rows = [{'family':f, 'chat_id':-1000-i} for i,f in enumerate(sorted(sender.FAMILIES))]
        self.readonly = []
        self.queries = []
        @asynccontextmanager
        async def transaction(**kwargs):
            self.readonly.append(kwargs)
            yield
        async def fetch(query, families):
            self.queries.append(query)
            self.assertEqual(set(families), sender.FAMILIES)
            return self.rows
        conn = SimpleNamespace(transaction=transaction, fetch=fetch)
        @asynccontextmanager
        async def acquire(): yield conn
        self.pool = SimpleNamespace(acquire=acquire)
        self.client = SimpleNamespace(is_connected=lambda:True, send_file=AsyncMock(side_effect=self.send))
        self.service = SimpleNamespace(_client=self.client)
        self.sent, self.fail_anchor, self.fail_album = [], None, None
        self.partial = False
        self.counter = 100
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.assets = Path(temporary.name)
        username = 'DEMO_TEST_' + '0' * 40
        ctx = {'account_id':0,'dialog_id':0,'session_id':'TEST-DEMO','telegram_user_id':990000000000000001,
               'started_at':'2026-11-03T08:00:00+00:00','finished_at':'2026-11-03T08:01:00+00:00',
               'partner_username':username,'target_username':username,'partner_first_name':'Тест',
               'partner_last_name':'Демо','worker_phone':''}
        messages = [{'id':1,'is_outgoing':False,'date':'2026-11-03T08:00:00+00:00',
                     'text':'ТЕСТ @'+username,'type':'regular','has_media':False}]
        captions = {'hidden':'🧪 ТЕСТ. Контакт скрыт','revealed':'🧪 ТЕСТ. @'+username,'username':username}
        for name,data in (('ctx.json',ctx),('messages.json',messages),('captions.json',captions)):
            (self.assets/name).write_text(json.dumps(data),encoding='utf-8')
        for i in range(1,11): (self.assets/f'photo_{i:02d}.jpg').write_bytes(b'\xff\xd8\xff'+bytes([i]))
        for value in (patch.object(sender,'FIXTURE_ROOT',self.assets), patch.object(sender,'GROUP_DELAY_SEC',0),
                      patch.object(sender.redis_mod,'get_client',return_value=self.redis),
                      patch.object(sender.db,'get_pool',return_value=self.pool)):
            value.start(); self.addCleanup(value.stop)

    async def send(self, chat, file, **kwargs):
        run = json.loads(self.redis.values['report_demo_run:'+RUN])
        row = next(r for r in run['groups'] if r['chat_id']==chat)
        album = isinstance(file, list)
        self.assertEqual(row['phase'], 'sending_album' if album else 'sending_anchor')
        self.sent.append({'chat':chat,'album':album,'file':file,'kwargs':kwargs})
        error = self.fail_album if album else self.fail_anchor
        if error and chat == self.rows[0]['chat_id']: raise error
        self.counter += 20
        if album:
            count = 3 if self.partial and chat==self.rows[0]['chat_id'] else 10
            return [SimpleNamespace(id=self.counter+i, grouped_id=self.counter*100) for i in range(count)]
        return SimpleNamespace(id=self.counter)

    async def test_all_groups_use_existing_client_and_real_anonymized_html(self):
        result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(len(result['groups']), 13)
        self.assertEqual(len(self.sent), 26)
        self.assertEqual(self.readonly, [{'readonly':True}])
        self.assertIn('EXISTS(SELECT 1 FROM accounts', self.queries[0])
        for row in result['groups']:
            state = json.loads(self.redis.values['demo_report:'+row['nonce']])
            self.assertEqual((state['chat_id'],state['message_id']), (row['chat_id'],row['message_id']))
            self.assertEqual(row['photo_count'], 10)
            self.assertEqual(len(row['photo_message_ids']), 10)
            self.assertTrue(row['grouped_id'])
        for entry in self.sent:
            kwargs = entry['kwargs']
            self.assertIsNone(kwargs['parse_mode'])
            if entry['album']:
                self.assertEqual([f.name for f in entry['file']], [f'photo_{i:02d}.jpg' for i in range(1,11)])
                self.assertFalse(kwargs['force_document'])
                self.assertGreater(kwargs['reply_to'], 0)
            else:
                self.assertEqual(entry['file'].name,'report_test.html')
                blob = entry['file'].getvalue()
                self.assertIn(b'<!doctype html>',blob)
                self.assertNotIn(b'DEMO_TEST_',blob)
                self.assertNotIn(b'990000000000000001',blob)
                self.assertTrue(kwargs['caption'].startswith('🧪 ТЕСТ'))
                self.assertTrue(all(b.data.startswith(b'demo:') for b in kwargs['buttons'][0]))

    async def test_repeat_run_returns_receipt_without_more_sends_or_queries(self):
        first = await sender.send_demo_batch(self.service, JOB)
        second = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(first, second)
        self.assertEqual(len(self.sent), 26)
        self.assertEqual(len(self.queries), 1)

    async def test_existing_claim_without_ledger_does_not_send(self):
        self.redis.values['report_demo_claim:'+RUN] = b'1'
        result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(result['phase'],'already_claimed')
        self.assertEqual(self.sent, [])

    async def test_duplicate_group_ids_are_sent_only_once(self):
        self.rows[1]['chat_id'] = self.rows[0]['chat_id']
        result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(len(result['groups']),12)
        self.assertEqual(len(self.sent),24)
        self.assertEqual(len(result['groups'][0]['families']),2)

    async def test_anchor_timeout_is_uncertain_no_retry_other_groups_continue(self):
        self.fail_anchor = asyncio.TimeoutError()
        result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(result['phase'],'partial')
        self.assertEqual(result['groups'][0]['phase'],'uncertain')
        self.assertIsNone(result['groups'][0]['message_id'])
        self.assertEqual(sum(e['chat']==self.rows[0]['chat_id'] for e in self.sent),1)
        self.assertEqual(sum(r['phase']=='sent' for r in result['groups']),12)
        await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(len(self.sent),25)

    async def test_album_timeout_preserves_anchor_no_resend(self):
        self.fail_album = asyncio.TimeoutError()
        result = await sender.send_demo_batch(self.service, JOB)
        row = result['groups'][0]
        self.assertEqual(row['phase'],'uncertain')
        self.assertGreater(row['message_id'],0)
        self.assertEqual(row['photo_count'],0)
        self.assertEqual(sum(e['chat']==row['chat_id'] for e in self.sent),2)
        await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(len(self.sent),26)

    async def test_partial_album_is_not_called_complete_or_retried(self):
        self.partial = True
        result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(result['groups'][0]['phase'],'uncertain')
        self.assertEqual(result['groups'][0]['photo_count'],3)
        self.assertEqual(len(result['groups'][0]['photo_message_ids']),3)

    async def test_cancellation_preserves_claim_and_uncertain_phase(self):
        self.fail_album = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await sender.send_demo_batch(self.service, JOB)
        result = json.loads(self.redis.values['report_demo_run:'+RUN])
        self.assertEqual(result['phase'],'cancelled')
        self.assertEqual(result['groups'][0]['phase'],'uncertain')
        await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(len(self.sent),2)

    async def test_missing_or_nonphoto_fixture_fails_before_sends(self):
        (self.assets/'photo_01.jpg').write_bytes(b'video')
        result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(result['phase'],'failed')
        self.assertEqual(self.sent,[])

    async def test_missing_family_mapping_fails_before_sends(self):
        self.rows.pop()
        result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(result['phase'],'failed')
        self.assertEqual(self.sent,[])

    async def test_duplicate_normalized_family_cannot_add_extra_group(self):
        self.rows.append({**self.rows[0], 'chat_id':-99999})
        result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(result['phase'],'failed')
        self.assertEqual(self.sent,[])

    async def test_untrusted_job_is_rejected_and_offline_bot_never_recreated(self):
        for job in ({'run_id':RUN,'families':['ONTON']}, {'run_id':'bad','families':JOB['families']}):
            with self.assertRaises(ValueError): await sender.send_demo_batch(self.service, job)
        self.assertEqual(self.redis.calls,[])
        result = await sender.send_demo_batch(SimpleNamespace(_client=None), JOB)
        self.assertEqual(result['phase'],'failed')
        self.assertEqual(self.sent,[])

    async def test_no_report_storage_bus_or_crm_mutations(self):
        with patch('core.minio.put_object',new_callable=AsyncMock) as storage, \
             patch('core.bus.publish',new_callable=AsyncMock) as bus, \
             patch('modules.partner_crm.service.PartnerCrmService.push_lead_moderated',new_callable=AsyncMock) as crm:
            result = await sender.send_demo_batch(self.service, JOB)
        self.assertEqual(result['phase'],'completed')
        storage.assert_not_awaited();bus.assert_not_awaited();crm.assert_not_awaited()


if __name__=='__main__': unittest.main()
