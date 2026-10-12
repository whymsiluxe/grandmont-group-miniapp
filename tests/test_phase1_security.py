import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402
import routes.commercial_docs as commercial_docs  # noqa: E402


class AngebotAccessTests(unittest.TestCase):
    def test_worker_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.require_angebot_access(role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_allowed(self):
        self.assertIsNone(backend.require_angebot_access(role='owner'))

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
        self.assertEqual(
            backend.CREATE_OBJECT_SCRIPT,
            os.path.join(backend.BACKEND_DIR, 'create_object.py'),
        )
        self.assertEqual(
            backend.CREATE_OBJECT_FOLDER_SCRIPT,
            os.path.join(backend.BACKEND_DIR, 'create_object_folder.py'),
        )


if __name__ == '__main__':
    unittest.main()
