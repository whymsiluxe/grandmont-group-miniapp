import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


EXPECTED = {
    ('POST', '/api/transcribe'): 'routes.voice',
    ('GET', '/api/transcribe/{file_id}/audio'): 'routes.voice',
    ('POST', '/api/chat/messages/voice'): 'routes.voice',
    ('POST', '/api/manager/command/parse'): 'routes.manager_assistant',
    ('POST', '/api/tasks/extract'): 'routes.manager_assistant',
}


class Phase10VoiceManagerRouteContractTests(unittest.TestCase):
    def test_extracted_routes_registered_once_and_owned_by_modules(self):
        routes = list(iter_app_routes(backend.app))
        for (method, path), module_name in EXPECTED.items():
            matches = [
                route for route in routes
                if route.path == path and method in getattr(route, 'methods', set())
            ]
            self.assertEqual(len(matches), 1, f'{method} {path} must be registered exactly once')
            endpoint = matches[0].endpoint
            self.assertEqual(endpoint.__module__, module_name)

    def test_main_no_longer_declares_phase10_http_implementations(self):
        main_path = os.path.join(os.path.dirname(__file__), '..', 'backend', 'main.py')
        with open(main_path, encoding='utf-8') as handle:
            source = handle.read()
        for method, path in EXPECTED:
            decorator = f'@app.{method.lower()}("{path}")'
            decorator_single = f"@app.{method.lower()}('{path}')"
            self.assertNotIn(decorator, source)
            self.assertNotIn(decorator_single, source)


if __name__ == '__main__':
    unittest.main()
