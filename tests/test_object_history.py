import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402
import core.permissions as permissions  # noqa: E402


OWNER = {'id': '1', 'first_name': 'Борис'}
WORKER = {'id': '10', 'first_name': 'Иван'}


class FakeObjekteLib:
    def __init__(self):
        self.updated_fields = []
        self.stage_status_updates = []
        self.completed = []
        self.rows = [
            {'_row': 7, 'Название этапа': 'Потолок', 'Статус': 'в процессе', 'ID строки этапа': 'OBJ-1-S1'},
        ]

    def update_object_field(self, object_id, field, value):
        self.updated_fields.append((object_id, field, value))

    def all_stages(self, object_id):
        return list(self.rows)

    def update_stage_status(self, row_num, status, date_str):
        self.stage_status_updates.append((row_num, status, date_str))

    def sync_current_stage(self, object_id):
        return None

    def worker_complete_stage(self, object_id, row_num, worker_user_id, date_str):
        self.completed.append((object_id, row_num, worker_user_id, date_str))

    def append_row_safe(self, sheet, row):
        return None


class ObjectHistoryTests(unittest.TestCase):
    def setUp(self):
        for path in (backend.OBJECT_HISTORY_FILE, backend.OBJECT_ASSIGNMENTS_FILE, backend.OBJECT_INFO_FILE):
            if os.path.exists(path):
                os.remove(path)

    def _profiles_patch(self):
        return patch.object(backend, '_load_worker_profiles', return_value={
            '1': {'name': 'Борис'},
            '10': {'name': 'Иван'},
            '20': {'name': 'Мария'},
        })

    def test_history_endpoint_filters_sorts_and_limits_events(self):
        with self._profiles_patch():
            backend._append_object_history('OBJ-1', 'old', 'Старое событие', user=OWNER, at='2026-09-14T10:00:00')
            backend._append_object_history('OBJ-2', 'other', 'Другой объект', user=OWNER, at='2026-09-14T12:00:00')
            backend._append_object_history('OBJ-1', 'new', 'Новое событие', user=WORKER, at='2026-09-14T11:00:00')

        result = backend.get_object_history('OBJ-1', limit=10, user=OWNER, _=None)
        self.assertEqual([e['kind'] for e in result['history']], ['new', 'old'])
        self.assertEqual(result['history'][0]['actor_name'], 'Иван')

        limited = backend.get_object_history('OBJ-1', limit=1, user=OWNER, _=None)
        self.assertEqual(len(limited['history']), 1)

    def test_assignment_creation_appends_worker_assigned_history(self):
        rows = [['ID объекта', 'Статус'], ['OBJ-1', 'В работе']]
        with self._profiles_patch(), \
             patch.object(backend, '_load_abwesenheit', return_value=[]), \
             patch.object(backend, '_load_roles', return_value={'10': 'worker'}), \
             patch.object(backend, '_cached_get_used_range', return_value=rows):
            body = backend.AssignBody(
                user_id='10', work_type_id='tile_work',
                date_from='2026-09-15', date_to='2026-09-16',
                task_note='закончить потолок',
            )
            backend.assign_user('OBJ-1', body, user=OWNER, _=None)

        history = backend.get_object_history('OBJ-1', limit=10, user=OWNER, _=None)['history']
        self.assertEqual(len(history), 1)
        event = history[0]
        self.assertEqual(event['kind'], 'worker_assigned')
        self.assertEqual(event['meta']['worker_id'], '10')
        self.assertIn('Иван', event['title'])

    def test_object_status_and_stage_events_are_human_readable(self):
        fake = FakeObjekteLib()
        rows = [['ID объекта', 'Статус'], ['OBJ-1', 'Пауза']]
        with self._profiles_patch(), \
             patch.object(backend, '_load_repo_objekte_lib', return_value=fake), \
             patch.object(backend, '_cached_get_used_range', return_value=rows):
            backend.update_object_status('OBJ-1', backend.StatusBody(status='В работе'), user=OWNER, _=None)
            backend.update_stage('OBJ-1', 7, backend.StageStatusBody(status='готово'), user=OWNER, _=None)
            backend.worker_complete_stage('OBJ-1', 7, user=WORKER, _=None)

        history = backend.get_object_history('OBJ-1', limit=10, user=OWNER, _=None)['history']
        kinds = {event['kind'] for event in history}
        self.assertIn('object_status_changed', kinds)
        self.assertIn('stage_status_changed', kinds)
        self.assertIn('stage_completed', kinds)
        stage_event = next(event for event in history if event['kind'] == 'stage_status_changed')
        self.assertIn('Потолок', stage_event['subtitle'])
        status_event = next(event for event in history if event['kind'] == 'object_status_changed')
        self.assertEqual(status_event['meta']['old_status'], 'Пауза')

    def test_defect_creation_appends_history(self):
        ticket = {
            'id': 'M1', 'object_id': 'OBJ-1', 'description': 'Трещина у окна',
            'status': 'open', 'photo_paths': [], 'assigned_worker_id': '20',
        }
        fake = FakeObjekteLib()
        with self._profiles_patch(), \
             patch.object(backend.ml, 'create_ticket', return_value=ticket), \
             patch.object(backend, '_load_repo_objekte_lib', return_value=fake):
            asyncio.run(backend.create_mangel_ticket(
                object_id='OBJ-1', description='Трещина у окна',
                assigned_worker_id='20', file=None, user=OWNER, role='owner',
            ))

        history = backend.get_object_history('OBJ-1', limit=10, user=OWNER, _=None)['history']
        self.assertEqual(history[0]['kind'], 'defect_created')
        self.assertEqual(history[0]['meta']['ticket_id'], 'M1')
        self.assertIn('Трещина', history[0]['subtitle'])

    def test_history_route_registered(self):
        paths = {route.path for route in iter_app_routes(backend.app)}
        self.assertIn('/api/objects/{object_id}/history', paths)


if __name__ == '__main__':
    unittest.main()
