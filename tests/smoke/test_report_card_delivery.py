"""Offline report delivery/decision tests. No DB, Telegram, Redis or CRM I/O."""
import asyncio
import io
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.report_bot.service import ReportBotService, MODERATION_DELAY_SEC
from core.events import EventType


class CardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.record = dict(dialog_id=77123, account_id=29, family='TEST',
                           username='Lead_Secret', chat_id=-555, message_id=91,
                           minio_key='success/TEST_01_RU_@Filename_Alias_03-11-26_11-00.html',
                           choice=None, pushed_at=None)
        self.ctx = dict(username='Lead_Secret', first_name='Анна', last_name='',
                        telegram_user_id=987654321, extracted_name='Анна',
                        extracted_workplace='t.me/Lead_Secret', evaluated_at=datetime.now(timezone.utc))
        self.info = dict(username='Lead_Secret', target_username='Previous_Handle',
                         worker_name='TEST_01_RU', telegram_user_id=987654321)
        async def fetchrow(query, *args):
            if 'UPDATE report_moderation' in query:
                if (self.record['pushed_at'] is not None or
                    args[-2:] != (self.record['chat_id'], self.record['message_id'])):
                    return None
                self.record.update(choice=args[1], push_after=args[4])
                return dict(self.record)
            return dict(self.ctx)
        self.conn = SimpleNamespace(fetchrow=AsyncMock(side_effect=fetchrow), execute=AsyncMock())
        @asynccontextmanager
        async def acquire():
            yield self.conn
        self.pool = SimpleNamespace(acquire=acquire)
        self.crm = SimpleNamespace(push_lead_moderated=AsyncMock())
        self.bot = ReportBotService(self.crm)
        self.bot._dialog_info = AsyncMock(return_value=self.info)
        self.bot._load_families = AsyncMock()
        self.bot._families = {'TEST': {'chat_id': -555, 'l2_login': 'test_login'}}
        self.bot._client = SimpleNamespace(edit_message=AsyncMock(), send_file=AsyncMock(return_value=SimpleNamespace(id=91)))
        self.db_patch = patch('modules.report_bot.service.db.get_pool', return_value=self.pool)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def event(self, choice, **extra):
        result = SimpleNamespace(data=f'm:{choice}:77123'.encode(), chat_id=-555,
            message_id=91, sender_id=123, get_sender=AsyncMock(return_value=SimpleNamespace(username='manager')),
            answer=AsyncMock())
        for key, value in extra.items(): setattr(result, key, value)
        return result

    async def test_initial_caption_and_no_take_do_not_expose_contact(self):
        for reveal in (False,):
            caption = await self.bot._build_caption(77123, 'TEST_01_RU', reveal_username=reveal)
            self.assertNotIn('lead_secret', caption.lower())
            self.assertNotIn('987654321', caption)
            self.assertIn('Анна', caption)

    async def test_take_reveals_immediately_and_keeps_four_hour_crm_delay(self):
        before = datetime.now(timezone.utc)
        await self.bot._on_callback(self.event('take'))
        self.assertEqual(self.record['choice'], 'take')
        caption = self.bot._client.edit_message.await_args.args[2]
        self.assertIn('@Lead_Secret', caption)
        delay = (self.record['push_after'] - before).total_seconds()
        self.assertLess(abs(delay - MODERATION_DELAY_SEC), 2)
        self.crm.push_lead_moderated.assert_not_awaited()

    async def test_change_from_take_hides_contact_again(self):
        await self.bot._on_callback(self.event('take'))
        for choice in ('no_take', 'invalid'):
            await self.bot._on_callback(self.event(choice))
            self.assertEqual(self.record['choice'], choice)
            self.assertNotIn('lead_secret', self.bot._client.edit_message.await_args.args[2].lower())

    async def test_copied_or_forged_anchor_cannot_select_or_reveal(self):
        for event in (self.event('take', chat_id=-999), self.event('take', message_id=999)):
            await self.bot._on_callback(event)
            event.answer.assert_awaited_once()
        self.assertIsNone(self.record['choice'])
        self.bot._client.edit_message.assert_not_awaited()

    async def test_finalized_card_cannot_be_changed(self):
        self.record['pushed_at'] = datetime.now(timezone.utc)
        await self.bot._on_callback(self.event('take'))
        self.bot._client.edit_message.assert_not_awaited()

    async def test_edit_failure_preserves_choice_and_timer(self):
        self.bot._client.edit_message.side_effect = RuntimeError('offline')
        event = self.event('take')
        await self.bot._on_callback(event)
        self.assertEqual(self.record['choice'], 'take')
        self.assertIsNotNone(self.record['push_after'])
        event.answer.assert_awaited_once()

    async def test_slow_take_edit_cannot_follow_a_later_invalid_edit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def edit(*args, **kwargs):
            if '@Lead_Secret' in args[2]:
                entered.set()
                await release.wait()
        self.bot._client.edit_message.side_effect = edit
        take = asyncio.create_task(self.bot._on_callback(self.event('take')))
        await entered.wait()
        invalid = asyncio.create_task(self.bot._on_callback(self.event('invalid')))
        await asyncio.sleep(0)
        self.assertEqual(self.record['choice'], 'take')
        release.set()
        await asyncio.gather(take, invalid)
        self.assertEqual(self.record['choice'], 'invalid')
        self.assertNotIn('lead_secret', self.bot._client.edit_message.await_args.args[2].lower())

    async def delivery(self, photos, side_effect=None, claimed=True, collect_side_effect=None):
        redis = SimpleNamespace(set=AsyncMock(return_value=claimed), delete=AsyncMock())
        html = b'<!doctype html><html><head><title>@Lead_Secret</title></head><body>Previous_Handle 987654321</body></html>'
        event = dict(type=EventType.AUTOCHAT_REPORT_SAVED, account_id=29,
                     data=dict(folder='success', dialog_id=77123, minio_key='success/@Lead_Secret.html'))
        if side_effect:
            self.bot._client.send_file.side_effect = side_effect
        with patch('modules.report_bot.service.redis_mod.get_client', return_value=redis), \
             patch('modules.report_bot.service.minio.get_object', new=AsyncMock(return_value=html)), \
             patch('modules.report_bot.service.collect_report_photos', new=AsyncMock(return_value=photos, side_effect=collect_side_effect)) as collect, \
             patch('modules.report_bot.service.bus.publish', new=AsyncMock()) as publish:
            try:
                await self.bot._handle_report(event)
            finally:
                self.last_delivery = redis, collect, publish
        return redis, collect, publish

    async def test_delivery_masks_file_and_links_album_to_original_anchor(self):
        photos = [io.BytesIO(b'photo1'), io.BytesIO(b'photo2')]
        redis, collect, publish = await self.delivery(photos,
            [SimpleNamespace(id=91), [SimpleNamespace(id=92), SimpleNamespace(id=93)]])
        self.assertEqual(self.bot._client.send_file.await_count, 2)
        first, album = self.bot._client.send_file.await_args_list
        self.assertEqual(first.args[1].name, 'report_77123.html')
        anonymous = first.args[1].getvalue().lower()
        for sensitive in (b'lead_secret', b'previous_handle', b'987654321'):
            self.assertNotIn(sensitive, anonymous)
        self.assertEqual(album.kwargs['reply_to'], 91)
        self.assertNotIn('buttons', album.kwargs)
        redis.delete.assert_not_awaited()
        self.assertEqual(publish.await_args.kwargs['data']['photo_count'], 2)

    async def test_one_photo_uses_single_photo_send(self):
        photo = io.BytesIO(b'one')
        await self.delivery([photo], [SimpleNamespace(id=91), SimpleNamespace(id=92)])
        self.assertIs(self.bot._client.send_file.await_args.args[1], photo)

    async def test_photo_failure_never_releases_claim_or_retries_anchor(self):
        redis, collect, publish = await self.delivery([io.BytesIO(b'photo')],
            [SimpleNamespace(id=91), RuntimeError('album unavailable')])
        redis.delete.assert_not_awaited()
        self.assertEqual(self.bot._client.send_file.await_count, 2)
        delivered = [call for call in publish.await_args_list
                     if call.kwargs['type'] == EventType.REPORT_DELIVERED]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(publish.await_args.kwargs['type'], EventType.REPORT_PHOTOS_ATTACHED)
        self.assertEqual(publish.await_args.kwargs['data']['photo_error'], 'RuntimeError')

    async def test_shutdown_during_enrichment_keeps_confirmed_delivery_event(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.delivery([], collect_side_effect=asyncio.CancelledError())
        redis, collect, publish = self.last_delivery
        self.assertEqual(publish.await_count, 1)
        self.assertEqual(publish.await_args.kwargs['type'], EventType.REPORT_DELIVERED)
        redis.delete.assert_not_awaited()

    async def test_old_alias_is_hidden_in_caption_as_well_as_html(self):
        self.ctx['first_name'] = '@Previous_Handle'
        await self.delivery([])
        self.assertNotIn('previous_handle', self.bot._client.send_file.await_args.kwargs['caption'].lower())

    async def test_filename_alias_stays_hidden_after_no_take_and_final_seal(self):
        self.ctx['extracted_name'] = 'Filename_Alias'
        await self.bot._on_callback(self.event('no_take'))
        self.assertNotIn('filename_alias', self.bot._client.edit_message.await_args.args[2].lower())
        await self.bot._seal_message(dict(self.record, choice='invalid'), 'invalid')
        self.assertNotIn('filename_alias', self.bot._client.edit_message.await_args.args[2].lower())

    async def test_crm_warning_never_echoes_error_with_private_contact(self):
        self.bot._client.send_message = AsyncMock()
        await self.bot._warn_push_failed(self.record, 'GET /leads/Lead_Secret failed', 3)
        self.assertNotIn('lead_secret', self.bot._client.send_message.await_args.args[1].lower())

    async def test_final_seal_uses_decision_to_control_contact(self):
        for choice in ('take', 'no_take', 'invalid'):
            row = dict(self.record, choice=choice, chosen_by_name='manager')
            await self.bot._seal_message(row, 'transferred')
            caption = self.bot._client.edit_message.await_args.args[2]
            self.assertEqual('@Lead_Secret' in caption, choice == 'take')
            self.assertIsNone(self.bot._client.edit_message.await_args.kwargs['buttons'])

    async def test_no_available_photos_still_delivers_report_once(self):
        await self.delivery([])
        self.assertEqual(self.bot._client.send_file.await_count, 1)

    async def test_duplicate_report_does_not_fetch_photos_or_send(self):
        redis, collect, publish = await self.delivery([], claimed=False)
        self.bot._client.send_file.assert_not_awaited()
        collect.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
