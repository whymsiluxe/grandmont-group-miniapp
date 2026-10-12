import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


EXPECTED = {
    ('GET', '/api/alerts'),
    ('POST', '/api/alerts/dismiss'),
}


class Phase13AlertsRouteContractTests(unittest.TestCase):
    def test_routes_registered_once(self):
        routes = list(iter_app_routes(backend.app))
        for method, path in EXPECTED:
            matches = [
                route for route in routes
                if route.path == path and method in getattr(route, 'methods', set())
            ]
            self.assertEqual(len(matches), 1, f'{method} {path} must be registered exactly once')

    def test_extracted_module_has_no_back_import(self):
        module_path = os.path.join(os.path.dirname(__file__), '..', 'backend', 'routes', 'alerts.py')
        with open(module_path, encoding='utf-8') as handle:
            source = handle.read()
        self.assertNotIn('import main', source)
        self.assertNotIn('from main import', source)


if __name__ == '__main__':
    unittest.main()
