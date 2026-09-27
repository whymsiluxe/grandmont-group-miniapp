"""Regression tests for the owner-reported Documents-500: object_info.json
document records missing 'file'/'id' keys used to raise KeyError (d["file"],
d["id"]) in the document list/file-serving/delete routes. These are called
per-object by frontend/js/document-gallery.js after it loads GET /api/objects
-- a single malformed document entry for one object must not 500 that
object's document list, and must not crash file lookup/delete for the
OTHER, valid documents on the same object.
"""
import os
import sys
import unittest
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402

USER = {'id': '1'}


class DocumentsResilienceTests(unittest.TestCase):
    def test_get_documents_skips_non_dict_entries(self):
        info = {'OBJ-1': {'documents': ['garbage', {'id': 'd1', 'file': 'a.pdf', 'name': 'A'}], 'items': [], 'description': ''}}
        with patch.object(backend, '_load_object_info', return_value=info):
            result = backend.get_object_documents('OBJ-1', user=USER, _=None)
        self.assertEqual(len(result['documents']), 1)
        self.assertEqual(result['documents'][0]['id'], 'd1')

    def test_get_documents_handles_malformed_entry_shape(self):
        """object_info.json['OBJ-1'] itself not a dict (corrupt legacy write)."""
        info = {'OBJ-1': ['not', 'a', 'dict']}
        with patch.object(backend, '_load_object_info', return_value=info):
            result = backend.get_object_documents('OBJ-1', user=USER, _=None)
        self.assertEqual(result['documents'], [])

    def test_get_document_file_skips_malformed_sibling_and_finds_valid_one(self):
        info = {'OBJ-1': {'documents': [
            {'id': 'd0', 'name': 'no file key'},  # malformed: missing 'file'
            {'id': 'd1', 'file': 'real.pdf', 'name': 'Real', 'content_type': 'application/pdf'},
        ]}}
        with patch.object(backend, '_load_object_info', return_value=info), \
             patch.object(backend.os.path, 'exists', return_value=True), \
             patch('main.FileResponse', return_value='FILE_RESPONSE_STUB'):
            result = backend.get_object_document_file('OBJ-1', 'real.pdf', user=USER, _=None)
        self.assertEqual(result, 'FILE_RESPONSE_STUB')

    def test_get_document_file_missing_returns_404_not_500(self):
        info = {'OBJ-1': {'documents': [{'id': 'd0', 'name': 'no file key'}]}}
        with patch.object(backend, '_load_object_info', return_value=info):
            with pytest.raises(HTTPException) as exc_info:
                backend.get_object_document_file('OBJ-1', 'missing.pdf', user=USER, _=None)
        self.assertEqual(exc_info.value.status_code, 404)

    def test_delete_document_skips_malformed_sibling(self):
        info = {'OBJ-1': {'documents': [
            'garbage',
            {'id': 'd1', 'file': 'real.pdf', 'name': 'Real'},
        ]}}

        def fake_transaction(path, default, mutator):
            return mutator(info)

        with patch.object(backend, 'update_json_transaction', side_effect=fake_transaction), \
             patch.object(backend.os.path, 'exists', return_value=False):
            result = backend.delete_object_document('OBJ-1', 'd1', user=USER, _=None)
        self.assertEqual(result, {'status': 'ok'})
        self.assertEqual(info['OBJ-1']['documents'], ['garbage'])


if __name__ == '__main__':
    unittest.main()
