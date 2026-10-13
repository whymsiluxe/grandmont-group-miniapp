"""Phase 24 regressions for feed/news single-store lost-update races."""
from __future__ import annotations

import inspect
import json
import os
import sys
from copy import deepcopy
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.feed_news import (  # noqa: E402
    FeedNewsRouteDeps,
    FeedReadBody,
    NewsCommentBody,
    create_feed_news_router,
)


class TransactionStore:
    def __init__(self, by_path):
        self.by_path = deepcopy(by_path)
        self.calls = []

    def update(self, path, default, mutator):
        self.calls.append(path)
        current = deepcopy(self.by_path.get(path, default))
        result = mutator(current)
        self.by_path[path] = deepcopy(current)
        return result


def _build_handlers(tmp_path, store: TransactionStore):
    weather_feed = tmp_path / 'weather_feed.json'
    news_feed = tmp_path / 'news_feed.json'
    weather_reactions = tmp_path / 'weather_reactions.json'
    news_reactions = tmp_path / 'news_reactions.json'
    news_reads = tmp_path / 'news_reads.json'
    news_comments = tmp_path / 'news_comments.json'
    feed_reads = tmp_path / 'feed_reads.json'
    feed_saved = tmp_path / 'feed_saved.json'
    birthday_alerts = tmp_path / 'birthday_alerts.json'

    news_feed.write_text(json.dumps([
        {'id': 'post-1', 'title': 'News', 'category': 'Site', 'created': 10, 'likes': 0, 'dislikes': 0},
    ]))
    weather_feed.write_text('[]')

    def safe_load(path, default):
        return deepcopy(store.by_path.get(path, default))

    deps = FeedNewsRouteDeps(
        get_current_user=lambda: {'id': '1'},
        get_role=lambda: 'worker',
        safe_load_json=safe_load,
        atomic_write_json=lambda path, value: store.by_path.__setitem__(path, deepcopy(value)),
        update_json_transaction=store.update,
        weather_feed_file=lambda: str(weather_feed),
        news_feed_file=lambda: str(news_feed),
        weather_reactions_file=lambda: str(weather_reactions),
        news_reactions_file=lambda: str(news_reactions),
        news_reads_file=lambda: str(news_reads),
        news_comments_file=lambda: str(news_comments),
        feed_reads_file=lambda: str(feed_reads),
        feed_saved_file=lambda: str(feed_saved),
        birthday_alerts_file=lambda: str(birthday_alerts),
        load_photo_meta=lambda: [],
        load_worker_profiles=lambda: {},
        load_roles=lambda: {'1': 'worker', '2': 'worker'},
        sanitize_display_name=lambda raw, fallback: str(raw or fallback),
        resolve_current_display_name=lambda _uid, name: name,
        business_today=lambda: date(2026, 10, 14),
        business_today_str=lambda: '2026-10-14',
        create_critical_alert=lambda **kwargs: None,
        emit_comment_activity_alerts=lambda *args, **kwargs: None,
        comment_alert_recipients=lambda *args, **kwargs: set(),
    )
    _router, handlers = create_feed_news_router(deps)
    paths = {
        'weather_reactions': str(weather_reactions),
        'news_reads': str(news_reads),
        'news_comments': str(news_comments),
        'feed_reads': str(feed_reads),
    }
    return handlers, paths


def test_weather_reaction_transaction_preserves_other_users(tmp_path):
    key = 'OBJ-1::123'
    store = TransactionStore({})
    handlers, paths = _build_handlers(tmp_path, store)
    store.by_path[paths['weather_reactions']] = {key: {'2': True}}

    result = handlers.react_weather_entry(
        {'object': 'OBJ-1', 'created': '123', 'liked': True},
        user={'id': '1'},
    )

    assert result == {'likes': 2, 'liked_by_me': True}
    assert store.by_path[paths['weather_reactions']][key] == {'2': True, '1': True}


def test_get_news_feed_transaction_preserves_other_users_read_counters(tmp_path):
    store = TransactionStore({})
    handlers, paths = _build_handlers(tmp_path, store)
    store.by_path[paths['news_reads']] = {'2': {'Site': 7}}

    response = handlers.get_news_feed(user={'id': '1'})

    assert response['feed'][0]['id'] == 'post-1'
    assert store.by_path[paths['news_reads']]['2']['Site'] == 7
    assert store.by_path[paths['news_reads']]['1']['Site'] == 1


def test_add_news_comment_transaction_preserves_fresh_comment(tmp_path):
    store = TransactionStore({})
    handlers, paths = _build_handlers(tmp_path, store)
    store.by_path[paths['news_comments']] = {
        'post-1': [
            {'id': 'fresh', 'user_id': '2', 'name': 'Other', 'text': 'fresh', 'reply_to': None, 'ts': 1},
        ]
    }

    response = handlers.add_news_comment(
        'post-1',
        NewsCommentBody(text='mine'),
        user={'id': '1', 'first_name': 'Me'},
    )

    ids = [item['id'] for item in response['comments']]
    assert 'fresh' in ids
    assert len(ids) == 2
    assert [item['id'] for item in store.by_path[paths['news_comments']]['post-1']] == ids


def test_delete_news_comment_transaction_preserves_parallel_comment(tmp_path):
    store = TransactionStore({})
    handlers, paths = _build_handlers(tmp_path, store)
    store.by_path[paths['news_comments']] = {
        'post-1': [
            {'id': 'mine', 'user_id': '1', 'name': 'Me', 'text': 'old', 'reply_to': None, 'ts': 1},
            {'id': 'fresh', 'user_id': '2', 'name': 'Other', 'text': 'fresh', 'reply_to': None, 'ts': 2},
        ]
    }

    response = handlers.delete_news_comment(
        'post-1', 'mine', user={'id': '1'}, role='worker',
    )

    assert [item['id'] for item in response['comments']] == ['fresh']
    assert [item['id'] for item in store.by_path[paths['news_comments']]['post-1']] == ['fresh']


def test_mark_feed_read_transaction_preserves_other_tabs_and_users(tmp_path):
    store = TransactionStore({})
    handlers, paths = _build_handlers(tmp_path, store)
    store.by_path[paths['feed_reads']] = {
        '1': {'last_photos_read_at': 111},
        '2': {'last_news_read_at': 222},
    }

    result = handlers.mark_feed_read(FeedReadBody(tab='news'), user={'id': '1'})

    assert result['ok'] is True
    assert result['tab'] == 'news'
    assert store.by_path[paths['feed_reads']]['1']['last_photos_read_at'] == 111
    assert store.by_path[paths['feed_reads']]['2']['last_news_read_at'] == 222
    assert store.by_path[paths['feed_reads']]['1']['last_news_read_at'] == result['read_at']


def test_phase24_single_store_writes_are_structurally_transactional():
    import routes.feed_news as feed_routes

    source = inspect.getsource(feed_routes.create_feed_news_router)
    blocks = {
        'weather': source.split('    def react_weather_entry', 1)[1].split('    def load_news_reactions', 1)[0],
        'get_news': source.split('    def get_news_feed', 1)[1].split('    @router.post', 1)[0],
        'add_comment': source.split('    def add_news_comment', 1)[1].split('    @router.get', 1)[0],
        'delete_comment': source.split('    def delete_news_comment', 1)[1].split('    @router.post', 1)[0],
        'mark_read': source.split('    def mark_feed_read', 1)[1].split('    @router.post', 1)[0],
    }
    for block in blocks.values():
        assert 'deps.update_json_transaction(' in block
