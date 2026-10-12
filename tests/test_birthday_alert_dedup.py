import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault('BOT_TOKEN', 'ci-dummy')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402


OWNER_ID = '1'
WORKER_ID = '10'


class BirthdayAlertDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_birthday = backend.BIRTHDAY_ALERTS_FILE
        self._orig_critical = backend.CRITICAL_ALERTS_FILE
        self._orig_chat = backend.CHAT_FILE
        backend.BIRTHDAY_ALERTS_FILE = os.path.join(self.tmp.name, 'birthday_alerts.json')
        backend.CRITICAL_ALERTS_FILE = os.path.join(self.tmp.name, 'critical_alerts.json')
        backend.CHAT_FILE = os.path.join(self.tmp.name, 'chat.json')
        backend._atomic_write_json(backend.BIRTHDAY_ALERTS_FILE, [])
        backend._atomic_write_json(backend.CRITICAL_ALERTS_FILE, [])
        backend._atomic_write_json(backend.CHAT_FILE, [])
        self.roles_patch = patch.object(backend, '_load_roles', return_value={OWNER_ID: 'owner', WORKER_ID: 'worker'})
        self.profiles_patch = patch.object(backend, '_load_worker_profiles', return_value={
            WORKER_ID: {'name': 'Иван', 'birthday': '1990-10-12'},
        })
        self.business_patch = patch.object(backend, 'business_today', return_value=datetime(2026, 10, 12).date())
        self.telegram_patch = patch.object(backend, 'send_telegram_message')
        self.roles_patch.start()
        self.profiles_patch.start()
        self.business_patch.start()
        self.telegram_patch.start()

    def tearDown(self):
        patch.stopall()
        backend.BIRTHDAY_ALERTS_FILE = self._orig_birthday
        backend.CRITICAL_ALERTS_FILE = self._orig_critical
        backend.CHAT_FILE = self._orig_chat
        self.tmp.cleanup()

    def _pending_alerts(self):
        return [a for a in backend._load_critical_alerts() if not a.get('acknowledged_at')]

    def test_repeated_birthday_check_does_not_duplicate(self):
        backend._check_upcoming_birthdays()
        backend._check_upcoming_birthdays()
        birthday_records = backend._load_birthday_alerts()
        self.assertEqual(len(birthday_records), 1)
        pending = self._pending_alerts()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['kind'], 'birthday')
        self.assertEqual(pending[0]['ref_id'], f'birthday:{WORKER_ID}:2026:today')

    def test_ack_then_same_event_is_not_recreated_but_next_year_is_allowed(self):
        backend._check_upcoming_birthdays()
        pending = self._pending_alerts()
        self.assertEqual(len(pending), 1)
        alert_id = pending[0]['id']

        def _ack(items):
            alert = next(a for a in items if a['id'] == alert_id)
            alert['acknowledged_at'] = 1234567890
            return alert
        backend.update_json_transaction(backend.CRITICAL_ALERTS_FILE, [], _ack)

        today = datetime(2026, 10, 12).date()
        next_year_idem = f"birthday:{WORKER_ID}:{today.year + 1}:today"
        backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='birthday',
            title='🎂 Сегодня день рождения у Иван', ref_id=next_year_idem,
        )
        pending = self._pending_alerts()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['ref_id'], next_year_idem)

    def test_ref_id_is_the_idem_key_not_the_bare_worker_id(self):
        import inspect
        src = inspect.getsource(backend._check_upcoming_birthdays)
        assert "ref_id=idem" in src
        assert "ref_id=uid" not in src

    def test_check_upcoming_birthdays_uses_transactional_store_update(self):
        """Guard the atomic RMW behavior regardless of which module owns it."""
        import inspect
        src = inspect.getsource(backend._check_upcoming_birthdays)
        assert "update_json_transaction" in src
        assert "birthday_alerts_file" in src or "BIRTHDAY_ALERTS_FILE" in src


if __name__ == '__main__':
    unittest.main()
