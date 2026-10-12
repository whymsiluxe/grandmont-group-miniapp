from pathlib import Path

path = Path('backend/main.py')
text = path.read_text(encoding='utf-8')
start = '# ---------- Раунд 6 §5: пересылка комментариев + in-app activity alerts ----------\n'
end = '# ---------- Chat core domain ----------\n'
insert_before = '\n_whisper_model = None\n'

assert text.count(start) == 1, f'bridge start marker count={text.count(start)}'
assert text.count(end) == 1, f'bridge end marker count={text.count(end)}'
assert text.count(insert_before) == 1, f'whisper marker count={text.count(insert_before)}'

before, rest = text.split(start, 1)
old_bridge, after = rest.split(end, 1)
text = before + '# ---------- Activity/comment bridge wiring is composed after Chat core. ----------\n\n' + end + after

wiring = '''
# ---------- Activity/comment bridge ----------
# This integration surface needs Feed/Photo plus Chat handlers, so composition
# intentionally happens after Chat core has been constructed.
if __package__:
    from .routes.activity_bridge import (
        ActivityBridgeDeps, ActivityReadBody, CommentForwardBody,
        create_activity_bridge_router,
    )
else:
    from routes.activity_bridge import (  # noqa: E402
        ActivityBridgeDeps, ActivityReadBody, CommentForwardBody,
        create_activity_bridge_router,
    )

_activity_bridge_router, _activity_bridge_handlers = create_activity_bridge_router(
    ActivityBridgeDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        activity_alerts_file=lambda: ACTIVITY_ALERTS_FILE,
        news_feed_file=lambda: NEWS_FEED_FILE,
        photo_dir=lambda: PHOTO_DIR,
        cached_get_used_range=lambda tab_name: _cached_get_used_range(tab_name),
        load_roles=lambda: _load_roles(),
        load_news_comments=lambda: _load_news_comments(),
        photo_lock=_photo_lock,
        load_photo_meta=lambda: _load_photo_meta(),
        check_thread_access=lambda thread_key, uid, role: _check_thread_access(thread_key, uid, role),
        reject_self_chat=lambda uid, target_uid: _reject_self_chat(uid, target_uid),
        validate_dm_recipient=lambda target_uid: _validate_dm_recipient(target_uid),
        chat_thread_id=lambda uid, target_uid: _chat_thread_id(uid, target_uid),
        load_chat_thread_meta=lambda: _load_chat_thread_meta(),
        chat_lock=_chat_lock,
        load_chat=lambda: _load_chat(),
        save_chat=lambda messages: _save_chat(messages),
    )
)
app.include_router(_activity_bridge_router)

# Compatibility aliases for existing alert aggregation, tests and callers.
_load_activity_alerts = _activity_bridge_handlers.load_activity_alerts
_save_activity_alerts = _activity_bridge_handlers.save_activity_alerts
_news_post_title = _activity_bridge_handlers.news_post_title
_find_object_row_by_id = _activity_bridge_handlers.find_object_row_by_id
_resolve_object_name = _activity_bridge_handlers.resolve_object_name
_comment_alert_recipients = _activity_bridge_handlers.comment_alert_recipients
_emit_comment_activity_alerts = _activity_bridge_handlers.emit_comment_activity_alerts
mark_activity_alerts_read = _activity_bridge_handlers.mark_activity_alerts_read
forward_comment = _activity_bridge_handlers.forward_comment
get_feed_photo_file = _activity_bridge_handlers.get_feed_photo_file
'''

text = text.replace(insert_before, '\n' + wiring + insert_before, 1)
path.write_text(text, encoding='utf-8')
print('activity bridge block replaced:', len(old_bridge.splitlines()), 'old lines ->', len(wiring.splitlines()), 'wiring lines')
