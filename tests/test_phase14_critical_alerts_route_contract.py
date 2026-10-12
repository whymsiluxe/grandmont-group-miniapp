import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


EXPECTED = {
    ('POST', '/api/critical-alerts/{alert_id}/ack'),
    ('POST', '/api/critical-alerts/{alert_id}/resolve'),
    ('GET', '/api/critical-alerts/{alert_id}/photo/{filename}'),
    ('GET', '/api/critical-alerts/pending'),
    ('POST', '/api/critical-alerts'),
}


class Phase14CriticalAlertsRouteContractTests(unittest.TestCase):
    def test_routes_registered_once_and_owned_by_extracted_module(self):
        routes = list(iter_app_routes(backend.app))
        for method, path in EXPECTED:
            matches = [
                route for route in routes
                if route.path == path and method in getattr(route, 'methods', set())
            ]
            self.assertEqual(len(matches), 1, f'{method} {path} must be registered exactly once')
            self.assertEqual(matches[0].endpoint.__module__, 'routes.critical_alerts')

    def test_main_has_no_critical_alert_http_implementations(self):
        main_path = os.path.join(os.path.dirname(__file__), '..', 'backend', 'main.py')
        with open(main_path, encoding='utf-8') as handle:
            source = handle.read()
        for method, path in EXPECTED:
            self.assertNotIn(f'@app.{method.lower()}("{path}")', source)
            self.assertNotIn(f"@app.{method.lower()}('{path}')", source)

    def test_extracted_module_has_no_back_import(self):
        module_path = os.path.join(os.path.dirname(__file__), '..', 'backend', 'routes', 'critical_alerts.py')
        with open(module_path, encoding='utf-8') as handle:
            source = handle.read()
        self.assertNotIn('import main', source)
        self.assertNotIn('from main import', source)


if __name__ == '__main__':
    unittest.main()
