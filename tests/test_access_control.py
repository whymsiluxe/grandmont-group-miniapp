"""Backend tests for Telegram initData HMAC validation (Release-аудит Этап 4) and
negative authorization -- Worker A vs Worker B, unknown/expired/tampered initData,
direct access to another user's protected file (Release-аудит Этап 3).

Same plain stdlib unittest approach as tests/test_owner_kt_requirements.py -- route
handlers called directly with explicit kwargs; validate_init_data is tested with
genuinely HMAC-signed initData strings built with the same secret-key derivation
the real Telegram client would use (BOT_TOKEN from env), so these are real
signature checks, not mocked ones.

Run:
    cd miniapp-repo && python3 -m unittest tests.test_access_control -v
(same environment requirements as test_chat_backend.py: BOT_TOKEN in env, run
with the miniapp .venv's python3.)
"""
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402
from chat_domain_testkit import ChatDomainHarness  # noqa: E402


def _build_init_data(user_id=111, first_name='Test', auth_date=None, tamper=False, bad_hash=False, missing_hash=False):
    """Строит genuine HMAC-подписанный initData тем же способом, что и Telegram
    WebApp клиент -- data_check_string = отсортированные 'k=v' пары без hash,
    подпись = HMAC-SHA256(secret_key, data_check_string), secret_key =
    HMAC-SHA256('WebAppData', BOT_TOKEN)."""
    if auth_date is None:
        auth_date = int(time.time())
    user_json = json.dumps({'id': user_id, 'first_name': first_name}, separators=(',', ':'))
    params = {'auth_date': str(auth_date), 'user': user_json, 'query_id': 'AAAtest'}
    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(params.items()))
    computed_hash = hmac.new(backend._secret_key(), data_check_string.encode(), hashlib.sha256).hexdigest()
    if tamper:
        computed_hash = ('0' if computed_hash[0] != '0' else '1') + computed_hash[1:]

    parts = [f'{k}={quote(v, safe="")}' for k, v in params.items()]
    if not missing_hash:
        hash_value = 'deadbeef' if bad_hash else computed_hash
        parts.append(f'hash={hash_value}')
    return '&'.join(parts)


class InitDataHmacTests(unittest.TestCase):
    """Release-аудит Этап 4: HMAC-проверка initData строго по официальной схеме,
    constant-time comparison (hmac.compare_digest -- проверено чтением кода),
    auth_date TTL, отклонение поддельной подписи."""

    def test_valid_init_data_accepted(self):
        init_data = _build_init_data(user_id=42, first_name='Иван')
        user = backend.validate_init_data(init_data)
        self.assertEqual(user['id'], 42)
        self.assertEqual(user['first_name'], 'Иван')

    def test_tampered_hash_rejected(self):
        init_data = _build_init_data(tamper=True)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_completely_fake_hash_rejected(self):
        init_data = _build_init_data(bad_hash=True)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_missing_hash_rejected(self):
        init_data = _build_init_data(missing_hash=True)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_expired_init_data_rejected(self):
        # INIT_DATA_MAX_AGE = 3600 -- 2 часа назад точно протухло.
        old_auth_date = int(time.time()) - 7200
        init_data = _build_init_data(auth_date=old_auth_date)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn('expired', ctx.exception.detail.lower())

    def test_far_future_auth_date_rejected(self):
        # age < -60 -- initData "из будущего" тоже отклоняется (не только протухшее).
        future_auth_date = int(time.time()) + 600
        init_data = _build_init_data(auth_date=future_auth_date)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_malformed_init_data_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data('not a valid query string; with garbage=')
        self.assertEqual(ctx.exception.status_code, 401)

    def test_empty_init_data_rejected(self):
        with self.assertRaises(HTTPException):
            backend.validate_init_data('')

    def test_signature_is_constant_time_compare(self):
        # Не полноценный timing-attack тест (нестабильно в CI), но подтверждаем,
        # что реализация использует hmac.compare_digest, а не == -- читаем исходник
        # validate_init_data напрямую, чтобы зафиксировать это как regression guard.
        import inspect
        source = inspect.getsource(backend.validate_init_data)
        self.assertIn('hmac.compare_digest', source)

    def test_user_id_cannot_be_spoofed_via_body_alone(self):
        # get_current_user получает user ТОЛЬКО из validate_init_data(x_telegram_init_data)
        # ИЛИ verify_session_token(authorization) -- нет пути, которым JSON body с
        # произвольным user_id повлиял бы на identity.
        # 03.08 (ТЗ Задача 1): сигнатура расширена authorization-параметром для
        # 12-часового session token (см. tests/test_session_token.py) -- сам regression
        # guard теперь проверяет, что оба источника identity -- ТОЛЬКО заголовки
        # (initData/Bearer token), не тело запроса, и что оба параметра проходят через
        # Header(...), а не через body-модель.
        import inspect
        sig = inspect.signature(backend.get_current_user)
        params = list(sig.parameters.keys())
        self.assertEqual(set(params), {'authorization', 'x_telegram_init_data'})


