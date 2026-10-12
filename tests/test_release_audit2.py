"""31.07 Release-аудит доп.раунд: тесты для П2 (roadmap_lib isolated loader),
П3 (worker object-mutation scoping), П4 (corrupt-JSON safe handling), П5 (abwesenheit
redaction), П6 (tool checkout race), П7 (archived chat attachment access).

Тот же стиль, что tests/test_chat_actions.py / tests/test_tools.py -- plain unittest,
route handlers вызываются напрямую, зависимости мокаются через patch.object на
РЕАЛЬНЫЙ модуль backend (import main as backend), не на отдельный import.

Run:
    /home/promonta/agent/miniapp/.venv/bin/python3 -m unittest tests.test_release_audit2 -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402

OWNER = {'id': 1, 'first_name': 'Boss'}
WORKER_A = {'id': 10, 'first_name': 'Ivan'}
WORKER_B = {'id': 20, 'first_name': 'Petr'}


# ---------- П2: roadmap_lib isolated loader ----------

class RoadmapLibIsolatedLoaderTests(unittest.TestCase):
    def test_module_level_rl_is_repo_file(self):
        self.assertIn('backend' + os.sep + 'roadmap_lib.py', backend.rl.__file__.replace('/', os.sep))

    def test_loader_returns_cached_singleton(self):
        self.assertIs(backend._load_repo_roadmap_lib(), backend.rl)

    def test_loader_uses_explicit_path_not_global_sys_path(self):
        self.assertEqual(backend.ROADMAP_LIB_PATH, os.path.join(backend.BACKEND_DIR, 'roadmap_lib.py'))
        self.assertTrue(os.path.isfile(backend.ROADMAP_LIB_PATH))


# ---------- П3: worker object-mutation scoping ----------

class WorkerStageMutationScopeTests(unittest.TestCase):
    def _accepted_assignment(self, uid):
        return {'OBJ-1': [{'user_id': str(uid), 'status': 'accepted'}]}

    def test_worker_without_assignment_cannot_create_stage(self):
        with patch.object(backend, '_load_assignments', return_value={}):
            with self.assertRaises(HTTPException) as ctx:
                backend.require_object_access('OBJ-1', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_worker_a_cannot_mutate_worker_b_object(self):
        assignments = {'OBJ-1': [{'user_id': str(WORKER_A['id']), 'status': 'accepted'}]}
        with patch.object(backend, '_load_assignments', return_value=assignments):
            with self.assertRaises(HTTPException) as ctx:
                backend.require_object_access('OBJ-1', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_pending_assignment_does_not_grant_write_access(self):
        assignments = {'OBJ-1': [{'user_id': str(WORKER_A['id']), 'status': 'pending'}]}
        with patch.object(backend, '_load_assignments', return_value=assignments):
            with self.assertRaises(HTTPException) as ctx:
                backend.require_object_access('OBJ-1', user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_declined_assignment_does_not_grant_write_access(self):
        assignments = {'OBJ-1': [{'user_id': str(WORKER_A['id']), 'status': 'declined'}]}
        with patch.object(backend, '_load_assignments', return_value=assignments):
            with self.assertRaises(HTTPException) as ctx:
                backend.require_object_access('OBJ-1', user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_assigned_worker_can_write(self):
        with patch.object(backend, '_load_assignments', return_value=self._accepted_assignment(WORKER_A['id'])):
            # не должно бросить
            backend.require_object_access('OBJ-1', user=WORKER_A, role='worker')

    def test_owner_can_write_any_object(self):
        with patch.object(backend, '_load_assignments', return_value={}):
            backend.require_object_access('OBJ-1', user=OWNER, role='owner')

    def test_create_stage_endpoint_wired_to_require_object_access(self):
        """create_stage/update_stage_description_endpoint должны объявлять
        require_object_access как Depends -- проверяем по сигнатуре, не через живой
        вызов (тот бьёт по реальному objekte_lib.get_used_range, вне scope этого теста)."""
        import inspect
        sig = inspect.signature(backend.create_stage)
        dep = sig.parameters['_'].default
        self.assertIs(dep.dependency, backend.require_object_access)
        sig2 = inspect.signature(backend.update_stage_description_endpoint)
        dep2 = sig2.parameters['_'].default
        self.assertIs(dep2.dependency, backend.require_object_access)


# ---------- П4: corrupt-JSON safe handling ----------

class CorruptJsonSafeHandlingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, 'critical_store.json')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{not valid json!!')
        backend.CRITICAL_JSON_PATHS.add(self.path)

    def tearDown(self):
        backend.CRITICAL_JSON_PATHS.discard(self.path)
        for fn in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, fn))
        os.rmdir(self.tmpdir)

    def test_corrupt_critical_json_raises_not_silently_defaults(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])

    def test_corrupt_critical_json_gets_quarantined_not_overwritten(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])
        self.assertFalse(os.path.exists(self.path))
        # 31.07 (доп.раунд, П1): помимо quarantine-копии теперь также создаётся
        # постоянный .corrupt-lock marker -- исключаем его из подсчёта snapshot-копий.
        quarantined = [f for f in os.listdir(self.tmpdir) if '.corrupt-' in f and not f.endswith('.corrupt-lock')]
        self.assertEqual(len(quarantined), 1)
        with open(os.path.join(self.tmpdir, quarantined[0]), encoding='utf-8') as f:
            self.assertEqual(f.read(), '{not valid json!!')
        self.assertTrue(os.path.exists(backend._corrupt_lock_path(self.path)))

    def test_transaction_on_corrupt_critical_json_does_not_write_default(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend.update_json_transaction(self.path, list, lambda d: d.append('x'))
        # файл должен быть унесён в карантин, НЕ переписан пустым []
        self.assertFalse(os.path.exists(self.path))

    def test_non_critical_path_still_falls_back_to_default_as_before(self):
        non_critical = os.path.join(self.tmpdir, 'not_critical.json')
        with open(non_critical, 'w', encoding='utf-8') as f:
            f.write('{broken')
        try:
            result = backend._safe_load_json(non_critical, {'default': True})
            self.assertEqual(result, {'default': True})
            self.assertTrue(os.path.exists(non_critical))  # НЕ унесён в карантин
        finally:
            os.remove(non_critical)

    def test_valid_json_on_critical_path_still_works(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'ok': True}, f)
        result = backend._safe_load_json(self.path, {})
        self.assertEqual(result, {'ok': True})


class CorruptJsonExceptionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_returns_503(self):
        response = await backend._corrupt_json_handler(None, backend.CorruptJsonError('/tmp/x.json'))
        self.assertEqual(response.status_code, 503)


# ---------- П5: abwesenheit field redaction ----------

class AbwesenheitRedactionTests(unittest.TestCase):
    FULL_ENTRY = {
        'id': 'a1', 'user_id': '10', 'name': 'Ivan', 'date_from': '2099-08-01',
        'date_to': '2099-08-05', 'open_ended': False, 'reason': 'krankheit',
        'note': 'Приватная мед. деталь про операцию', 'start_time': '', 'end_time': '',
        'status': 'approved',
    }

    def _list_from_real_temp_store(self, user, role):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(backend, 'ABWESENHEIT_FILE', os.path.join(td, 'abwesenheit.json')):
            backend._save_abwesenheit([dict(self.FULL_ENTRY)])
            return backend.list_all_abwesenheit(user=user, role=role)

    def test_worker_sees_redacted_fields_for_others(self):
        result = self._list_from_real_temp_store(user=WORKER_B, role='worker')
        entry = result['entries'][0]
        self.assertNotIn('note', entry)
        self.assertEqual(entry['name'], 'Ivan')
        self.assertEqual(entry['status'], 'approved')

    def test_worker_sees_own_full_entry(self):
        result = self._list_from_real_temp_store(user=WORKER_A, role='worker')
        entry = result['entries'][0]
        self.assertEqual(entry['note'], self.FULL_ENTRY['note'])

    def test_owner_sees_full_entry(self):
        result = self._list_from_real_temp_store(user=OWNER, role='owner')
        entry = result['entries'][0]
        self.assertEqual(entry['note'], self.FULL_ENTRY['note'])


# ---------- П6: tool checkout/return race ----------

class ToolCheckoutRaceTests(unittest.TestCase):
    def test_same_lock_instance_for_same_serial(self):
        self.assertIs(backend._lock_for_tool('T-014'), backend._lock_for_tool('T-014'))

    def test_different_serials_get_different_locks(self):
        self.assertIsNot(backend._lock_for_tool('T-014'), backend._lock_for_tool('T-015'))

    def test_concurrent_checkout_second_gets_409_not_two_successes(self):
        free_tool = {'Серийный #': 'T-020', 'Кто взял': '', 'Статус': '', 'ID держателя': ''}
        checkout_calls = []

        fake_tl = MagicMock()
        fake_tl.get_tool.return_value = free_tool
        fake_tl.mapped_status.return_value = 'free'

        def fake_checkout(*a, **kw):
            checkout_calls.append(1)
            fake_tl.mapped_status.return_value = 'in_use'

        fake_tl.checkout_tool.side_effect = fake_checkout

        body = backend.CheckoutBody(object_name='Baustelle X')
        with patch.object(backend, '_load_repo_tools_lib', return_value=fake_tl):
            backend.checkout_tool('T-020', body, user=WORKER_A)
            with self.assertRaises(HTTPException) as ctx:
                backend.checkout_tool('T-020', body, user=WORKER_B)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(checkout_calls), 1)


# ---------- П7: archived chat attachment access ----------

class ArchivedAttachmentAccessTests(unittest.TestCase):
    def test_participant_opens_attachment_from_archived_message(self):
        att = {'file': 'archived1.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}
        archived_msg = {
            'id': 'old1', 'ts': 1000, 'user_id': 10, 'name': 'Ivan', 'text': '',
            'to_user_id': None, 'thread_key': 'obj:OBJ-1', 'attachment': att,
        }
        with patch.object(backend, '_load_chat', return_value=[]), \
             patch.object(backend, '_safe_load_json', return_value=[archived_msg]), \
             patch.object(backend.os.path, 'exists', return_value=True), \
             patch.object(backend, '_check_thread_access'), \
             patch.object(backend, 'FileResponse', return_value='OK') as mock_fr:
            result = backend.get_chat_attachment('archived1.jpg', user=WORKER_A, role='worker')
        self.assertEqual(result, 'OK')
        mock_fr.assert_called_once()

    def test_outsider_denied_for_archived_attachment(self):
        att = {'file': 'archived2.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}
        archived_msg = {
            'id': 'old2', 'ts': 1000, 'user_id': 10, 'name': 'Ivan', 'text': '',
            'to_user_id': None, 'thread_key': 'obj:OBJ-1', 'attachment': att,
        }
        with patch.object(backend, '_load_chat', return_value=[]), \
             patch.object(backend, '_safe_load_json', return_value=[archived_msg]), \
             patch.object(backend.os.path, 'exists', return_value=True), \
             patch.object(backend, '_check_thread_access', side_effect=HTTPException(403, 'no access')):
            with self.assertRaises(HTTPException) as ctx:
                backend.get_chat_attachment('archived2.jpg', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == '__main__':
    unittest.main()
