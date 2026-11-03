"""Explicit, single-use demo job on the already connected report-bot client.

Only a read-only family lookup and expiring Redis receipts are persisted. No
real dialog, CRM operation, report event, profile fetch or extra client exists.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core import db, redis as redis_mod
from modules.autochat.report import build_html
from .demo import _buttons
from .privacy import anonymize_report

log = logging.getLogger(__name__)
TTL = 86400
GROUP_DELAY_SEC = 2.0
FIXTURE_ROOT = Path('/app/.claude/operations/2026-11-03-demo-reports/assets')
FAMILIES = frozenset(('AFERIST','BANAN','JOKER','KALAW','YARIK','PIPITO','SAD',
                      'MESSI','DORA','TOFFY','DONBAS','EKSTAZ','YASYA'))


def _fixture():
    ctx, messages, captions = [json.loads((FIXTURE_ROOT/name).read_text(encoding='utf-8-sig'))
                              for name in ('ctx.json','messages.json','captions.json')]
    if ctx.get('account_id') != 0 or ctx.get('dialog_id') != 0:
        raise ValueError('Demo fixture must not reference real records')
    ctx = {**ctx, **{k:datetime.fromisoformat(ctx[k]) for k in ('started_at','finished_at')}}
    messages = [{**m,'date':datetime.fromisoformat(m['date'])} for m in messages]
    for key in ('hidden','revealed'):
        if not captions[key].startswith('🧪 ТЕСТ') or len(captions[key]) > 900:
            raise ValueError('Invalid demo caption')
    photos = []
    for index in range(1, 11):
        path = FIXTURE_ROOT/f'photo_{index:02d}.jpg'
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Oversized demo photo')
        blob = path.read_bytes()
        if not blob.startswith(b'\xff\xd8\xff'):
            raise ValueError('Demo fixture is not a JPEG photo')
        photos.append(blob)
    if sum(map(len, photos)) > 20 * 1024 * 1024:
        raise ValueError('Oversized demo album')
    return ctx, messages, captions, photos


async def send_demo_batch(service, job: dict) -> dict:
    """Send one fixed preview per distinct configured group, never auto-retry."""
    run_id, families = job.get('run_id'), job.get('families')
    if (not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id)
            or not isinstance(families, list) or len(families) != 13
            or not all(isinstance(f, str) for f in families) or set(families) != FAMILIES):
        raise ValueError('Invalid demo job')
    redis = redis_mod.get_client()
    run_key = 'report_demo_run:' + run_id
    if not await redis.set('report_demo_claim:' + run_id, b'1', nx=True, ex=TTL):
        previous = await redis.get(run_key)
        return json.loads(previous) if previous else {'run_id':run_id,'phase':'already_claimed','groups':[]}
    result = {'run_id':run_id,'phase':'preparing','groups':[]}

    async def save():
        result['updated_at'] = datetime.now(timezone.utc).isoformat()
        await redis.set(run_key, json.dumps(result, ensure_ascii=False), ex=TTL)

    try:
        await save()
        client = service._client
        if client is None or not client.is_connected():
            raise RuntimeError('Report bot unavailable')
        ctx, messages, captions, photo_bytes = _fixture()
        async with db.get_pool().acquire() as conn, conn.transaction(readonly=True):
            rows = await conn.fetch('''SELECT upper(r.family) family, r.chat_id
                FROM report_families r WHERE r.is_active AND upper(r.family)=ANY($1::text[])
                AND EXISTS(SELECT 1 FROM accounts a WHERE a.id<>22
                    AND split_part(upper(a.name),'_',1)=upper(r.family)) ORDER BY r.family''', families)
        if len(rows) != 13 or {r['family'] for r in rows} != FAMILIES:
            raise ValueError('Configured demo groups do not match authorized families')
        groups = {}
        for row in rows:
            groups.setdefault(int(row['chat_id']), []).append(row['family'])
        result['groups'] = [{'chat_id':chat, 'families':names, 'nonce':uuid.uuid4().hex,
                             'phase':'pending','message_id':None,'photo_message_ids':[],
                             'photo_count':0,'grouped_id':None}
                            for chat,names in groups.items()]
        result['phase'] = 'sending'
        await save()
        for group_index, row in enumerate(result['groups']):
            try:
                if group_index:
                    await asyncio.sleep(GROUP_DELAY_SEC)
                family = '/'.join(row['families'])
                hidden = captions['hidden'] + '\nСемейство: ' + family
                revealed = captions['revealed'] + '\nСемейство: ' + family
                if max(len(hidden), len(revealed)) > 1024:
                    raise ValueError('Demo caption exceeds Telegram limit')
                html = build_html({**ctx,'worker_name':'ТЕСТ — '+family}, messages).encode('utf-8')
                html = anonymize_report(html, [captions['username']], ctx['telegram_user_id'])
                document = io.BytesIO(html); document.name = 'report_test.html'
                state = {'chat_id':row['chat_id'],'message_id':0,'caption_hidden':hidden,
                         'caption_revealed':revealed,'choice':None}
                key = 'demo_report:' + row['nonce']
                if not await redis.set(key, json.dumps(state, ensure_ascii=False), nx=True, ex=TTL):
                    raise RuntimeError('Demo nonce already exists')
                row['phase'] = 'sending_anchor'
                await save()
                sent = await asyncio.wait_for(client.send_file(
                    row['chat_id'], document, caption=hidden, force_document=True,
                    buttons=_buttons(row['nonce'], None), parse_mode=None,
                ), timeout=30)
                if type(getattr(sent, 'id', None)) is not int or sent.id <= 0:
                    raise ValueError('Missing anchor confirmation')
                row['message_id'] = state['message_id'] = sent.id
                row['phase'] = 'anchor_sent'
                await save()
                if not await redis.set(key, json.dumps(state, ensure_ascii=False), xx=True, keepttl=True):
                    raise RuntimeError('Demo state expired')
                photos = []
                for index, blob in enumerate(photo_bytes, 1):
                    photo = io.BytesIO(blob); photo.name = f'photo_{index:02d}.jpg'; photos.append(photo)
                row['phase'] = 'sending_album'
                await save()
                album = await asyncio.wait_for(client.send_file(
                    row['chat_id'], photos, reply_to=sent.id, force_document=False, parse_mode=None,
                ), timeout=60)
                ids = [getattr(item, 'id', None) for item in album] if isinstance(album, (list,tuple)) else []
                row['photo_message_ids'] = [i for i in ids if type(i) is int and i > 0]
                row['photo_count'] = len(set(row['photo_message_ids']))
                grouped_ids = {getattr(item, 'grouped_id', None) for item in album} if isinstance(album, (list,tuple)) else set()
                row['grouped_id'] = next(iter(grouped_ids)) if len(grouped_ids)==1 else None
                if len(ids) != 10 or not all(type(i) is int and i > 0 for i in ids) or len(set(ids)) != 10:
                    raise ValueError('Incomplete album confirmation')
                if type(row['grouped_id']) is not int or not row['grouped_id']:
                    raise ValueError('Missing album grouping confirmation')
                row['phase'] = 'sent'
            except asyncio.CancelledError:
                row['phase'] = 'uncertain' if row['phase'].startswith('sending_') else 'cancelled'
                raise
            except Exception as exc:
                row['phase'] = 'uncertain' if row['phase'].startswith('sending_') else 'failed'
                row['error_kind'] = type(exc).__name__
                log.warning('demo delivery unavailable: %s', type(exc).__name__)
            await save()
        result['phase'] = 'completed' if all(r['phase']=='sent' for r in result['groups']) else 'partial'
    except asyncio.CancelledError:
        result['phase'] = 'cancelled'
        try: await save()
        except Exception: pass
        raise
    except Exception as exc:
        result['phase'], result['error_kind'] = 'failed', type(exc).__name__
        log.warning('demo batch unavailable: %s', type(exc).__name__)
    await save()
    return result