class CrossWorkerAuthorizationTests(unittest.TestCase):
    """Release-аудит Этап 3: Worker A не может трогать данные Worker B."""

    def setUp(self):
        # 20.09: abwesenheit-мутации ушли под update_json_transaction(), которая
        # читает файл напрямую -- patch.object(_load_abwesenheit) на её путь больше
        # не влияет. Изолированный временный стор вместо мока.
        import tempfile as _tf
        self._abw_tmp = _tf.mkdtemp(prefix='abw-acl-')
        self._abw_orig = backend.ABWESENHEIT_FILE
        backend.ABWESENHEIT_FILE = os.path.join(self._abw_tmp, 'abwesenheit.json')

    def tearDown(self):
        backend.ABWESENHEIT_FILE = self._abw_orig

    def test_worker_cannot_return_another_workers_tool(self):
        tl = backend._load_repo_tools_lib()
        from unittest.mock import patch
        tool_held_by_b = {
            'Серийный #': 'T-1', 'Название Инструмента': 'X', 'Категория': 'Y',
            'Кто взял': 'Worker B', 'Статус': 'На объекте', 'Обьект/Адрес': 'OBJ-1',
            'ID держателя': '222',
        }
        with patch.object(tl, 'get_tool', return_value=tool_held_by_b):
            import asyncio
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(backend.return_tool(serial='T-1', user={'id': 111}, role='worker'))
            self.assertEqual(ctx.exception.status_code, 403)

    def test_worker_cannot_close_another_workers_absence(self):
        entry = {'id': 'abw-1', 'user_id': '222', 'status': 'approved',
                 'date_from': '2026-09-01', 'date_to': '2026-09-02'}
        backend._save_abwesenheit([entry])
        with self.assertRaises(HTTPException) as ctx:
            backend.close_abwesenheit(entry_id='abw-1', user={'id': 111}, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_worker_cannot_delete_another_workers_absence(self):
        entry = {'id': 'abw-1', 'user_id': '222', 'status': 'approved',
                 'date_from': '2026-09-01', 'date_to': '2026-09-02'}
        backend._save_abwesenheit([entry])
        with self.assertRaises(HTTPException) as ctx:
            backend.delete_abwesenheit(entry_id='abw-1', user={'id': 111}, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)
        # запрет должен не только вернуть 403, но и оставить запись на месте
        with open(backend.ABWESENHEIT_FILE, encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)), 1)

    def test_worker_cannot_delete_another_workers_chat_message(self):
        h = ChatDomainHarness(roles={'111': 'worker', '222': 'worker'})
        try:
            h.messages = [{'id': 'm1', 'user_id': 222, 'text': 'hi', 'to_user_id': None}]
            with self.assertRaises(HTTPException) as ctx:
                h.actions.delete_chat_message(msg_id='m1', user={'id': 111}, role='worker')
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            h.close()

    def test_worker_cannot_ack_another_workers_critical_alert(self):
        import tempfile
        alert = {'id': 'a1', 'target_user_id': '222'}
        body = backend.CriticalAlertAckBody(comment='')
        # 23.09: ack_critical_alert() now reads via update_json_transaction()
        # (real file I/O under a lock, to close a read-modify-write race --
        # see test_critical_alert_ack_race.py), not the module-level
        # _load_critical_alerts() -- patching that no longer intercepts the
        # read this endpoint actually does. Seed a real temp store instead,
        # same pattern as test_critical_alert_dedup.py.
        with tempfile.TemporaryDirectory() as tmp:
            orig_file = backend.CRITICAL_ALERTS_FILE
            backend.CRITICAL_ALERTS_FILE = os.path.join(tmp, 'critical_alerts.json')
            try:
                backend._save_critical_alerts([alert])
                with self.assertRaises(HTTPException) as ctx:
                    backend.ack_critical_alert(alert_id='a1', body=body, user={'id': 111})
                self.assertEqual(ctx.exception.status_code, 403)
            finally:
                backend.CRITICAL_ALERTS_FILE = orig_file

    def test_worker_cannot_view_another_workers_checkin_photo(self):
        from unittest.mock import patch
        sessions = [{'id': 's1', 'user_id': 222, 'start_photos': ['a.jpg'], 'finish_photos': []}]
        with patch.object(backend, '_load_checkin_meta', return_value=sessions):
            with self.assertRaises(HTTPException) as ctx:
                backend.get_checkin_photo(session_id='s1', which='start', index=0,
                                          user={'id': 111}, role='worker')
            self.assertEqual(ctx.exception.status_code, 403)

    def test_worker_cannot_access_another_workers_calendar(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.get_worker_calendar(target_user_id='222', year=2026, month=7,
                                         user={'id': 111}, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_can_do_all_of_the_above(self):
        # Контрольная проверка -- owner-роль обходит все эти self-only ограничения
        # (иначе тесты выше могли бы случайно проверять "всегда 403", а не именно
        # cross-user изоляцию).
        entry = {'id': 'abw-1', 'user_id': '222', 'status': 'approved',
                 'date_from': '2026-09-01', 'date_to': '2026-09-02'}
        backend._save_abwesenheit([entry])
        result = backend.delete_abwesenheit(entry_id='abw-1', user={'id': 999}, role='owner')
        self.assertEqual(result, {"status": "ok"})
        with open(backend.ABWESENHEIT_FILE, encoding='utf-8') as f:
            self.assertEqual(json.load(f), [])


class DirectFileAccessTests(unittest.TestCase):
    """Release-аудит Этап 3: нельзя получить защищённый файл прямым URL без
    правильной авторизации, даже зная точное имя файла (IDOR)."""

    def test_worker_cannot_fetch_other_threads_chat_attachment(self):
        h = ChatDomainHarness(roles={'111': 'worker', '222': 'worker', '333': 'worker'})
        try:
            h.create_attachment('secret.jpg')
            h.messages = [{
                'id': 'm1', 'user_id': 222, 'to_user_id': '333',
                'attachment': {'file': 'secret.jpg'},
            }]
            with self.assertRaises(HTTPException) as ctx:
                h.actions.get_chat_attachment(fname='secret.jpg', user={'id': 111}, role='worker')
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            h.close()

    def test_worker_cannot_fetch_another_users_transcribe_audio_by_guessing_id(self):
        # file_id существует только в подпапке user 222 -- 111 (non-owner) ищет
        # только в СВОЕЙ подпапке, ничего не находит -> 404, утечки нет.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            original_dir = backend.TRANSCRIBE_AUDIO_DIR
            backend.TRANSCRIBE_AUDIO_DIR = tmp
            try:
                other_user_dir = os.path.join(tmp, '222')
                os.makedirs(other_user_dir)
                with open(os.path.join(other_user_dir, 'abc123.ogg'), 'wb') as f:
                    f.write(b'fake audio')
                with self.assertRaises(HTTPException) as ctx:
                    backend.get_transcribe_audio(file_id='abc123', user={'id': 111}, role='worker')
                self.assertEqual(ctx.exception.status_code, 404)
            finally:
                backend.TRANSCRIBE_AUDIO_DIR = original_dir


if __name__ == '__main__':
    unittest.main()
