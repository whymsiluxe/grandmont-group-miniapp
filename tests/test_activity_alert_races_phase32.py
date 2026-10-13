"""Phase 32 regressions for activity-alert lost updates."""
from __future__ import annotations

import inspect
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import routes.activity_bridge as activity_routes  # noqa: E402
from routes.activity_bridge import (  # noqa: E402
    ActivityBridgeDeps,
    ActivityReadBody,
    create_activity_bridge_router,
)


def _unexpected_split_io(*_args, **_kwargs):
    raise AssertionError('activity alert mutations must not use split load/save persistence')


def _build_handlers(tmp_path, initial=None):
    activity_file = tmp_path / 'activity_alerts.json'
    if initial is not None:
        activity_file.write_text(json.dumps(initial), encoding='utf-8')

    deps = ActivityBridgeDeps(
        get_current_user=lambda: {'id': '9'},
        get_role=lambda: 'worker',
        safe_load_json=_unexpected_split_io,
        atomic_write_json=_unexpected_split_io,
        activity_alerts_file=lambda: str(activity_file),
        news_feed_file=lambda: str(tmp_path / 'news.json'),
        photo_dir=lambda: str(tmp_path / 'photos'),
        cached_get_used_range=lambda _name: [],
        load_roles=lambda: {},
        load_news_comments=lambda: {},
        photo_lock=threading.Lock(),
        load_photo_meta=lambda: [],
        check_thread_access=lambda *_args, **_kwargs: None,
        reject_self_chat=lambda *_args, **_kwargs: None,
        validate_dm_recipient=lambda *_args, **_kwargs: None,
        chat_thread_id=lambda *_args, **_kwargs: 'group',
        load_chat_thread_meta=lambda: {},
        chat_lock=threading.Lock(),
        load_chat=lambda: [],
        save_chat=lambda _items: None,
    )
    _router, handlers = create_activity_bridge_router(deps)
    return handlers, activity_file


def test_concurrent_activity_emits_preserve_every_comment(tmp_path):
    handlers, activity_file = _build_handlers(tmp_path)

    def emit(index: int):
        handlers.emit_comment_activity_alerts(
            'news_comment',
            f'post-{index}',
            f'comment-{index}',
            '42',
            'Worker',
            f'Comment {index}',
            '',
            {'9'},
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(emit, range(60)))

    items = json.loads(activity_file.read_text(encoding='utf-8'))
    assert len(items) == 60
    assert {item['comment_id'] for item in items} == {
        f'comment-{index}' for index in range(60)
    }


def test_concurrent_duplicate_emit_is_idempotent_per_recipient_comment(tmp_path):
    handlers, activity_file = _build_handlers(tmp_path)

    def emit(_index: int):
        handlers.emit_comment_activity_alerts(
            'photo_comment',
            'photo-1',
            'comment-1',
            '42',
            'Worker',
            'Comment',
            '',
            {'9'},
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(emit, range(40)))

    items = json.loads(activity_file.read_text(encoding='utf-8'))
    assert len(items) == 1
    assert items[0]['target_user_id'] == '9'
    assert items[0]['comment_id'] == 'comment-1'


def test_concurrent_mark_read_preserves_updates_for_all_refs(tmp_path):
    initial = [
        {
            'id': f'alert-{index}',
            'target_user_id': '9',
            'kind': 'news_comment',
            'ref_id': f'post-{index}',
            'comment_id': f'comment-{index}',
            'read_at': None,
        }
        for index in range(60)
    ]
    handlers, activity_file = _build_handlers(tmp_path, initial)

    def mark(index: int):
        return handlers.mark_activity_alerts_read(
            ActivityReadBody(kind='news_comment', ref_id=f'post-{index}'),
            user={'id': '9'},
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(mark, range(60)))

    assert all(result == {'status': 'ok'} for result in results)
    items = json.loads(activity_file.read_text(encoding='utf-8'))
    assert all(item['read_at'] for item in items)


def test_activity_alert_writers_use_storage_transactions_structurally():
    source = inspect.getsource(activity_routes.create_activity_bridge_router)
    emit_block = source.split('    def emit_comment_activity_alerts', 1)[1].split(
        "    @router.post('/api/activity-alerts/read')", 1
    )[0]
    read_block = source.split('    def mark_activity_alerts_read', 1)[1].split(
        "    @router.post('/api/comments/forward')", 1
    )[0]

    assert 'update_json_transaction(deps.activity_alerts_file(), [], _mutate)' in emit_block
    assert 'load_activity_alerts()' not in emit_block
    assert 'save_activity_alerts(items)' not in emit_block
    assert 'update_json_transaction(deps.activity_alerts_file(), [], _mutate)' in read_block
    assert 'load_activity_alerts()' not in read_block
    assert 'save_activity_alerts(items)' not in read_block
