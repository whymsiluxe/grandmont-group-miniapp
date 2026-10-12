"""Contract guard for the extracted weather/news/birthday feed router."""
import os
import sys

os.environ.setdefault('BOT_TOKEN', 'ci-dummy')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402


EXPECTED = {
    ('GET', '/api/feed/weather'),
    ('POST', '/api/feed/weather/react'),
    ('GET', '/api/feed/birthdays'),
    ('GET', '/api/feed/news'),
    ('POST', '/api/feed/news/{post_id}/react'),
    ('POST', '/api/feed/news/{post_id}/comments'),
    ('GET', '/api/feed/news/{post_id}/comments'),
    ('DELETE', '/api/feed/news/{post_id}/comments/{comment_id}'),
    ('POST', '/api/feed/read'),
    ('POST', '/api/feed/saved'),
    ('GET', '/api/feed/unread'),
}


def test_feed_news_routes_are_present_once_and_owned_by_extracted_module():
    matched = {}
    for route in backend.app.routes:
        path = getattr(route, 'path', None)
        methods = getattr(route, 'methods', set()) or set()
        for method in methods:
            key = (method, path)
            if key in EXPECTED:
                matched.setdefault(key, []).append(route)

    assert set(matched) == EXPECTED
    for key, routes in matched.items():
        assert len(routes) == 1, f'duplicate route registration for {key}: {len(routes)}'
        assert routes[0].endpoint.__module__ == 'routes.feed_news', key
