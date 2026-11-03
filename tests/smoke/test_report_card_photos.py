"""Offline report-photo policy tests; no sockets, database or Telegram sessions.

Run: python -m unittest tests.smoke.test_report_card_photos
Uses the deployed Telethon types with a fake existing client's read methods.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import FloodWaitError
from telethon.client.downloads import DownloadMethods
from telethon.tl import types
from telethon.tl.functions.stories import GetPinnedStoriesRequest

from modules.report_bot.media import collect_report_photos
from modules.worker.wrapper import TelegramWrapper

JPEG = b"\xff\xd8\xff"


def photo(ident, *, size=100, progressive=False, video=False):
    size_type = types.PhotoSizeProgressive if progressive else types.PhotoSize
    thumb = size_type.__new__(size_type)
    thumb.type, thumb.w, thumb.h = "x", 800, 800
    if progressive:
        thumb.sizes = [20, size]
    else:
        thumb.size = size
    obj = types.Photo.__new__(types.Photo)
    obj.id, obj.sizes = ident, [thumb]
    obj.video_sizes = [types.VideoSize(type="u", w=800, h=800, size=1000)] if video else []
    return obj


def story(ident, image=None, *, public=True, protected=False, video=False):
    obj = types.StoryItem.__new__(types.StoryItem)
    obj.id, obj.public, obj.noforwards = ident, public, protected
    media_type = types.MessageMediaDocument if video else types.MessageMediaPhoto
    obj.media = media_type.__new__(media_type)
    obj.media.photo = image
    return obj


class ExistingClient:
    def __init__(self, profile=(), pages=()):
        self.get_input_entity = AsyncMock(return_value=types.InputPeerUser(42, 1))
        self.get_profile_photos = AsyncMock(return_value=list(profile))
        self.pages = iter(pages)
        self.requests, self.downloads = [], []
        self.blobs = {}
        self.download_error = None

    async def __call__(self, request):
        self.requests.append(request)
        if not isinstance(request, GetPinnedStoriesRequest):
            raise AssertionError("unexpected Telegram method")
        page = next(self.pages, [])
        if isinstance(page, BaseException):
            raise page
        return SimpleNamespace(stories=page)

    async def download_media(self, value, *, file, thumb):
        self.downloads.append((value, thumb))
        if self.download_error:
            raise self.download_error
        file.write(self.blobs.get(value.id, JPEG + str(value.id).encode()))
        return file


def wrapper(client):
    instance = TelegramWrapper.__new__(TelegramWrapper)
    instance.account_id, instance._client = 23, client
    return instance


class PhotoSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_profile_photos_prevent_story_requests(self):
        client = ExistingClient([photo(n) for n in range(12)])
        result = await wrapper(client).fetch_report_photos(42, limit=99)
        self.assertEqual(result, [JPEG + str(n).encode() for n in range(10)])
        self.assertEqual(client.requests, [])
        client.get_profile_photos.assert_awaited_once_with(types.InputPeerUser(42, 1), limit=10)

    async def test_profile_first_then_pinned_photos_with_pagination_and_dedup(self):
        client = ExistingClient([photo(1), photo(2), photo(3)], [
            [story(100, photo(9), video=True), story(99, photo(8), protected=True),
             story(98, photo(1)), story(97, photo(4), public=False)],
            [story(96, photo(5)), story(95, photo(6))], [],
        ])
        client.blobs[5] = JPEG + b"4"  # Different photo ID, same file.
        result = await wrapper(client).fetch_report_photos(42)
        self.assertEqual(result, [JPEG + str(n).encode() for n in (1, 2, 3, 4, 6)])
        self.assertEqual([req.offset_id for req in client.requests], [0, 97, 95])
        self.assertTrue(all(req.limit == 100 for req in client.requests))
        self.assertEqual([p.id for p, _ in client.downloads], [1, 2, 3, 4, 5, 6])

    async def test_animated_profile_download_is_explicit_static_progressive(self):
        original = photo(1, progressive=True, video=True)
        client = ExistingClient([original])
        self.assertEqual(await wrapper(client).fetch_report_photos(42, limit=1), [JPEG + b"1"])
        downloaded, thumb = client.downloads[0]
        self.assertIsNot(downloaded, original)
        self.assertEqual(downloaded.video_sizes, [])
        self.assertTrue(original.video_sizes)
        self.assertEqual(thumb, "x")
        self.assertIsInstance(downloaded.sizes[0], types.PhotoSizeProgressive)
        # Exercise the installed Telethon selection logic, not only our fake.
        chosen = DownloadMethods()._get_thumb(downloaded.sizes + downloaded.video_sizes, thumb)
        self.assertIs(chosen, downloaded.sizes[0])

    async def test_profile_read_failure_can_fall_back_to_accessible_pinned_photos(self):
        client = ExistingClient(pages=[[story(5, photo(2))]])
        client.get_profile_photos.side_effect = RuntimeError("private profile")
        self.assertEqual(await wrapper(client).fetch_report_photos(42), [JPEG + b"2"])

    async def test_inaccessible_stories_preserve_collected_profile_photos(self):
        client = ExistingClient([photo(1)], [RuntimeError("unavailable")])
        self.assertEqual(await wrapper(client).fetch_report_photos(42), [JPEG + b"1"])

    async def test_flood_wait_stops_optional_fetch_without_retry_or_runner_actions(self):
        client = ExistingClient([photo(1)], [FloodWaitError(request=None, capture=90)])
        with patch("modules.worker.wrapper.bus.publish", new_callable=AsyncMock) as publish:
            self.assertEqual(await wrapper(client).fetch_report_photos(42), [JPEG + b"1"])
        self.assertEqual(len(client.requests), 1)
        publish.assert_not_awaited()

    async def test_download_flood_does_not_try_stories(self):
        client = ExistingClient([photo(1), photo(2)])
        client.download_error = FloodWaitError(request=None, capture=90)
        self.assertEqual(await wrapper(client).fetch_report_photos(42), [])
        self.assertEqual(client.requests, [])
        self.assertEqual(len(client.downloads), 1)

    async def test_cancellation_propagates_without_followup_requests(self):
        client = ExistingClient([photo(1)])
        client.download_error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await wrapper(client).fetch_report_photos(42)
        self.assertEqual(client.requests, [])

    async def test_timeout_ends_optional_fetch_and_keeps_completed_photos(self):
        client = ExistingClient([photo(1)], [asyncio.TimeoutError()])
        self.assertEqual(await wrapper(client).fetch_report_photos(42), [JPEG + b"1"])
        self.assertEqual(len(client.requests), 1)

    async def test_metadata_and_stream_byte_limits_and_nonphoto_bytes(self):
        client = ExistingClient([photo(1, size=5 * 1024 * 1024), photo(2), photo(3), photo(4)])
        client.blobs[2] = JPEG + b"x" * (4 * 1024 * 1024)
        client.blobs[3] = b"not jpeg or video"
        self.assertEqual(await wrapper(client).fetch_report_photos(42), [JPEG + b"4"])
        self.assertEqual([p.id for p, _ in client.downloads], [2, 3, 4])

    async def test_total_bytes_are_bounded_to_twenty_mib(self):
        client = ExistingClient([photo(n, size=4 * 1024 * 1024) for n in range(10)])
        for n in range(10):
            client.blobs[n] = JPEG + bytes([n]) * (4 * 1024 * 1024 - len(JPEG))
        result = await wrapper(client).fetch_report_photos(42)
        self.assertEqual(len(result), 5)
        self.assertEqual(sum(map(len, result)), 20 * 1024 * 1024)
        self.assertEqual(len(client.downloads), 5)

    async def test_pages_and_failed_download_attempts_are_bounded(self):
        client = ExistingClient(pages=[
            [story(300 - n, photo(n)) for n in range(100)],
            [story(200 - n, photo(n + 100)) for n in range(100)],
            [story(100 - n, photo(n + 200)) for n in range(100)],
        ])
        client.download_error = RuntimeError("unavailable file")
        self.assertEqual(await wrapper(client).fetch_report_photos(42), [])
        self.assertEqual(len(client.downloads), 20)
        self.assertEqual(len(client.requests), 1)

    async def test_video_only_history_has_at_most_three_pages(self):
        client = ExistingClient(pages=[
            [story(300 - n, video=True) for n in range(100)],
            [story(200 - n, video=True) for n in range(100)],
            [story(100 - n, video=True) for n in range(100)],
            [story(1, photo(99))],
        ])
        self.assertEqual(await wrapper(client).fetch_report_photos(42), [])
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.downloads, [])

    async def test_missing_invalid_photos_and_repeated_page_end_safely(self):
        client = ExistingClient([None, types.PhotoEmpty(99)], [
            [story(5, None)], [story(5, None)], [story(1, photo(1))],
        ])
        self.assertEqual(await wrapper(client).fetch_report_photos(42), [])
        self.assertEqual(len(client.requests), 2)

    async def test_zero_limit_performs_no_reads(self):
        client = ExistingClient([photo(1)])
        self.assertEqual(await wrapper(client).fetch_report_photos(42, limit=0), [])
        client.get_input_entity.assert_not_awaited()


class CollectionAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_wrapper_only_neutral_filenames_and_valid_jpegs(self):
        worker = SimpleNamespace(fetch_report_photos=AsyncMock(return_value=[
            JPEG + b"a", JPEG + b"a", b"video", JPEG + b"b",
        ]))
        manager = SimpleNamespace(get_wrapper=Mock(return_value=worker))
        uploads = await collect_report_photos(manager, 23, 42)
        self.assertEqual([u.name for u in uploads], ["photo_01.jpg", "photo_02.jpg"])
        self.assertEqual([u.read() for u in uploads], [JPEG + b"a", JPEG + b"b"])
        manager.get_wrapper.assert_called_once_with(23)
        worker.fetch_report_photos.assert_awaited_once_with(42, limit=10)

    async def test_missing_worker_and_manager_do_not_start_anything(self):
        self.assertEqual(await collect_report_photos(None, 23, 42), [])
        manager = SimpleNamespace(get_wrapper=Mock(return_value=None))
        self.assertEqual(await collect_report_photos(manager, 23, 42), [])

    async def test_failure_and_timeout_are_optional_but_cancel_propagates(self):
        worker = SimpleNamespace(fetch_report_photos=AsyncMock(side_effect=RuntimeError("offline")))
        manager = SimpleNamespace(get_wrapper=lambda account_id: worker)
        self.assertEqual(await collect_report_photos(manager, 23, 42), [])
        worker.fetch_report_photos.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await collect_report_photos(manager, 23, 42)
        async def slow(*args, **kwargs):
            await asyncio.sleep(10)
        worker.fetch_report_photos.side_effect = slow
        with patch("modules.report_bot.media.COLLECT_TIMEOUT_SEC", 0.001):
            self.assertEqual(await collect_report_photos(manager, 23, 42), [])


if __name__ == "__main__":
    unittest.main()
