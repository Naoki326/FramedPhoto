"""Persistent finite-pool scheduler. Caller serializes select/render/commit.
Only successful cached cards are committed. This is not device exposure history.
"""
from collections import Counter
import json
import logging
import os
from pathlib import Path
import tempfile
import unicodedata

from app.story_catalog import POOLS

logger = logging.getLogger(__name__)
KEY = '_stories_v1'


class HistoryError(ValueError):
    pass


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold()
                   if c.isalnum())


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('not an object')
        for key, values in data.items():
            if not isinstance(values, list):
                raise ValueError('not a list')
            if key == KEY:
                for event in values:
                    if (not isinstance(event, dict) or
                        not all(isinstance(event.get(k), str) and event[k]
                                for k in ('pool', 'story_id', 'slot', 'module', 'status')) or
                        event['status'] != 'generated'):
                        raise ValueError('invalid story record')
            elif not all(isinstance(t, str) for t in values):
                raise ValueError('invalid legacy titles')
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.error('story history unavailable: %s (%s)', path, type(exc).__name__)
        raise HistoryError('history unavailable; refusing new selection') from exc


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def records(data, pool, module):
    result = [r for r in data.get(KEY, []) if r['pool'] == pool]
    # Legacy titles are retained. Migration is deterministic and not re-counted
    # per commit. Unknown titles stay visible, not claimed as identified stories.
    names = {module, '童话故事' if pool == 'fairy' else '历史故事'}
    aliases = {normalize(t): s['id'] for s in POOLS[pool]
               for t in [s['title'], *s['aliases']]}
    legacy = []
    for name in sorted(names):
        for title in data.get(name, []):
            identity = aliases.get(normalize(title))
            if identity:
                legacy.append({'story_id': identity})
            else:
                logger.info('unmapped legacy story title: module=%s', name)
    return legacy + result


def select(path, pool, module, slot):
    data = load(path)
    stories = POOLS[pool]
    for event in data.get(KEY, []):
        if event['pool'] == pool and event['slot'] == slot:
            for story in stories:
                if story['id'] == event['story_id']:
                    return story
            raise HistoryError('cached slot refers to removed story')
    events = records(data, pool, module)
    counts = Counter(e['story_id'] for e in events)
    # Least-used first yields full coverage before reuse. N-1 successful
    # selections of cooldown is capacity-based, not a promise of calendar days.
    minimum = min(counts[s['id']] for s in stories)
    candidates = [s for s in stories if counts[s['id']] == minimum]
    recent = {e['story_id'] for e in events[-(len(stories)-1):]} if len(stories) > 1 else set()
    cooled = [s for s in candidates if s['id'] not in recent]
    if cooled:
        candidates = cooled
    elif events:
        # Irregular migrated histories can conflict with coverage. Never repeat
        # the immediate predecessor; relax only the longer cooldown, with a log.
        candidates = [s for s in candidates if s['id'] != events[-1]['story_id']]
        logger.warning('story cooldown relaxed for migrated/changed pool: %s', pool)
    if not candidates:
        raise HistoryError('no eligible story')
    last = next((s for s in stories if events and s['id'] == events[-1]['story_id']), None)
    varied = [s for s in candidates if not last or s['category'] != last['category']]
    story = (varied or candidates)[0]
    logger.info('story selected pool=%s id=%s capacity=%d cooldown=%d',
                pool, story['id'], len(stories), len(stories)-1)
    return story


def commit(path, pool, module, slot, story):
    data = load(path)
    if any(e['pool'] == pool and e['slot'] == slot for e in data.get(KEY, [])):
        return
    data.setdefault(KEY, []).append(dict(pool=pool, module=module, slot=slot,
        story_id=story['id'], title=story['title'], category=story['category'],
        status='generated', content_version=story['version']))
    atomic_write(path, data)
    logger.info('story successfully generated pool=%s id=%s slot=%s', pool, story['id'], slot)
