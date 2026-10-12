"""Раунд 3, задача 5.2: закрытая потребность ОСТАЁТСЯ в JSON (для счётчика/фильтра
"Выполненные"), а не удаляется. Архив в Sheets — только при первом закрытии.
Переоткрытие снимает closed_at.

All Needs persistence assertions use a real temporary TASKS_FILE so route/module
extraction cannot be hidden by monkey-patching the old main._load_tasks location.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from fastapi import HTTPException  # noqa: E402

OWNER = {'id': 1, 'first_name': 'Boss'}


class NeedsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='tasks-needs-workflow-')
        self._orig_tasks_file = backend.TASKS_FILE
        backend.TASKS_FILE = os.path.join(self._tmp.name, 'tasks.json')

    def tearDown(self):
        backend.TASKS_FILE = self._orig_tasks_file
        self._tmp.cleanup()

    def _run(self, tasks, task_id, status):
        backend._save_tasks(tasks)
        with patch.object(backend, '_load_repo_objekte_lib', side_effect=RuntimeError('no sheets')):
            result = backend.update_task_status(task_id, backend.TaskStatusBody(status=status), user=OWNER, _=None)
        return result, backend._load_tasks()

    def test_worker_gets_403(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.require_owner(role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_closed_task_is_retained_not_removed(self):
        tasks = [{'id': 'T1', 'status': 'в работе', 'created_at': 1}]
        result, saved = self._run(tasks, 'T1', 'закрыто')
        self.assertEqual(result['status'], 'закрыто')
        self.assertIsNotNone(result.get('closed_at'))
        self.assertEqual([task['id'] for task in saved], ['T1'])

    def test_reopen_clears_closed_at(self):
        tasks = [{'id': 'T1', 'status': 'закрыто', 'created_at': 1, 'closed_at': 999}]
        result, saved = self._run(tasks, 'T1', 'открыто')
        self.assertEqual(result['status'], 'открыто')
        self.assertIsNone(result['closed_at'])
        self.assertIsNone(saved[0]['closed_at'])

    def test_accept_transition(self):
        tasks = [{'id': 'T1', 'status': 'открыто', 'created_at': 1}]
        result, _ = self._run(tasks, 'T1', 'в работе')
        self.assertEqual(result['status'], 'в работе')

    def test_archive_only_on_first_close(self):
        tasks = [{'id': 'T1', 'status': 'закрыто', 'created_at': 1, 'closed_at': 5}]
        backend._save_tasks(tasks)
        called = {'n': 0}

        def _sheets():
            called['n'] += 1
            raise RuntimeError('boom')

        with patch.object(backend, '_load_repo_objekte_lib', side_effect=_sheets):
            backend.update_task_status('T1', backend.TaskStatusBody(status='закрыто'), user=OWNER, _=None)
        self.assertEqual(called['n'], 0)

    def test_unknown_task_404(self):
        backend._save_tasks([])
        with self.assertRaises(HTTPException) as ctx:
            backend.update_task_status('nope', backend.TaskStatusBody(status='в работе'), user=OWNER, _=None)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_overdue_open_task_is_owner_alert(self):
        now = int(time.time())
        backend._save_tasks([{
            'id': 'T1', 'status': 'открыто', 'title': 'Нужен цемент',
            'object_id': 'OBJ-1', 'from_name': 'Ivan', 'due_at': now - 10,
            'priority': 'срочно', 'created_at': now - 3600,
        }])
        with patch.object(backend, '_cached_get_used_range', return_value=None), \
             patch.object(backend, '_load_repo_tools_lib', side_effect=RuntimeError('no tools')), \
             patch.object(backend, '_load_abwesenheit', return_value=[]), \
             patch.object(backend, '_load_critical_alerts', return_value=[]), \
             patch.object(backend, '_load_activity_alerts', return_value=[]), \
             patch.object(backend, '_load_alert_dismissals', return_value={}):
            alerts = backend.get_alerts(user=OWNER, role='owner')['alerts']

        overdue = [alert for alert in alerts if alert.get('task_overdue')]
        self.assertEqual(len(overdue), 1)
        self.assertEqual(overdue[0]['id'], 'task-overdue-T1')
        self.assertEqual(overdue[0]['type'], 'red')

    def test_future_or_closed_task_is_not_overdue_alert(self):
        now = int(time.time())
        backend._save_tasks([
            {'id': 'future', 'status': 'открыто', 'title': 'Позже', 'due_at': now + 3600},
            {'id': 'closed', 'status': 'закрыто', 'title': 'Готово', 'due_at': now - 10},
        ])
        alerts = backend._overdue_task_alerts(now=now)
        self.assertEqual(alerts, [])


if __name__ == '__main__':
    unittest.main()
