#!/usr/bin/env python3
"""One-shot bounded transform for Phase 2 Mangel extraction.

The script replaces only the legacy Mangel block in backend/main.py with
composition-root wiring, then deletes itself and its temporary workflow so the
final branch contains no migration machinery.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "backend" / "main.py"
WORKFLOW = ROOT / ".github" / "workflows" / "phase2-mangel-transform.yml"

text = MAIN.read_text(encoding="utf-8")
start_marker = "# 30.07 (Release-аудит P1): было два разных экземпляра mangel_lib в одном процессе"
end_marker = "# ---------- Фотоотчёт старт/финиш смены — Фаза 4a ----------"

if text.count(start_marker) != 1:
    raise SystemExit(f"expected exactly one Mangel start marker, found {text.count(start_marker)}")
if text.count(end_marker) != 1:
    raise SystemExit(f"expected exactly one Checkin boundary marker, found {text.count(end_marker)}")

start = text.index(start_marker)
end = text.index(end_marker, start)
legacy = text[start:end]

required_legacy_tokens = [
    '@app.get("/api/mangel")',
    '@app.post("/api/mangel")',
    '@app.get("/api/mangel/photos/{fname}/file")',
    '@app.patch("/api/mangel/{ticket_id}/status")',
    '@app.delete("/api/mangel/{ticket_id}")',
    'def require_mangel_access(',
    'def _enrich_mangel_tickets_with_author(',
]
for token in required_legacy_tokens:
    if token not in legacy:
        raise SystemExit(f"refusing transform: legacy token missing: {token}")

replacement = '''# ---------- Mangel / defects domain ----------
# The full defects HTTP/orchestration flow lives in routes/mangel.py. main.py is
# only the composition root and retains aliases for existing direct-call callers.
# Keep the canonical repo-backed module object for backwards compatibility with
# tests/internal code that patch backend.ml directly during the migration.
ml = _load_repo_mangel_lib()

if __package__:
    from .routes.mangel import (
        MangelCommentBody,
        MangelRouteDeps,
        MangelStatusBody,
        create_mangel_router,
    )
else:
    from routes.mangel import (  # noqa: E402
        MangelCommentBody,
        MangelRouteDeps,
        MangelStatusBody,
        create_mangel_router,
    )

_mangel_router, _mangel_handlers = create_mangel_router(
    MangelRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        load_mangel_lib=lambda: ml,
        load_objekte_lib=lambda: _load_repo_objekte_lib(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda value, fallback: _sanitize_display_name(value, fallback),
        sniff_image=lambda raw: sniff_image(raw),
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
        mangel_photo_dir=lambda: MANGEL_PHOTO_DIR,
        photo_dir=lambda: PHOTO_DIR,
        can_access_object=lambda user, role, object_id: can_access_object(user, role, object_id),
        append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(
            *args, **kwargs
        ),
    )
)
app.include_router(_mangel_router)

# Temporary compatibility surface. These are references to the extracted
# implementation; no Mangel business logic remains in main.py.
require_mangel_access = _mangel_handlers.require_mangel_access
_enrich_mangel_tickets_with_author = _mangel_handlers.enrich_mangel_tickets_with_author
get_mangel_list = _mangel_handlers.get_mangel_list
get_mangel_counts = _mangel_handlers.get_mangel_counts
get_mangel_ticket = _mangel_handlers.get_mangel_ticket
create_mangel_ticket = _mangel_handlers.create_mangel_ticket
get_mangel_photo_file = _mangel_handlers.get_mangel_photo_file
update_mangel_status = _mangel_handlers.update_mangel_status
add_mangel_comment = _mangel_handlers.add_mangel_comment
delete_mangel_ticket = _mangel_handlers.delete_mangel_ticket
get_mangel_comments = _mangel_handlers.get_mangel_comments


'''

updated = text[:start] + replacement + text[end:]

# Structural postconditions before writing.
if updated.count('create_mangel_router(') != 1:
    raise SystemExit("unexpected Mangel router factory count in main.py")
for prefix in ('@app.get("/api/mangel', '@app.post("/api/mangel', '@app.patch("/api/mangel', '@app.delete("/api/mangel'):
    if prefix in updated:
        raise SystemExit(f"direct Mangel route remained in main.py: {prefix}")
if len(updated) >= len(text):
    raise SystemExit("main.py did not shrink; refusing transform")

MAIN.write_text(updated, encoding="utf-8")

# Remove all one-shot migration machinery from the final tree.
Path(__file__).unlink()
if WORKFLOW.exists():
    WORKFLOW.unlink()

print(f"Mangel extraction wired; main.py shrank by {len(text) - len(updated)} bytes")
