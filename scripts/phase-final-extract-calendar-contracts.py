#!/usr/bin/env python3
from pathlib import Path

path = Path('backend/main.py')
src = path.read_text(encoding='utf-8')

timing_start = src.index('def _photo_pause_seconds(s: dict) -> int:')
timing_end = src.index('\ndef _session_hours_live(s: dict) -> float:', timing_start)
timing_wiring = '''_photo_pause_seconds = _checkin_support.photo_pause_seconds\n_photo_pause_minutes = _checkin_support.photo_pause_minutes\n_is_active_photo_checkin_session = _checkin_support.is_active_photo_checkin_session\n_hours_from_session = _checkin_support.hours_from_session\n\n'''
# _checkin_support itself is wired later in source today; move these aliases with
# that wiring instead of leaving forward references here.
src = src[:timing_start] + src[timing_end:]

support_start = src.index('def _load_checkin_meta() -> list:')
support_end = src.index('@app.get("/api/workers/{target_user_id}/calendar")', support_start)
support_wiring = '''# Check-in store/photo/access/timing support moved to core/checkin_support.py.\ntry:\n    from .core.checkin_support import CheckinSupportDeps, CheckinSupportRuntime\nexcept ImportError:\n    from core.checkin_support import CheckinSupportDeps, CheckinSupportRuntime  # noqa: E402\n\n_checkin_support = CheckinSupportRuntime(CheckinSupportDeps(\n    meta_file=lambda: CHECKIN_META_FILE,\n    photo_base=lambda: CHECKIN_PHOTO_BASE,\n    max_bytes=CHECKIN_MAX_BYTES,\n    safe_load_json=lambda path, default: _safe_load_json(path, default),\n    atomic_write_json=lambda path, value: _atomic_write_json(path, value),\n    sniff_image=lambda raw: sniff_image(raw),\n    allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,\n    load_assignments=lambda: _load_assignments(),\n    assignment_status=lambda assignment: _assignment_status(assignment),\n))\n_load_checkin_meta = _checkin_support.load_meta\n_save_checkin_meta = _checkin_support.save_meta\n_save_checkin_photos = _checkin_support.save_photos\n_checkin_file_count = _checkin_support.file_count\n_cleanup_checkin_photo_files = _checkin_support.cleanup_photo_files\n_get_active_assignment_for_checkin = _checkin_support.get_active_assignment\n_photo_pause_seconds = _checkin_support.photo_pause_seconds\n_photo_pause_minutes = _checkin_support.photo_pause_minutes\n_is_active_photo_checkin_session = _checkin_support.is_active_photo_checkin_session\n_hours_from_session = _checkin_support.hours_from_session\n\n'''
src = src[:support_start] + support_wiring + src[support_end:]

calendar_start = src.index('@app.get("/api/workers/{target_user_id}/calendar")')
calendar_end = src.index('\ntry:\n    from .routes.checkin import', calendar_start)
calendar_wiring = '''# ---------- Worker calendar domain ----------\ntry:\n    from .routes.worker_calendar import WorkerCalendarDeps, create_worker_calendar_router\nexcept ImportError:\n    from routes.worker_calendar import WorkerCalendarDeps, create_worker_calendar_router  # noqa: E402\n\n_worker_calendar_router, _worker_calendar_handlers = create_worker_calendar_router(\n    WorkerCalendarDeps(\n        get_current_user=get_current_user,\n        get_role=get_role,\n        load_absence=lambda: _load_abwesenheit(),\n        load_checkin_meta=lambda: _load_checkin_meta(),\n        load_assignments=lambda: _load_assignments(),\n        hours_from_session=lambda session: _hours_from_session(session),\n        get_worker_profile=lambda uid: _get_worker_profile(uid),\n        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),\n    )\n)\napp.include_router(_worker_calendar_router)\nget_worker_calendar = _worker_calendar_handlers.get_worker_calendar\n_worker_period_stats = _worker_calendar_handlers.worker_period_stats\nget_worker_calendar_stats = _worker_calendar_handlers.get_worker_calendar_stats\n\n'''
src = src[:calendar_start] + calendar_wiring + src[calendar_end:]

contracts_start = src.index('# ── Contract ingestion routes (Round 5 — Drive scope gated)')
contracts_end = src.index('\n# 31.07 (Release-аудит П4): регистрация критичных сторов', contracts_start)
contracts_wiring = '''# ---------- Contract ingestion domain ----------\ntry:\n    from .routes.contracts import ContractReviewBody, ContractsDeps, create_contracts_router\nexcept ImportError:\n    from routes.contracts import ContractReviewBody, ContractsDeps, create_contracts_router  # noqa: E402\n\n_contracts_router, _contracts_handlers = create_contracts_router(ContractsDeps(\n    get_current_user=get_current_user,\n    require_owner=require_owner,\n    safe_load_json=lambda path, default: _safe_load_json(path, default),\n    atomic_write_json=lambda path, value: _atomic_write_json(path, value),\n    state_file=lambda: CONTRACT_INGEST_STATE_FILE,\n    drive_folder_id=lambda: CONTRACTS_DRIVE_FOLDER_ID,\n    now=lambda: time.time(),\n))\napp.include_router(_contracts_router)\n_CONTRACT_INGEST_LOCK = _contracts_handlers.lock\n_load_contract_store = _contracts_handlers.load_contract_store\n_save_contract_store = _contracts_handlers.save_contract_store\nlist_contracts = _contracts_handlers.list_contracts\nget_contract = _contracts_handlers.get_contract\napprove_contract = _contracts_handlers.approve_contract\nreject_contract = _contracts_handlers.reject_contract\ntrigger_contract_ingest = _contracts_handlers.trigger_contract_ingest\n\n'''
src = src[:contracts_start] + contracts_wiring + src[contracts_end:]

path.write_text(src, encoding='utf-8')
print('main.py lines after transform:', len(src.splitlines()))
assert len(src.splitlines()) <= 3000, len(src.splitlines())
