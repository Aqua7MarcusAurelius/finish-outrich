"""Serialize decisions for one dialogue in the single application process.

No database connection is held while calling CRM. Weak references release locks
after the last holder/waiter; unrelated dialogues never block one another.
"""
from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary

_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def dialog_guard(dialog_id: int) -> asyncio.Lock:
    lock = _locks.get(dialog_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[dialog_id] = lock
    return lock
