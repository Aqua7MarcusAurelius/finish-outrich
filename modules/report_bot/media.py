"""Optional static-photo enrichment using an already running worker only."""
from __future__ import annotations

import asyncio
import io
import logging

log = logging.getLogger(__name__)
COLLECT_TIMEOUT_SEC = 35.0


async def collect_report_photos(manager, account_id: int, telegram_user_id: int) -> list[io.BytesIO]:
    """Return neutral JPEG uploads, or fewer/none when Telegram is unavailable.

    Never starts a worker or creates a client/session. Caller sends these as a
    separate album replying to the persisted report anchor; album failure must
    not retry the already delivered report. Cancellation is never swallowed.
    """
    if manager is None:
        return []
    try:
        wrapper = manager.get_wrapper(int(account_id))
        if wrapper is None:
            return []
        blobs = await asyncio.wait_for(
            wrapper.fetch_report_photos(int(telegram_user_id), limit=10),
            timeout=COLLECT_TIMEOUT_SEC,
        )
        uploads = []
        seen: set[bytes] = set()
        remaining = 20 * 1024 * 1024
        for blob in (blobs or [])[:10]:
            if (not isinstance(blob, bytes) or not blob.startswith(b"\xff\xd8\xff")
                    or len(blob) > min(4 * 1024 * 1024, remaining) or blob in seen):
                continue
            seen.add(blob)
            remaining -= len(blob)
            upload = io.BytesIO(blob)
            upload.name = f"photo_{len(uploads) + 1:02d}.jpg"
            uploads.append(upload)
        return uploads
    except Exception as exc:
        log.info("report photo enrichment unavailable account=%s error=%s",
                 account_id, type(exc).__name__)
        return []
