#!/usr/bin/env python3
from pathlib import Path

path = Path("backend/main.py")
text = path.read_text(encoding="utf-8")
start_marker = "# ---------- Инфо объекта (24.07, Step 3): work-items + документы ----------\n"
end_marker = "# moved to core/constants.py -- VALID_OBJECT_STATUSES\n"
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise SystemExit(f"document block markers not found: start={start} end={end}")
end += len(end_marker)
replacement = '''# ---------- Shared object info store + Documents domain ----------
if __package__:
    from .core.object_info_store import ObjectInfoStore
    from .routes.documents import DocumentsRouteDeps, create_documents_router
else:
    from core.object_info_store import ObjectInfoStore  # noqa: E402
    from routes.documents import DocumentsRouteDeps, create_documents_router  # noqa: E402

# Preserve the legacy import-time directory creation behavior.
os.makedirs(OBJECT_DOC_DIR, exist_ok=True)

_object_info_store = ObjectInfoStore(
    file_path=lambda: OBJECT_INFO_FILE,
    safe_load_json=lambda path, default: _safe_load_json(path, default),
    atomic_write_json=lambda path, value: _atomic_write_json(path, value),
)

# Temporary helper aliases for existing direct-call tests/internal callers. No
# object-info implementation remains in main.py.
_load_object_info = _object_info_store.load
_save_object_info = _object_info_store.save
_new_object_info_entry = _object_info_store.new_entry
_ensure_object_info_entry = _object_info_store.ensure_entry
_object_info_entry = _object_info_store.entry

_documents_router, _documents_handlers = create_documents_router(
    DocumentsRouteDeps(
        get_current_user=get_current_user,
        require_owner=require_owner,
        require_object_access=require_object_access,
        store=_object_info_store,
        object_doc_dir=lambda: OBJECT_DOC_DIR,
        sniff_image_or_pdf=lambda data: sniff_image_or_pdf(data),
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
        update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
        append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(*args, **kwargs),
    )
)
app.include_router(_documents_router)

get_object_documents = _documents_handlers.get_object_documents
upload_object_document = _documents_handlers.upload_object_document
delete_object_document = _documents_handlers.delete_object_document
get_object_document_file = _documents_handlers.get_object_document_file

# moved to core/constants.py -- VALID_OBJECT_STATUSES
'''
text = text[:start] + replacement + text[end:]
old_wiring = '''    object_info_entry=lambda object_id: _object_info_entry(object_id),
    ensure_object_info_entry=lambda data, object_id: _ensure_object_info_entry(data, object_id),
'''
new_wiring = '''    object_info_entry=_object_info_store.entry,
    ensure_object_info_entry=_object_info_store.ensure_entry,
'''
if text.count(old_wiring) != 1:
    raise SystemExit(f"expected exactly one Objects object-info wiring block, found {text.count(old_wiring)}")
text = text.replace(old_wiring, new_wiring)
path.write_text(text, encoding="utf-8")
print("phase5 documents transform applied")
