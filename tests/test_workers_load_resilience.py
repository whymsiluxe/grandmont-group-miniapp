"""Regression tests for the 2026-09-27 owner-reported Workers-screen outage:
GET /api/workers crashed whenever any worker_profiles.json entry was
malformed (not a dict) -- p.get(...) raised AttributeError for that uid and
took down the ENTIRE roster, not just that one worker. Independent root
cause from the Objects crash (different endpoint, different malformed
record shape)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402


class WorkersLoadResilienceTests(unittest.TestCase):
    def test_malformed_profile_entry_does_not_crash_roster(self):
        roles = {'1': 'owner', '10': 'worker', '20': 'worker'}
        profiles = {
            '10': {'name': 'Ivan', 'skills': ['stukatur']},
            '20': 'not-a-dict-profile',  # malformed legacy record
        }
        with patch.object(backend, '_load_roles', return_value=roles), \
             patch.object(backend, '_load_worker_profiles', return_value=profiles), \
             patch.object(backend, '_last_seen', {}):
            result = backend.list_workers(user={'id': '1'})
        uids = sorted(w['user_id'] for w in result['workers'])
        self.assertEqual(uids, ['1', '10', '20'])
        worker_20 = next(w for w in result['workers'] if w['user_id'] == '20')
        self.assertEqual(worker_20['skills'], [])
        self.assertEqual(worker_20['role'], 'worker')

    def test_malformed_skills_field_falls_back_to_empty_list(self):
        roles = {'10': 'worker'}
        profiles = {'10': {'name': 'Ivan', 'skills': 'not-a-list'}}
        with patch.object(backend, '_load_roles', return_value=roles), \
             patch.object(backend, '_load_worker_profiles', return_value=profiles), \
             patch.object(backend, '_last_seen', {}):
            result = backend.list_workers(user={'id': '1'})
        self.assertEqual(result['workers'][0]['skills'], [])

    def test_valid_workers_remain_visible_alongside_a_malformed_one(self):
        roles = {'1': 'owner', '10': 'worker', '20': 'worker'}
        profiles = {'10': {'name': 'Ivan'}, '20': ['garbage', 'shape']}
        with patch.object(backend, '_load_roles', return_value=roles), \
             patch.object(backend, '_load_worker_profiles', return_value=profiles), \
             patch.object(backend, '_last_seen', {}):
            result = backend.list_workers(user={'id': '1'})
        names = {w['user_id']: w['name'] for w in result['workers']}
        self.assertEqual(names['10'], 'Ivan')

    def test_role_default_stays_none_not_worker_for_unlisted_uid(self):
        """Preserves the 09.09 access-invariant fix this endpoint already
        had -- sanitization must not reintroduce a silent 'worker' default
        for a uid with no roles.json entry."""
        roles = {}
        profiles = {'30': {'name': 'ProfileOnly'}}
        with patch.object(backend, '_load_roles', return_value=roles), \
             patch.object(backend, '_load_worker_profiles', return_value=profiles), \
             patch.object(backend, '_last_seen', {}):
            result = backend.list_workers(user={'id': '1'})
        w = result['workers'][0]
        self.assertIsNone(w['role'])
        self.assertFalse(w['access_granted'])


if __name__ == '__main__':
    unittest.main()
