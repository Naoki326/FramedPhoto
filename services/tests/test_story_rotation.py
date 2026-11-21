"""Offline story scheduling and persistence regression tests."""
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from app import free_module as fm
from app import story_rotation as sr


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(fm, 'HISTORY_FILE', tmp_path / 'history.json')
    monkeypatch.setattr(fm, 'CACHE_DIR', tmp_path)
    monkeypatch.setattr(fm, 'free_enabled', lambda: True)
    monkeypatch.setattr(fm, '_cache_fps6', None)
    monkeypatch.setattr(fm, '_cache_key', '')
    module = fm.default_modules()[4]
    monkeypatch.setattr(fm, 'pick_module', lambda **kw: module)
    monkeypatch.setattr(fm, '_content_base', lambda _: dt.datetime(2026, 1, 1))
    monkeypatch.setattr(fm, '_llm_content', lambda m, d: None)
    monkeypatch.setattr(fm, '_generate_illustration', lambda p: None)
    monkeypatch.setattr(fm, '_render_text_card', lambda *args: b'png')
    monkeypatch.setattr(fm, 'prepare_image', lambda *a, **k: SimpleNamespace(data=b'FPS6-test'))
    monkeypatch.setattr(fm, '_save_to_library', lambda *a: 'library')
    return module


def test_rotation_no_replacement_reload_category_and_boundary(tmp_path):
    path = tmp_path / 'history.json'
    pool = sr.POOLS['fairy']
    assert len(pool) >= 20
    chosen = []
    for i in range(len(pool) * 2):
        story = sr.select(path, 'fairy', 'renamed', str(i))
        sr.commit(path, 'fairy', 'renamed', str(i), story)
        chosen.append(story)
    n = len(pool)
    assert len({s['id'] for s in chosen[:n]}) == n
    assert len({s['id'] for s in chosen[n:]}) == n
    assert chosen[n-1]['id'] != chosen[n]['id']
    assert all(a['category'] != b['category'] for a, b in zip(chosen, chosen[1:]))
    assert sr.select(path, 'fairy', 'renamed', '0')['id'] == chosen[0]['id']


def test_legacy_alias_avoided(tmp_path):
    path = tmp_path / 'h.json'
    path.write_text(json.dumps({'童话故事': ['仙履奇缘']}))
    assert sr.select(path, 'fairy', 'renamed', 'new')['title'] != '仙履奇缘'
    assert sr.select(path, 'fairy', 'renamed', 'new')['id'] != 'fairy.cinderella'


def test_exact_prompt_boundary(env):
    assert fm._story_pool(env) == 'fairy'
    assert fm._story_pool(dict(env, name='自定名字')) == 'fairy'
    assert fm._story_pool(dict(env, prompt=env['prompt'] + '只讲海洋故事')) is None


def test_failure_does_not_consume_and_corruption_blocks(env, monkeypatch):
    monkeypatch.setattr(fm, '_render_text_card', lambda *a: None)
    assert fm.render_free_fps6() is None
    assert not fm.HISTORY_FILE.exists()
    fm.HISTORY_FILE.write_text('{broken')
    assert fm.render_free_fps6() is None
    assert fm.HISTORY_FILE.read_text() == '{broken'


def test_concurrent_slot_idempotent_and_restart(env, monkeypatch):
    with ThreadPoolExecutor(max_workers=2) as ex:
        assert list(ex.map(lambda _: fm.render_free_fps6(), range(2))) == [b'FPS6-test'] * 2
    records = json.loads(fm.HISTORY_FILE.read_text())['_stories_v1']
    assert len(records) == 1
    monkeypatch.setattr(fm, '_cache_fps6', None)
    assert fm.render_free_fps6() == b'FPS6-test'
    assert len(json.loads(fm.HISTORY_FILE.read_text())['_stories_v1']) == 1


def test_wrong_llm_identity_uses_catalog(env, monkeypatch):
    seen = []
    monkeypatch.setattr(fm, '_llm_content', lambda m, d: {'story_id': 'wrong', 'title': '换故事', 'body': '不可信', 'image_prompt': 'bad'})
    monkeypatch.setattr(fm, '_render_text_card', lambda m, t, b: seen.append((t, b)) or b'png')
    assert fm.render_free_fps6()
    assert seen and seen[0][0] != '换故事' and seen[0][1] != '不可信'


def test_custom_corrupt_history_fails_closed(env, monkeypatch):
    env['prompt'] = '只讲海洋故事'
    fm.HISTORY_FILE.write_text('{broken')
    assert fm.render_free_fps6() is None


def test_orphan_cache_is_not_served(env, monkeypatch):
    cache = fm._cache_file(env, dt.datetime(2026, 1, 1))
    cache.write_bytes(b'orphan')
    assert fm.render_free_fps6() == b'FPS6-test'
    assert len(json.loads(fm.HISTORY_FILE.read_text())['_stories_v1']) == 1


def test_slots_include_module_config(env):
    base = dt.datetime(2026, 1, 1)
    assert fm._cache_file(env, base) != fm._cache_file(dict(env, name='other'), base)


def test_fallback_preserves_style(env, monkeypatch):
    seen = []
    monkeypatch.setattr(fm, '_generate_illustration', lambda p: seen.append(p))
    assert fm.render_free_fps6()
    assert env['style'] in seen[0]


def test_write_failure_not_success(env, monkeypatch):
    def fail(*a, **kw):
        raise OSError('disk full')
    monkeypatch.setattr(sr, 'commit', fail)
    assert fm.render_free_fps6() is None
    assert not list(fm.CACHE_DIR.glob('*.fps6'))
