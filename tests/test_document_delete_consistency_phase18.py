"""Phase 18 regression for post-commit document binary cleanup failures."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
import routes.documents as documents_route  # noqa: E402


class DocumentDeleteConsistencyPhase18Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='document-delete-phase18-')
        self._orig_info_file = backend.OBJECT_INFO_FILE
        self._orig_doc_dir = backend.OBJECT_DOC_DIR
        backend.OBJECT_INFO_FILE = os.path.join(self.tmp, 'object_info.json')
        backend.OBJECT_DOC_DIR = os.path.join(self.tmp, 'docs')
        os.makedirs(backend.OBJECT_DOC_DIR, exist_ok=True)

    def tearDown(self):
        backend.OBJECT_INFO_FILE = self._orig_info_file
        backend.OBJECT_DOC_DIR = self._orig_doc_dir

    def test_binary_remove_failure_does_not_false_500_after_metadata_delete(self):
        object_id = 'obj-1'
        filename = 'owned.pdf'
        document = {
            'id': 'doc-1',
            'file': filename,
            'name': 'owned.pdf',
            'content_type': 'application/pdf',
            'uploaded_by': 'Owner',
            'uploaded_at': 1,
        }
        backend._save_object_info({
            object_id: {
                'description': '',
                'items': [],
                'documents': [document],
            }
        })
        file_path = os.path.join(backend.OBJECT_DOC_DIR, filename)
        with open(file_path, 'wb') as handle:
            handle.write(b'%PDF-1.4\n')

        real_exists = os.path.exists
        with patch.object(documents_route.os, 'remove', side_effect=OSError('disk busy')):
            result = backend.delete_object_document(
                object_id,
                'doc-1',
                user={'id': 1},
                _=None,
            )

        self.assertEqual(result, {'status': 'ok'})
        self.assertTrue(real_exists(file_path), 'failed cleanup leaves only an orphan binary')
        stored = backend._load_object_info()
        self.assertEqual(stored[object_id]['documents'], [])


if __name__ == '__main__':
    unittest.main()
