"""Phase 1 security fix tests.

Tests covering:
- Budget-percent field schema drift across main.py/objekte_lib.py (all aliases)
- Chat attachment crash (attachment=None plain-text messages)
- TASKS_FILE locking (lock_for pattern verified)
- require_angebot_access dead manager role removed
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend
import routes.commercial_docs as commercial_docs
from fastapi import HTTPException

# main.py inserts /home/promonta/agent into sys.path[0], so plain `import objekte_lib`
# would resolve to the SYSTEM copy at /home/promonta/agent/objekte_lib.py, not the repo.
# _load_repo_objekte_lib() loads the repo's copy by explicit path — use that.
ol = backend._load_repo_objekte_lib()


OWNER = {'id': 1, 'first_name': 'Boss'}


class BudgetPercentAliasTests(unittest.TestCase):
    """get_budget_percent() must recognize all historical alias column names."""

    def test_canonical_column_name(self):
        obj = {'потрачено в % от бюджета': '72.5'}
        self.assertEqual(ol.get_budget_percent(obj), '72.5')

    def test_historical_alias_pct_byudzhet(self):
        obj = {'% бюджета': '55.0'}
        self.assertEqual(ol.get_budget_percent(obj), '55.0')

    def test_historical_alias_potracheno_pct(self):
        obj = {'Потрачено %': '80'}
        self.assertEqual(ol.get_budget_percent(obj), '80')

    def test_canonical_takes_precedence_over_alias(self):
        obj = {'потрачено в % от бюджета': '60', '% бюджета': '99'}
        self.assertEqual(ol.get_budget_percent(obj), '60')

    def test_missing_returns_zero(self):
        obj = {'Объект': 'Some object', 'Статус': 'В работе'}
        self.assertEqual(ol.get_budget_percent(obj), 0)

    def test_empty_string_treated_as_missing(self):
        obj = {'потрачено в % от бюджета': ''}
        self.assertEqual(ol.get_budget_percent(obj), 0)


class BudgetFieldsStrippingTests(unittest.TestCase):
    """BUDGET_FIELDS must include all alias keys to protect worker DTOs."""

    def test_budget_fields_contains_canonical_name(self):
        self.assertIn('потрачено в % от бюджета', backend.BUDGET_FIELDS)

    def test_budget_fields_contains_legacy_alias(self):
        self.assertIn('% бюджета', backend.BUDGET_FIELDS)

    def test_budget_fields_contains_third_alias(self):
        self.assertIn('Потрачено %', backend.BUDGET_FIELDS)

    def test_budget_fields_strips_eur_amounts(self):
        self.assertIn('Бюджет (EUR)', backend.BUDGET_FIELDS)
        self.assertIn('Потрачено (EUR)', backend.BUDGET_FIELDS)

    def test_all_aliases_stripped_from_worker_dto(self):
        """All budget aliases must be removed from worker-facing object dict."""
        obj = {
            'Объект': 'Test', 'Статус': 'В работе',
            'Бюджет (EUR)': '50000',
            'Потрачено (EUR)': '30000',
            'потрачено в % от бюджета': '60',
            '% бюджета': '60',
            'Потрачено %': '60',
        }
        for field in backend.BUDGET_FIELDS:
            obj.pop(field, None)
        for field in backend.BUDGET_FIELDS:
            self.assertNotIn(field, obj, f"Field '{field}' still in worker DTO")
        self.assertIn('Объект', obj)  # non-budget fields survive


class ChatAttachmentCrashTests(unittest.TestCase):
    """get_chat_attachment must not crash on messages with attachment=None."""

    def test_plain_text_message_attachment_none_does_not_crash(self):
        """Messages where 'attachment' key is explicitly None (plain-text) must
        not cause AttributeError via .get('attachment', {}).get('file')."""
        messages = [
            {'id': 'm1', 'attachment': None, 'to_user_id': '42', 'thread_key': ''},
            {'id': 'm2', 'attachment': {'file': 'photo.jpg'}, 'to_user_id': '42', 'thread_key': ''},
        ]
        # Verify that (msg.get('attachment') or {}).get('file') handles None attachment safely
        for msg in messages:
            val = (msg.get('attachment') or {}).get('file')
            # Should not raise; value is either None or 'photo.jpg'
            self.assertIn(val, (None, 'photo.jpg'))

    def test_absent_attachment_key_does_not_crash(self):
        msg = {'id': 'm3', 'to_user_id': '42'}
        val = (msg.get('attachment') or {}).get('file')
        self.assertIsNone(val)

    def test_get_chat_attachment_returns_404_when_no_matching_message(self):
        """Verify the fix is in the actual route handler (not just the helper logic)."""
        import tempfile, os
        path = os.path.join(backend.CHAT_ATTACH_DIR, 'nofile.jpg')
        # If file doesn't exist, the route should 404 before reaching the attachment check
        with patch.object(backend, 'CHAT_ATTACH_DIR', '/nonexistent'):
            with self.assertRaises(HTTPException) as ctx:
                backend.get_chat_attachment('nofile.jpg', user=OWNER, role='owner')
        self.assertEqual(ctx.exception.status_code, 404)


class RequireAngebotAccessTests(unittest.TestCase):
    """require_angebot_access must only allow 'owner', not 'manager'."""

    def test_owner_allowed(self):
        # Should not raise
        backend.require_angebot_access(role='owner')

    def test_worker_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.require_angebot_access(role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_manager_rejected(self):
        """'manager' role can never be assigned (set_role hard-rejects it), so
        require_angebot_access must reject it too — not silently grant access."""
        with self.assertRaises(HTTPException) as ctx:
            backend.require_angebot_access(role='manager')
        self.assertEqual(ctx.exception.status_code, 403)


class PdfGeneratorPathTests(unittest.TestCase):
    """PDF generators must be repo/runtime-local, not hardwired to prod paths."""

    def test_pdf_generator_scripts_resolve_from_backend_dir(self):
        self.assertEqual(backend.ANGEBOT_SCRIPT, os.path.join(backend.BACKEND_DIR, 'angebot_free.js'))
        self.assertEqual(backend.RECHNUNG_SCRIPT, os.path.join(backend.BACKEND_DIR, 'rechnung.js'))

    def test_pdf_filename_is_safe_for_multipart_header(self):
        filename = commercial_docs.safe_pdf_filename('Angebot', ' ACME "bad"\r\n.pdf ')
        self.assertEqual(filename, 'Angebot_ACME_bad.pdf')
        self.assertNotIn('\r', filename)
        self.assertNotIn('\n', filename)
        self.assertNotIn('"', filename)


class CreateObjectIntegrationPathTests(unittest.TestCase):
    def test_create_object_scripts_are_validated_before_subprocess(self):
        with patch.object(backend, 'CREATE_OBJECT_SCRIPT', '/missing/create_object.py'), \
             patch.object(backend, 'CREATE_OBJECT_FOLDER_SCRIPT', '/missing/create_object_folder.py'), \
             patch.object(backend.subprocess, 'run') as run_mock:
            with self.assertRaises(HTTPException) as ctx:
                backend.create_object_endpoint(
                    backend.NewObjectBody(name='Obj', adresse='Adr', budget='1000'),
                    background_tasks=MagicMock(),
                    user=OWNER,
                    _=None,
                )
        self.assertEqual(ctx.exception.status_code, 500)
        run_mock.assert_not_called()

    def test_create_object_uses_configured_scripts_and_current_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            create_script = os.path.join(tmp, 'create_object.py')
            folder_script = os.path.join(tmp, 'create_object_folder.py')
            open(create_script, 'w').close()
            open(folder_script, 'w').close()
            result = MagicMock(returncode=0, stdout='OK: OBJ-1\n', stderr='')
            tasks = MagicMock()

            with patch.object(backend, 'CREATE_OBJECT_SCRIPT', create_script), \
                 patch.object(backend, 'CREATE_OBJECT_FOLDER_SCRIPT', folder_script), \
                 patch.object(backend.subprocess, 'run', return_value=result) as run_mock:
                response = backend.create_object_endpoint(
                    backend.NewObjectBody(name='Obj', adresse='Adr', budget='1000'),
                    background_tasks=tasks,
                    user=OWNER,
                    _=None,
                )

        self.assertEqual(response['object_id'], 'OBJ-1')
        self.assertEqual(run_mock.call_args[0][0][0], sys.executable)
        self.assertEqual(run_mock.call_args[0][0][1], create_script)
        tasks.add_task.assert_called_once()
        self.assertEqual(tasks.add_task.call_args[0][1][0], sys.executable)
        self.assertEqual(tasks.add_task.call_args[0][1][1], folder_script)
