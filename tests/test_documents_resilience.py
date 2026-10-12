"""Regression tests for the owner-reported Documents-500.

Malformed document records must never break valid siblings.  These tests use the
real temporary JSON store and filesystem path instead of monkey-patching helpers
that used to live in ``main.py``; that keeps the regression contract independent
of the implementation module while exercising more of the production path.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402

USER = {'id': '1'}


class DocumentsResilienceTests(unittest.TestCase):
    def _with_store(self, info, callback, files=None):
        with tempfile.TemporaryDirectory() as td:
            info_path = os.path.join(td, 'object_info.json')
            doc_dir = os.path.join(td, 'documents')
            os.makedirs(doc_dir, exist_ok=True)
            with patch.object(backend, 'OBJECT_INFO_FILE', info_path), \
                 patch.object(backend, 'OBJECT_DOC_DIR', doc_dir):
                backend._save_object_info(info)
                for name, data in (files or {}).items():
                    with open(os.path.join(doc_dir, name), 'wb') as handle:
                        handle.write(data)
                return callback(doc_dir)

    def test_get_documents_skips_non_dict_entries(self):
        info = {
            'OBJ-1': {
                'documents': ['garbage', {'id': 'd1', 'file': 'a.pdf', 'name': 'A'}],
                'items': [],
                'description': '',
            }
        }

        result = self._with_store(
            info,
            lambda _: backend.get_object_documents('OBJ-1', user=USER, _=None),
        )
        self.assertEqual(len(result['documents']), 1)
        self.assertEqual(result['documents'][0]['id'], 'd1')

    def test_get_documents_handles_malformed_entry_shape(self):
        """object_info.json['OBJ-1'] itself may be a corrupt legacy non-dict."""
        info = {'OBJ-1': ['not', 'a', 'dict']}
        result = self._with_store(
            info,
            lambda _: backend.get_object_documents('OBJ-1', user=USER, _=None),
        )
        self.assertEqual(result['documents'], [])

    def test_get_document_file_skips_malformed_sibling_and_finds_valid_one(self):
        info = {
            'OBJ-1': {
                'documents': [
                    {'id': 'd0', 'name': 'no file key'},
                    {
                        'id': 'd1',
                        'file': 'real.pdf',
                        'name': 'Real',
                        'content_type': 'application/pdf',
                    },
                ]
            }
        }

        def _read(doc_dir):
            result = backend.get_object_document_file('OBJ-1', 'real.pdf', user=USER, _=None)
            self.assertIsInstance(result, FileResponse)
            self.assertEqual(result.path, os.path.join(doc_dir, 'real.pdf'))
            self.assertEqual(result.media_type, 'application/pdf')
            return result

        self._with_store(info, _read, files={'real.pdf': b'%PDF-test'})

    def test_get_document_file_missing_returns_404_not_500(self):
        info = {'OBJ-1': {'documents': [{'id': 'd0', 'name': 'no file key'}]}}

        def _read(_):
            with pytest.raises(HTTPException) as exc_info:
                backend.get_object_document_file('OBJ-1', 'missing.pdf', user=USER, _=None)
            self.assertEqual(exc_info.value.status_code, 404)

        self._with_store(info, _read)

    def test_delete_document_skips_malformed_sibling(self):
        info = {
            'OBJ-1': {
                'documents': [
                    'garbage',
                    {'id': 'd1', 'file': 'real.pdf', 'name': 'Real'},
                ]
            }
        }

        def _delete(doc_dir):
            real_path = os.path.join(doc_dir, 'real.pdf')
            self.assertTrue(os.path.exists(real_path))
            result = backend.delete_object_document('OBJ-1', 'd1', user=USER, _=None)
            self.assertEqual(result, {'status': 'ok'})
            self.assertFalse(os.path.exists(real_path))
            persisted = backend._load_object_info()
            self.assertEqual(persisted['OBJ-1']['documents'], ['garbage'])

        self._with_store(info, _delete, files={'real.pdf': b'%PDF-test'})


if __name__ == '__main__':
    unittest.main()
