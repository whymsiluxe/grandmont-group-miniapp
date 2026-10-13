"""Phase 25 regressions for canonical news reaction persistence."""
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
    NewsReactionIn,
    create_feed_news_router,
)


class TransactionStore:
    def __init__(self):
        self.by_path = {}
        self.transaction_paths = []
        self.atomic_writes = []

    def safe_load(self, path, default):
        return deepcopy(self.by_path.get(path, default))

    def atomic_write(self, path, value):
        self.atomic_writes.append(path)
        self.by_path[path] = deepcopy(value)

    def update(self, path, default, mutator):
        self.transaction_paths.append(path)
        current = deepcopy(self.by_path.get(path, default))
        result = mutator(current)
        self.by_path[path] = deepcopy(current)
        return result


def _build_handlers(tmp_path, *, feed=None, reactions=None):
    store = TransactionStore()
    weather_feed = tmp_path / 'weather_feed.json'
    news_feed = tmp_path / 'news_feed.json'
    weather_reactions = tmp_path / 'weather_reactions.json'
    news_reactions = tmp_path / 'news_reactions.json'
    news_reads = tmp_path / 'news_reads.json'
    news_comments = tmp_path / 'news_comments.json'
    feed_reads = tmp_path / 'feed_reads.json'
    feed_saved = tmp_path / 'feed_saved.json'
    birthday_alerts = tmp_path / 'birthday_alerts.json'

    weather_feed.write_text('[]')
    news_feed.write_text(json.dumps(feed or [
        {'id': 'post-1', 'title': 'News', 'category': 'Site', 'created': 10, 'likes': 99, 'dislikes': 88},
    ]))
    store.by_path[str(news_reactions)] = deepcopy(reactions or {})

    deps = FeedNewsRouteDeps(
        get_current_user=lambda: {'id': '1'},
        get_role=lambda: 'worker',
        safe_load_json=store.safe_load,
        atomic_write_json=store.atomic_write,
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
        load_roles=lambda: {'1': 'worker', '2': 'worker', '3': 'worker'},
        sanitize_display_name=lambda raw, fallback: str(raw or fallback),
        resolve_current_display_name=lambda _uid, name: name,
        business_today=lambda: date(2026, 10, 14),
        business_today_str=lambda: '2026-10-14',
        create_critical_alert=lambda **kwargs: None,
        emit_comment_activity_alerts=lambda *args, **kwargs: None,
        comment_alert_recipients=lambda *args, **kwargs: set(),
    )
    _router, handlers = create_feed_news_router(deps)
    return handlers, store, str(news_feed), str(news_reactions)


def test_news_feed_derives_counts_from_reaction_map_not_stale_feed_counters(tmp_path):
    handlers, _store, _feed_path, _reaction_path = _build_handlers(
        tmp_path,
        reactions={'post-1': {'1': 'like', '2': 'like', '3': 'dislike'}},
    )

    response = handlers.get_news_feed(user={'id': '1'})
    post = response['feed'][0]

    assert post['likes'] == 2
    assert post['dislikes'] == 1
    assert post['my_reaction'] == 'like'


def test_react_news_post_transaction_preserves_other_users_and_derives_counts(tmp_path):
    handlers, store, _feed_path, reaction_path = _build_handlers(
        tmp_path,
        reactions={'post-1': {'2': 'like'}},
    )

    response = handlers.react_news_post(
        'post-1', NewsReactionIn(reaction='dislike'), user={'id': '1'},
    )

    assert response == {'ok': True, 'likes': 1, 'dislikes': 1, 'my_reaction': 'dislike'}
    assert store.by_path[reaction_path]['post-1'] == {'2': 'like', '1': 'dislike'}
    assert reaction_path in store.transaction_paths


def test_react_news_post_none_removes_only_current_user(tmp_path):
    handlers, store, _feed_path, reaction_path = _build_handlers(
        tmp_path,
        reactions={'post-1': {'1': 'like', '2': 'dislike'}},
    )

    response = handlers.react_news_post(
        'post-1', NewsReactionIn(reaction='none'), user={'id': '1'},
    )

    assert response == {'ok': True, 'likes': 0, 'dislikes': 1, 'my_reaction': None}
    assert store.by_path[reaction_path]['post-1'] == {'2': 'dislike'}


def test_react_news_post_never_rewrites_news_feed(tmp_path):
    handlers, store, feed_path, _reaction_path = _build_handlers(
        tmp_path,
        reactions={'post-1': {'2': 'like'}},
    )
    before = open(feed_path, encoding='utf-8').read()

    handlers.react_news_post(
        'post-1', NewsReactionIn(reaction='like'), user={'id': '1'},
    )

    after = open(feed_path, encoding='utf-8').read()
    assert after == before
    assert feed_path not in store.atomic_writes


def test_phase25_reaction_path_is_structurally_single_store():
    import routes.feed_news as feed_routes

    source = inspect.getsource(feed_routes.create_feed_news_router)
    get_block = source.split('    def get_news_feed', 1)[1].split('    @router.post', 1)[0]
    react_block = source.split('    def react_news_post', 1)[1].split('    @router.post', 1)[0]

    assert "post['likes'] = sum(1 for reaction in post_reactions.values() if reaction == 'like')" in get_block
    assert "post['dislikes'] = sum(1 for reaction in post_reactions.values() if reaction == 'dislike')" in get_block
    assert 'deps.update_json_transaction(deps.news_reactions_file(), {}, mutate)' in react_block
    assert 'save_news_reactions(' not in react_block
    assert 'deps.atomic_write_json(path, feed)' not in react_block
