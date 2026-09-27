"""Regression tests for the 2026-09-27 owner-reported outage: GET /api/objects
(and GET /api/my-assignments, which Start Shift's object picker depends on)
crashed with a 500 whenever ANY assignment record in object_assignments.json
was malformed -- not a dict, or a dict missing 'user_id'. That single crash
also took down the Documents gallery (frontend/js/document-gallery.js loads
GET /api/objects first) and Start Shift's own object picker
(frontend/js/worker-checkin-fab.js _openWorkerObjectPicker calls the same
endpoint). This file locks: one malformed record is skipped, not fatal;
valid objects/assignments still load; worker access scoping is unchanged.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402


class FakeObjekteLib:
    def all_stages_grouped(self):
        return {}


OWNER = {'id': '1', 'first_name': 'Boris'}
WORKER = {'id': '10', 'first_name': 'Ivan'}
FOREIGN_WORKER = {'id': '99', 'first_name': 'Foreign'}

SHEET_ROWS = [
    ['ID объекта', 'Объект', 'Адрес', 'Статус'],
    ['OBJ-1', 'Дом на Ленина', 'ул. Ленина 1', 'В работе'],
]


class ObjectsLoadResilienceTests(unittest.TestCase):
    def _patches(self, assignments, profiles=None, images=None):
        return (
            patch.object(backend, '_cached_get_used_range', return_value=SHEET_ROWS),
            patch.object(backend, '_load_assignments', return_value=assignments),
            patch.object(backend, '_load_worker_profiles', return_value=profiles or {}),
            patch.object(backend, '_load_object_images', return_value=images or {}),
            patch.object(backend, '_load_repo_objekte_lib', return_value=FakeObjekteLib()),
        )

    def test_malformed_assignment_missing_user_id_does_not_crash_owner_list(self):
        """The exact reported crash: a[.user_id] KeyError on the owner path."""
        assignments = {'OBJ-1': [{'status': 'accepted', 'date_from': '', 'date_to': ''}]}
        patches = self._patches(assignments)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = backend.list_objects(user=OWNER, role='owner')
        self.assertEqual(len(result['objects']), 1)
        self.assertEqual(result['objects'][0]['assigned_users'], [])

    def test_assignment_entry_not_a_dict_does_not_crash_owner_or_worker_list(self):
        assignments = {'OBJ-1': ['not-a-dict', None, 42]}
        patches = self._patches(assignments)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            owner_result = backend.list_objects(user=OWNER, role='owner')
            worker_result = backend.list_objects(user=WORKER, role='worker')
        self.assertEqual(len(owner_result['objects']), 1)
        self.assertEqual(len(worker_result['objects']), 1)
        self.assertEqual(worker_result['objects'][0]['assigned_users'], [])

    def test_assignments_value_not_a_list_does_not_crash(self):
        """object_assignments.json[oid] itself malformed (a dict instead of a list)."""
        assignments = {'OBJ-1': {'unexpected': 'shape'}}
        patches = self._patches(assignments)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = backend.list_objects(user=OWNER, role='owner')
        self.assertEqual(len(result['objects']), 1)

    def test_malformed_worker_profile_does_not_crash_object_list(self):
        assignments = {'OBJ-1': [{'user_id': '10', 'status': 'accepted', 'date_from': '', 'date_to': ''}]}
        profiles = {'10': 'not-a-dict-profile'}
        patches = self._patches(assignments, profiles=profiles)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = backend.list_objects(user=OWNER, role='owner')
        self.assertEqual(len(result['objects']), 1)
        self.assertEqual(result['objects'][0]['assigned_users'][0]['user_id'], '10')

    def test_malformed_date_fields_do_not_crash_owner_filtering(self):
        """Non-string date_from/date_to (e.g. an int from a bad legacy write)
        used to raise TypeError comparing str <= non-str in the today-window check."""
        assignments = {'OBJ-1': [{'user_id': '10', 'status': 'accepted', 'date_from': 20260901, 'date_to': None}]}
        patches = self._patches(assignments)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = backend.list_objects(user=OWNER, role='owner')
        self.assertEqual(len(result['objects']), 1)

    def test_valid_objects_still_load_alongside_a_malformed_assignment(self):
        assignments = {'OBJ-1': [
            {'status': 'accepted'},  # malformed: no user_id
            {'user_id': '10', 'status': 'accepted', 'date_from': '', 'date_to': ''},  # valid
        ]}
        patches = self._patches(assignments)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = backend.list_objects(user=OWNER, role='owner')
        self.assertEqual(len(result['objects']), 1)
        uids = [u['user_id'] for u in result['objects'][0]['assigned_users']]
        self.assertEqual(uids, ['10'])

    def test_worker_object_access_scoping_unchanged_by_sanitization(self):
        """A worker not on the object's assignment list still sees no
        my_assignments for it -- sanitization must not accidentally grant
        access to a foreign worker."""
        assignments = {'OBJ-1': [{'user_id': '10', 'status': 'accepted', 'date_from': '', 'date_to': ''}]}
        patches = self._patches(assignments)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = backend.list_objects(user=FOREIGN_WORKER, role='worker')
        self.assertEqual(result['objects'][0]['my_assignments'], [])

    def test_my_assignments_endpoint_skips_malformed_records_for_start_shift(self):
        """GET /api/my-assignments backs the Start Shift object picker
        (_openWorkerObjectPicker in worker-checkin-fab.js) -- a malformed
        sibling record for another object must not hide the worker's own
        valid assignment."""
        assignments = {
            'OBJ-1': [{'user_id': '10', 'id': 'a1', 'date_from': '2026-09-01', 'date_to': '2026-09-30'}],
            'OBJ-2': ['garbage', {'status': 'accepted'}],  # no user_id at all
        }
        patches = self._patches(assignments)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = backend.my_assignments(user=WORKER)
        object_ids = [a['object_id'] for a in result['assignments']]
        self.assertEqual(object_ids, ['OBJ-1'])


if __name__ == '__main__':
    unittest.main()
