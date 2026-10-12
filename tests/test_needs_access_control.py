"""03.08 (ТЗ Задача 4): access control на Потребности (/api/tasks GET+POST) -- worker
должен иметь has_active_object_access(user_id, object_id), чтобы читать/создавать
потребность НА КОНКРЕТНОМ объекте. Owner -- доступ всегда. Запрос list_tasks() без
object_id (глобальный экран "мои потребности") НЕ трогается этой задачей.

Tests use a real temporary TASKS_FILE so they validate public storage behavior
rather than the historical implementation location of ``main._load_tasks``.
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402

OWNER = {'id': 1, 'first_name': 'Boss'}
WORKER_A = {'id': 10, 'first_name': 'Ivan'}


def _assignment(uid, status='accepted', date_from=None, date_to=None):
    if date_from is None:
        date_from = (date.today() - timedelta(days=5)).isoformat()
    if date_to is None:
        date_to = (date.today() + timedelta(days=25)).isoformat()
    return {'user_id': str(uid), 'status': status, 'date_from': date_from, 'date_to': date_to}


class _TempTasksStoreMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='tasks-access-control-')
        self._orig_tasks_file = backend.TASKS_FILE
        backend.TASKS_FILE = os.path.join(self._tmp.name, 'tasks.json')

    def tearDown(self):
        backend.TASKS_FILE = self._orig_tasks_file
        self._tmp.cleanup()


class ListTasksAccessTests(_TempTasksStoreMixin, unittest.TestCase):
    def test_active_worker_reads_object_tasks(self):
        assignments = {'OBJ-1': [_assignment(10)]}
        backend._save_tasks([])
        with patch.object(backend, '_load_assignments', return_value=assignments):
            result = backend.list_tasks(object_id='OBJ-1', user=WORKER_A, role='worker')
        self.assertEqual(result['tasks'], [])

    def test_worker_without_object_access_gets_403(self):
        with patch.object(backend, '_load_assignments', return_value={}):
            with self.assertRaises(HTTPException) as ctx:
                backend.list_tasks(object_id='OBJ-1', user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_future_assignment_denies_read_access(self):
        assignments = {'OBJ-1': [_assignment(10, date_from='2099-01-01', date_to='2099-01-10')]}
        with patch.object(backend, '_load_assignments', return_value=assignments):
            with self.assertRaises(HTTPException) as ctx:
                backend.list_tasks(object_id='OBJ-1', user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_pending_assignment_denies_read_access(self):
        assignments = {'OBJ-1': [_assignment(10, status='pending')]}
        with patch.object(backend, '_load_assignments', return_value=assignments):
            with self.assertRaises(HTTPException) as ctx:
                backend.list_tasks(object_id='OBJ-1', user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_declined_assignment_denies_read_access(self):
        assignments = {'OBJ-1': [_assignment(10, status='declined')]}
        with patch.object(backend, '_load_assignments', return_value=assignments):
            with self.assertRaises(HTTPException) as ctx:
                backend.list_tasks(object_id='OBJ-1', user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_expired_assignment_denies_read_access(self):
        assignments = {'OBJ-1': [_assignment(10, date_from='2020-01-01', date_to='2020-01-10')]}
        with patch.object(backend, '_load_assignments', return_value=assignments):
            with self.assertRaises(HTTPException) as ctx:
                backend.list_tasks(object_id='OBJ-1', user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_always_has_read_access(self):
        backend._save_tasks([])
        with patch.object(backend, '_load_assignments', return_value={}):
            result = backend.list_tasks(object_id='OBJ-1', user=OWNER, role='owner')
        self.assertEqual(result['tasks'], [])

    def test_no_object_id_worker_sees_only_own_unaffected(self):
        # object_id не передан -- доступ к объекту не должен проверяться; worker
        # по-прежнему видит только собственные записи глобального экрана.
        backend._save_tasks([
            {'id': 'mine', 'from_user_id': '10', 'priority': 'обычная', 'created_at': 2},
            {'id': 'other', 'from_user_id': '11', 'priority': 'срочно', 'created_at': 3},
        ])
        with patch.object(backend, '_load_assignments', side_effect=AssertionError('must not read assignments')):
            result = backend.list_tasks(object_id='', user=WORKER_A, role='worker')
        self.assertEqual([task['id'] for task in result['tasks']], ['mine'])


class CreateTaskAccessTests(_TempTasksStoreMixin, unittest.TestCase):
    def test_active_worker_creates_task(self):
        assignments = {'OBJ-1': [_assignment(10)]}
        body = backend.TaskCreateBody(title='Нужен цемент', object_id='OBJ-1')
        with patch.object(backend, '_load_assignments', return_value=assignments), \
             patch.object(backend, '_load_roles', return_value={'1': 'owner', '10': 'worker'}), \
             patch.object(backend, '_get_worker_profile', return_value={'name': 'Ivan'}), \
             patch.object(backend, 'send_telegram_message'):
            task = backend.create_task(body, user=WORKER_A, role='worker')
        self.assertEqual(task['object_id'], 'OBJ-1')
        self.assertEqual([item['id'] for item in backend._load_tasks()], [task['id']])

    def test_worker_without_object_access_cannot_create(self):
        body = backend.TaskCreateBody(title='Нужен цемент', object_id='OBJ-1')
        with patch.object(backend, '_load_assignments', return_value={}):
            with self.assertRaises(HTTPException) as ctx:
                backend.create_task(body, user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_pending_assignment_cannot_create(self):
        assignments = {'OBJ-1': [_assignment(10, status='pending')]}
        body = backend.TaskCreateBody(title='Нужен цемент', object_id='OBJ-1')
        with patch.object(backend, '_load_assignments', return_value=assignments):
            with self.assertRaises(HTTPException) as ctx:
                backend.create_task(body, user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_cannot_create_task_regardless_of_access(self):
        body = backend.TaskCreateBody(title='Нужен цемент', object_id='OBJ-1')
        with self.assertRaises(HTTPException) as ctx:
            backend.create_task(body, user=OWNER, role='owner')
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn('работники', ctx.exception.detail)


if __name__ == '__main__':
    unittest.main()
