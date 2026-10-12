"""Contract guard for the extracted activity/comment bridge router."""
import os
import sys

os.environ.setdefault('BOT_TOKEN', 'ci-dummy')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


EXPECTED = {
    ('POST', '/api/activity-alerts/read'),
    ('POST', '/api/comments/forward'),
    ('GET', '/api/feed/photos/{photo_id}/file'),
}


def test_activity_bridge_routes_are_present_once_and_owned_by_extracted_module():
    matched = {}
    for route in iter_app_routes(backend.app):
        path = getattr(route, 'path', None)
        methods = getattr(route, 'methods', set()) or set()
        for method in methods:
            key = (method, path)
            if key in EXPECTED:
                matched.setdefault(key, []).append(route)

    assert set(matched) == EXPECTED
    for key, routes in matched.items():
        assert len(routes) == 1, f'duplicate route registration for {key}: {len(routes)}'
        assert routes[0].endpoint.__module__ == 'routes.activity_bridge', key
