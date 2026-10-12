from pathlib import Path

path = Path('backend/main.py')
source = path.read_text(encoding='utf-8')
start_marker = '# ---------- Critical Alerts — persisted, с deadline/comment/photo (Фаза 10.16) ----------\n'
end_marker = '# ---------- Abwesenheit domain ----------\n'
if source.count(start_marker) != 1 or source.count(end_marker) != 1:
    raise SystemExit('critical alerts markers mismatch')
start = source.index(start_marker)
end = source.index(end_marker, start)
old_lines = source[start:end].count('\n')
replacement = r'''# ---------- Critical Alerts domain ----------
try:
    from .routes.critical_alerts import (
        CriticalAlertAckBody,
        CriticalAlertCreateBody,
        CriticalAlertsDeps,
        create_critical_alerts_router,
    )
except ImportError:
    from routes.critical_alerts import (  # noqa: E402
        CriticalAlertAckBody,
        CriticalAlertCreateBody,
        CriticalAlertsDeps,
        create_critical_alerts_router,
    )

_critical_alerts_router, _critical_alerts_handlers = create_critical_alerts_router(
    CriticalAlertsDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        critical_alerts_file=lambda: CRITICAL_ALERTS_FILE,
        critical_alert_photo_dir=lambda: CRITICAL_ALERT_PHOTO_DIR,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
        load_roles=lambda: _load_roles(),
        send_telegram_message=lambda chat_id, text: send_telegram_message(chat_id, text),
        chat_lock=_chat_lock,
        load_chat=lambda: _load_chat(),
        save_chat=lambda messages: _save_chat(messages),
        chat_thread_id=lambda a, b: _chat_thread_id(a, b),
        sniff_image=lambda raw: sniff_image(raw),
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
    )
)
app.include_router(_critical_alerts_router)

_load_critical_alerts = _critical_alerts_handlers.load_critical_alerts
_save_critical_alerts = _critical_alerts_handlers.save_critical_alerts
_is_legacy_bare_uid_birthday_ref = _critical_alerts_handlers.is_legacy_bare_uid_birthday_ref
_supersede_legacy_siblings = _critical_alerts_handlers.supersede_legacy_siblings
_create_critical_alert = _critical_alerts_handlers.create_critical_alert
_ensure_critical_alert_chat = _critical_alerts_handlers.ensure_critical_alert_chat
ack_critical_alert = _critical_alerts_handlers.ack_critical_alert
resolve_critical_alert = _critical_alerts_handlers.resolve_critical_alert
get_critical_alert_photo = _critical_alerts_handlers.get_critical_alert_photo
list_pending_critical_alerts = _critical_alerts_handlers.list_pending_critical_alerts
create_critical_alert_endpoint = _critical_alerts_handlers.create_critical_alert_endpoint


'''
source = source[:start] + replacement + source[end:]
path.write_text(source, encoding='utf-8')
print(f'critical alerts block replaced: {old_lines} old lines -> {replacement.count(chr(10))} wiring lines')

# Move white-box guards to the actual implementation owner. The guarantees stay
# the same; only the obsolete assumption that implementation lives in main.py is removed.
race_path = Path('tests/test_critical_alert_ack_race.py')
race = race_path.read_text(encoding='utf-8')
race = race.replace(
    "assert 'update_json_transaction(CRITICAL_ALERTS_FILE' in src",
    "assert 'deps.update_json_transaction(' in src\n        assert 'deps.critical_alerts_file()' in src",
)
race_path.write_text(race, encoding='utf-8')

dedup_path = Path('tests/test_critical_alert_dedup.py')
dedup = dedup_path.read_text(encoding='utf-8')
old = '''        with patch.object(backend, '_ensure_critical_alert_chat') as mock_chat, \\
             patch.object(backend, 'send_telegram_message') as mock_push:\n            backend._create_critical_alert(\n                target_user_id=OWNER_ID, kind='plan_overdue', title='t', ref_id='2026-09-22',\n            )\n            backend._create_critical_alert(\n                target_user_id=OWNER_ID, kind='plan_overdue', title='t', ref_id='2026-09-22',\n            )\n        self.assertEqual(mock_chat.call_count, 1)\n        self.assertEqual(mock_push.call_count, 1)\n'''
new = '''        with patch.object(backend, '_load_chat', return_value=[]), \\
             patch.object(backend, '_save_chat') as mock_chat_save, \\
             patch.object(backend, 'send_telegram_message') as mock_push:\n            backend._create_critical_alert(\n                target_user_id=OWNER_ID, kind='plan_overdue', title='t', ref_id='2026-09-22',\n            )\n            backend._create_critical_alert(\n                target_user_id=OWNER_ID, kind='plan_overdue', title='t', ref_id='2026-09-22',\n            )\n        self.assertEqual(mock_chat_save.call_count, 1)\n        self.assertEqual(mock_push.call_count, 1)\n'''
if dedup.count(old) != 1:
    raise SystemExit('critical alert dedup test marker mismatch')
dedup_path.write_text(dedup.replace(old, new), encoding='utf-8')
