"""Phase 29 regressions for object-document media ownership."""
from __future__ import annotations

import json
import os
import sys
from copy import deepcopy

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from core.object_info_store import ObjectInfoStore  # noqa: E402
from routes.documents import DocumentsRouteDeps, create_documents_router  # noqa: E402


def _build_handlers(tmp_path, initial):
    doc_root = tmp_path / 'documents'
    doc_root.mkdir()
    info_file = tmp_path / 'object_info.json'
    state = deepcopy(initial)
    info_file.write_text(json.dumps(state), encoding='utf-8')

    def safe_load(path, default):
        if path == str(info_file):
            return deepcopy(state)
        return deepcopy(default)

    def atomic_write(_path, data):
        nonlocal state
        state = deepcopy(data)
        info_file.write_text(json.dumps(state), encoding='utf-8')

    def update_json_transaction(_path, default, mutator):
        nonlocal state
        working = deepcopy(state if state is not None else default)
        result = mutator(working)
        state = working
        info_file.write_text(json.dumps(state), encoding='utf-8')
        return result

    store = ObjectInfoStore(
        file_path=lambda: str(info_file),
        safe_load_json=safe_load,
        atomic_write_json=atomic_write,
    )
    deps = DocumentsRouteDeps(
        get_current_user=lambda: {'id': '42'},
        require_owner=lambda: None,
        require_object_access=lambda: None,
        store=store,
        object_doc_dir=lambda: str(doc_root),
        sniff_image_or_pdf=lambda _raw: 'application/pdf',
        allowed_image_mime_ext={'image/jpeg': 'jpg'},
        update_json_transaction=update_json_transaction,
        append_object_history_best_effort=lambda *_args, **_kwargs: None,
    )
    _router, handlers = create_documents_router(deps)
    return handlers, doc_root, lambda: deepcopy(state)


def _doc_entry(fname='doc.pdf', doc_id='doc-1'):
    return {
        'id': doc_id,
        'file': fname,
        'name': 'Document.pdf',
        'content_type': 'application/pdf',
        'uploaded_by': 'Worker',
        'uploaded_at': 1,
    }


def test_registered_document_file_remains_accessible(tmp_path):
    handlers, doc_root, _state = _build_handlers(
        tmp_path,
        {'OBJ-A': {'items': [], 'description': '', 'documents': [_doc_entry()]}},
    )
    document = doc_root / 'doc.pdf'
    document.write_bytes(b'pdf')

    response = handlers.get_object_document_file(
        'OBJ-A', 'doc.pdf', user={'id': '42'}, _=None
    )

    assert isinstance(response, FileResponse)
    assert response.path == str(document)


def test_registered_document_symlink_alias_is_rejected(tmp_path):
    handlers, doc_root, _state = _build_handlers(
        tmp_path,
        {'OBJ-A': {'items': [], 'description': '', 'documents': [_doc_entry()]}},
    )
    target = doc_root / 'other.pdf'
    target.write_bytes(b'other-document')
    alias = doc_root / 'doc.pdf'
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    with pytest.raises(HTTPException) as exc:
        handlers.get_object_document_file(
            'OBJ-A', 'doc.pdf', user={'id': '42'}, _=None
        )

    assert exc.value.status_code == 404
    assert target.read_bytes() == b'other-document'


def test_document_delete_does_not_follow_symlink_target(tmp_path):
    handlers, doc_root, state = _build_handlers(
        tmp_path,
        {'OBJ-A': {'items': [], 'description': '', 'documents': [_doc_entry()]}},
    )
    target = doc_root / 'other.pdf'
    target.write_bytes(b'keep-me')
    alias = doc_root / 'doc.pdf'
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    result = handlers.delete_object_document(
        'OBJ-A', 'doc-1', user={'id': '1'}, _=None
    )

    assert result == {'status': 'ok'}
    assert state()['OBJ-A']['documents'] == []
    assert target.read_bytes() == b'keep-me'


def test_document_metadata_path_traversal_is_rejected(tmp_path):
    handlers, _doc_root, _state = _build_handlers(
        tmp_path,
        {
            'OBJ-A': {
                'items': [],
                'description': '',
                'documents': [_doc_entry('../secret.pdf')],
            }
        },
    )
    secret = tmp_path / 'secret.pdf'
    secret.write_bytes(b'secret')

    with pytest.raises(HTTPException) as exc:
        handlers.get_object_document_file(
            'OBJ-A', '../secret.pdf', user={'id': '42'}, _=None
        )

    assert exc.value.status_code == 404
    assert secret.read_bytes() == b'secret'
