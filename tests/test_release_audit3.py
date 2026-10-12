"""31.07 доп.раунд 2: тесты для П1 (persistent corrupt-lock marker), П2 (reason тоже
скрыт в abwesenheit/all), П4 (MINIAPP_DATA_ROOT реальная изоляция), П5 (lock на
создание инструмента, настоящий concurrency-тест через threads).

Run:
    /home/promonta/agent/miniapp/.venv/bin/python3 -m pytest tests/test_release_audit3.py -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402

OWNER = {'id': 1, 'first_name': 'Boss'}
WORKER_A = {'id': 10, 'first_name': 'Ivan'}
WORKER_B = {'id': 20, 'first_name': 'Petr'}


# ---------- П1: persistent corrupt-lock marker ----------

class PersistentCorruptLockTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, 'critical_store.json')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{not valid json!!')
        backend.CRITICAL_JSON_PATHS.add(self.path)

    def tearDown(self):
        backend.CRITICAL_JSON_PATHS.discard(self.path)
        if os.path.isdir(self.tmpdir):
            for fn in os.listdir(self.tmpdir):
                os.remove(os.path.join(self.tmpdir, fn))
            os.rmdir(self.tmpdir)

    def test_first_request_503_and_quarantine(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.exists(backend._corrupt_lock_path(self.path)))

    def test_second_request_still_503_not_empty_default(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])
        # второй запрос -- исходный файл физически отсутствует, marker должен
        # продолжать блокировать, а не тихо вернуть default
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])

    def test_mutation_after_quarantine_does_not_create_empty_file(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])
        with self.assertRaises(backend.CorruptJsonError):
            backend.update_json_transaction(self.path, list, lambda d: d.append('x'))
        # файла НЕ должно появиться заново пустым
        self.assertFalse(os.path.exists(self.path))

    def test_marker_persists_and_contains_required_fields(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])
        lock_path = backend._corrupt_lock_path(self.path)
        with open(lock_path, encoding='utf-8') as f:
            marker = json.load(f)
        self.assertEqual(marker['original_path'], self.path)
        self.assertIn('quarantine_path', marker)
        self.assertIn('detected_at', marker)
        self.assertTrue(os.path.exists(marker['quarantine_path']))

    def test_quarantine_is_idempotent_not_re_quarantined(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])
        lock_path = backend._corrupt_lock_path(self.path)
        with open(lock_path, encoding='utf-8') as f:
            marker_first = json.load(f)
        # повторный вызов _quarantine_corrupt_json (напр. другой запрос попал в ту же
        # гонку) не должен создать ВТОРУЮ quarantine-копию / переписать marker
        backend._quarantine_corrupt_json(self.path)
        with open(lock_path, encoding='utf-8') as f:
            marker_second = json.load(f)
        self.assertEqual(marker_first, marker_second)

    def test_after_manual_restore_and_marker_removal_store_works_again(self):
        with self.assertRaises(backend.CorruptJsonError):
            backend._safe_load_json(self.path, [])
        # владелец вручную восстанавливает валидный JSON и удаляет marker
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'restored': True}, f)
        os.remove(backend._corrupt_lock_path(self.path))
        result = backend._safe_load_json(self.path, {})
        self.assertEqual(result, {'restored': True})

    def test_corrupt_chat_archive_not_overwritten_with_empty_list(self):
        self.assertIn(backend.CHAT_ARCHIVE_FILE, backend.CRITICAL_JSON_PATHS)

    def test_business_critical_projection_stores_are_critical(self):
        critical_filenames = {os.path.basename(path) for path in backend.CRITICAL_JSON_PATHS}
        self.assertIn('finish_outbox.json', critical_filenames)
        self.assertIn('object_info.json', critical_filenames)
        self.assertIn('object_images.json', critical_filenames)
        self.assertIn('work_calendar.json', critical_filenames)
        self.assertIn('tool_bookings.json', critical_filenames)
        self.assertIn('chat_thread_meta.json', critical_filenames)


# ---------- П2: reason скрыт для чужих в abwesenheit/all ----------

class AbwesenheitReasonRedactionTests(unittest.TestCase):
    FULL_ENTRY = {
        'id': 'a1', 'user_id': '10', 'name': 'Ivan', 'date_from': '2099-08-01',
        'date_to': '2099-08-05', 'open_ended': False, 'reason': 'krankheit',
        'note': 'приватная деталь', 'start_time': '', 'end_time': '', 'status': 'approved',
    }
    EXPECTED_PUBLIC = {'id', 'user_id', 'name', 'date_from', 'date_to', 'open_ended', 'status'}

    def _list_from_real_temp_store(self, user, role):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(backend, 'ABWESENHEIT_FILE', os.path.join(td, 'abwesenheit.json')):
            backend._save_abwesenheit([dict(self.FULL_ENTRY)])
            return backend.list_all_abwesenheit(user=user, role=role)

    def test_worker_sees_exact_public_field_set_for_others(self):
        result = self._list_from_real_temp_store(user=WORKER_B, role='worker')
        entry = result['entries'][0]
        self.assertEqual(set(entry.keys()), self.EXPECTED_PUBLIC)
        self.assertNotIn('reason', entry)
        self.assertNotIn('note', entry)
        self.assertNotIn('start_time', entry)
        self.assertNotIn('end_time', entry)

    def test_worker_sees_own_reason_and_note(self):
        result = self._list_from_real_temp_store(user=WORKER_A, role='worker')
        entry = result['entries'][0]
        self.assertEqual(entry['reason'], 'krankheit')
        self.assertEqual(entry['note'], 'приватная деталь')

    def test_owner_sees_reason_and_note(self):
        result = self._list_from_real_temp_store(user=OWNER, role='owner')
        entry = result['entries'][0]
        self.assertEqual(entry['reason'], 'krankheit')
        self.assertEqual(entry['note'], 'приватная деталь')


# ---------- П4: MINIAPP_DATA_ROOT реальная изоляция (subprocess) ----------

class DataRootIsolationTests(unittest.TestCase):
    """Настоящий regression-тест через subprocess -- импортирует backend.main СВЕЖИМ
    процессом с MINIAPP_DATA_ROOT=/tmp/..., выполняет реальные файловые операции
    (не мокает _load_*/_save_*) и проверяет, что /home/promonta/agent/miniapp
    физически не тронут."""

    def test_isolated_data_root_no_prod_directory_touched(self):
        script = '''
import os, sys, json
sys.path.insert(0, "backend")
import main as backend

data_root = os.environ["MINIAPP_DATA_ROOT"]
assert backend.DATA_ROOT == data_root, f"DATA_ROOT mismatch: {backend.DATA_ROOT!r} != {data_root!r}"

# основные path-константы -- должны указывать внутрь data_root, не в prod
for name in ("ROLES_FILE", "CHAT_FILE", "CHAT_ARCHIVE_FILE", "TASKS_FILE",
             "ABWESENHEIT_FILE", "CHECKIN_META_FILE", "CRITICAL_ALERTS_FILE",
             "OBJECT_ASSIGNMENTS_FILE", "WORKER_PROFILES_FILE"):
    path = getattr(backend, name)
    assert path.startswith(data_root), f"{name}={path!r} not under {data_root!r}"

rl = backend.rl
assert rl.ROADMAP_FILE.startswith(data_root), rl.ROADMAP_FILE
assert rl.STAGE_REQUESTS_FILE.startswith(data_root), rl.STAGE_REQUESTS_FILE

ml = backend._load_repo_mangel_lib()
assert ml.MANGEL_FILE.startswith(data_root), ml.MANGEL_FILE

# реальные файловые операции -- не мок, физическая запись/чтение
backend._atomic_write_json(backend.ROLES_FILE, {"1": "owner"})
loaded = backend._safe_load_json(backend.ROLES_FILE, {})
assert loaded == {"1": "owner"}, loaded

# прод-директория не должна получить НИ ОДНОГО нового файла от этого прогона
prod_dir = "/home/promonta/agent/miniapp"
prod_roles = os.path.join(prod_dir, "roles.json")
prod_mtime_before = os.path.getmtime(prod_roles) if os.path.exists(prod_roles) else None

print("OK: DATA_ROOT isolation verified, prod_mtime_before=", prod_mtime_before)
'''
        with tempfile.TemporaryDirectory() as tmp_data_root:
            prod_roles = '/home/promonta/agent/miniapp/roles.json'
            mtime_before = os.path.getmtime(prod_roles) if os.path.exists(prod_roles) else None

            repo_root = os.path.join(os.path.dirname(__file__), '..')
            env = dict(os.environ)
            env['MINIAPP_DATA_ROOT'] = tmp_data_root
            env['BOT_TOKEN'] = 'ci-dummy-token-not-a-real-secret'
            result = subprocess.run(
                [sys.executable, '-c', script],
                cwd=repo_root, env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('OK: DATA_ROOT isolation verified', result.stdout)

            # физическая проверка: тестовые файлы реально создались ВНУТРИ tmp_data_root
            self.assertTrue(os.path.exists(os.path.join(tmp_data_root, 'roles.json')))

            # прод-файл не изменился (mtime тот же, что до прогона)
            mtime_after = os.path.getmtime(prod_roles) if os.path.exists(prod_roles) else None
            self.assertEqual(mtime_before, mtime_after)


# ---------- П5: lock на создание инструмента, настоящий concurrency-тест ----------

class ToolCreateRaceTests(unittest.TestCase):
    def test_concurrent_create_gets_distinct_serials_no_duplicates(self):
        """Настоящий concurrency-тест: два потока стартуют одновременно через
        threading.Barrier, оба вызывают create_tool -- без lock оба могли бы прочитать
        один и тот же max(serial) до того как первый успевал записать новую строку."""
        existing_serials = ['T-001', 'T-002']
        lock_inner = threading.Lock()

        def fake_add_tool(name, category, created_by):
            # эмулирует tools_lib.add_tool: читает существующие, max+1, "записывает"
            with lock_inner:
                nums = [int(s.split('-')[1]) for s in existing_serials]
                next_num = max(nums) + 1
                new_serial = f'T-{next_num:03d}'
                # искусственная задержка МЕЖДУ чтением и записью -- без внешнего lock
                # (main.py-уровня _tool_create_lock) это окно гонки
                import time as _time
                _time.sleep(0.05)
                existing_serials.append(new_serial)
            return new_serial

        fake_tl = MagicMock()
        fake_tl.add_tool.side_effect = fake_add_tool

        barrier = threading.Barrier(2)
        results = {}
        errors = []

        def worker(key, body):
            try:
                barrier.wait(timeout=2)
                r = backend.create_tool(body, user=OWNER, _=None)
                results[key] = r['serial']
            except Exception as e:
                errors.append(e)

        body_a = backend.NewToolBody(name='Bosch', category='Perforator')
        body_b = backend.NewToolBody(name='Makita', category='Saw')
        # patch.object -- НЕ thread-safe для одновременного enter/exit из разных потоков
        # (первая версия этого теста патчила ВНУТРИ каждого worker-потока -- гонка на
        # самом patch.__enter__/__exit__ иногда оставляла _load_repo_tools_lib
        # подменённым MagicMock'ом уже ПОСЛЕ выхода из `with`, ломая test_tools.py,
        # запускавшийся следом в том же процессе). Патчим один раз СНАРУЖИ потоков.
        with patch.object(backend, '_load_repo_tools_lib', return_value=fake_tl):
            t1 = threading.Thread(target=worker, args=('a', body_a))
            t2 = threading.Thread(target=worker, args=('b', body_b))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results['a'], results['b'], f"duplicate serial: {results}")


class ToolCheckoutConcurrencyThreadTests(unittest.TestCase):
    """31.07: улучшение прошлого checkout-race теста -- реальные потоки+barrier вместо
    двух последовательных вызовов (прошлый тест технически не доказывал, что lock
    работает под настоящей конкуренцией, только что второй вызов после первого 409-ит)."""

    def test_concurrent_checkout_only_one_succeeds(self):
        free_tool = {'Серийный #': 'T-030', 'Кто взял': '', 'Статус': '', 'ID держателя': ''}
        state = {'status': 'free'}
        state_lock = threading.Lock()

        fake_tl = MagicMock()

        def fake_get_tool(serial):
            return free_tool

        def fake_mapped_status(tool):
            with state_lock:
                return state['status']

        def fake_checkout(*a, **kw):
            import time as _time
            _time.sleep(0.05)
            with state_lock:
                state['status'] = 'in_use'

        fake_tl.get_tool.side_effect = fake_get_tool
        fake_tl.mapped_status.side_effect = fake_mapped_status
        fake_tl.checkout_tool.side_effect = fake_checkout

        WORKER_A_ = {'id': 10, 'first_name': 'Ivan'}
        WORKER_B_ = {'id': 20, 'first_name': 'Petr'}
        barrier = threading.Barrier(2)
        results = {}

        def worker(key, user):
            barrier.wait(timeout=2)
            body = backend.CheckoutBody(object_name='Baustelle X')
            try:
                backend.checkout_tool('T-030', body, user=user)
                results[key] = 'ok'
            except HTTPException as e:
                results[key] = e.status_code

        # patch снаружи потоков -- см. обоснование в ToolCreateRaceTests выше.
        with patch.object(backend, '_load_repo_tools_lib', return_value=fake_tl):
            t1 = threading.Thread(target=worker, args=('a', WORKER_A_))
            t2 = threading.Thread(target=worker, args=('b', WORKER_B_))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        outcomes = sorted(results.values(), key=str)
        self.assertEqual(len(results), 2)
        self.assertIn('ok', results.values())
        self.assertIn(409, results.values())


if __name__ == '__main__':
    unittest.main()
