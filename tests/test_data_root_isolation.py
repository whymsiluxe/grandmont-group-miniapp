"""Regression test: DATA_ROOT must never be the production path during pytest.

This test exists to prove that the conftest.py import-time guard works and that
main.py's own firewall fires if the guard is ever bypassed. If this test fails,
running ANY test suite could overwrite production data.

Failure here means: conftest.py was removed/broken OR main.py's guard was removed.
Fix: restore both before running the suite again.
"""
import os
import subprocess
import sys
import tempfile
import unittest


class DataRootIsolationTests(unittest.TestCase):

    def test_current_data_root_is_not_production(self):
        """The current pytest session must have isolated DATA_ROOT (conftest.py ran)."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
        import main as backend
        self.assertNotEqual(
            backend.DATA_ROOT,
            '/home/promonta/agent/miniapp',
            "DATA_ROOT is the production path — conftest.py guard failed!",
        )

    def test_data_root_contains_pytest_prefix(self):
        """conftest.py uses 'grandmont-group-pytest-' prefix; standalone tests use 'grandmont-group-test-'."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
        import main as backend
        self.assertTrue(
            'grandmont-group-pytest-' in backend.DATA_ROOT or 'grandmont-group-test-' in backend.DATA_ROOT,
            f"DATA_ROOT does not have expected prefix: {backend.DATA_ROOT}",
        )

    def test_main_raises_if_test_env_and_prod_path(self):
        """main.py must raise RuntimeError when GRANDMONT_GROUP_ENV=test + DATA_ROOT=prod.

        This simulates the exact failure mode that caused the 2026-09-08 incident:
        a test process importing main.py without MINIAPP_DATA_ROOT set.
        The guard in main.py must fire before any file I/O happens.
        """
        env = {
            **os.environ,
            'GRANDMONT_GROUP_ENV': 'test',
            'MINIAPP_DATA_ROOT': '/home/grandmont/agent/miniapp',
            'BOT_TOKEN': 'ci-dummy',
        }
        # Run in a subprocess so sys.modules cache doesn't interfere
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "backend"); import main'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertNotEqual(result.returncode, 0,
                            "Expected RuntimeError but process exited 0")
        self.assertIn('REFUSING TO RUN TESTS AGAINST PRODUCTION DATA ROOT',
                      result.stderr + result.stdout)

    def test_legacy_promonta_env_name_still_arms_guard(self):
        """Grandmont Group rebrand (26.09): PROMONTA_ENV was renamed to
        GRANDMONT_GROUP_ENV. A runner that still sets only the pre-rebrand name must
        keep the prod-DATA_ROOT guard armed (main._env_compat fallback)."""
        env = {k: v for k, v in os.environ.items() if k != 'GRANDMONT_GROUP_ENV'}
        env.update({
            'PROMONTA_ENV': 'test',
            'MINIAPP_DATA_ROOT': '/home/grandmont/agent/miniapp',
            'BOT_TOKEN': 'ci-dummy',
        })
        # Plain subprocess import (pytest not in sys.modules): only the env var can
        # arm the guard here.
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "backend"); import main'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertNotEqual(result.returncode, 0,
                            "Expected RuntimeError but process exited 0")
        self.assertIn('REFUSING TO RUN TESTS AGAINST PRODUCTION DATA ROOT',
                      result.stderr + result.stdout)

    def test_main_allows_temp_path_even_with_test_env(self):
        """main.py must NOT raise when GRANDMONT_GROUP_ENV=test + DATA_ROOT=temp dir."""
        tmp = tempfile.mkdtemp(prefix='grandmont-group-guard-test-')
        env = {
            **os.environ,
            'GRANDMONT_GROUP_ENV': 'test',
            'MINIAPP_DATA_ROOT': tmp,
            'BOT_TOKEN': 'ci-dummy',
        }
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "backend"); import main; print("OK")'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        self.assertEqual(result.returncode, 0,
                         f"Unexpected failure: {result.stderr}")
        self.assertIn('OK', result.stdout)

    def test_main_allows_prod_path_without_test_env(self):
        """Production process (GRANDMONT_GROUP_ENV unset) must still work even with prod DATA_ROOT.

        This test verifies backward compatibility: the existing production service
        (which does NOT set GRANDMONT_GROUP_ENV) is not affected by the guard.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ('GRANDMONT_GROUP_ENV', 'PROMONTA_ENV', 'MINIAPP_DATA_ROOT')}
        env['BOT_TOKEN'] = 'ci-dummy'
        # Don't set MINIAPP_DATA_ROOT or GRANDMONT_GROUP_ENV — simulates production
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, "backend"); import main; print("OK")'],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), '..'),
        )
        # Must NOT raise RuntimeError (production backward-compat)
        self.assertNotIn('REFUSING TO RUN TESTS', result.stderr + result.stdout)
        # returncode may be 0 or non-0 depending on other startup side effects,
        # but should not be a RuntimeError on the DATA_ROOT guard
        if result.returncode != 0:
            self.assertNotIn('RuntimeError', result.stderr)