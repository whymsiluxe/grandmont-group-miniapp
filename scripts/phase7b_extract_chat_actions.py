#!/usr/bin/env python3
from pathlib import Path

path = Path('backend/main.py')
text = path.read_text(encoding='utf-8')
start_marker = '@app.get("/api/chat/attachments/{fname}")\n'
end_marker = '# ---------- AI Chat (GLM / Sonnet / Opus, переключаемо) ----------\n'
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise SystemExit('chat actions extraction markers not found')
if text.find(start_marker, start + 1) >= 0:
    raise SystemExit('duplicate chat attachment route marker')

replacement = '''# ---------- Chat write/actions domain ----------\n# Message CRUD, reactions, thread controls, attachment serving and manager\n# broadcast live in routes/chat_actions.py and reuse the extracted Chat core.\nif __package__:\n    from .routes.chat_actions import (\n        BroadcastBody,\n        ChatActionsRouteDeps,\n        ChatMessageBody,\n        ChatReactionBody,\n        ChatThreadCloseBody,\n        ChatThreadPrefsBody,\n        create_chat_actions_router,\n    )\nelse:\n    from routes.chat_actions import (  # noqa: E402\n        BroadcastBody,\n        ChatActionsRouteDeps,\n        ChatMessageBody,\n        ChatReactionBody,\n        ChatThreadCloseBody,\n        ChatThreadPrefsBody,\n        create_chat_actions_router,\n    )\n\n_chat_actions_router, _chat_actions_handlers = create_chat_actions_router(\n    ChatActionsRouteDeps(\n        get_current_user=get_current_user,\n        get_role=get_role,\n        require_owner=require_owner,\n        core=_chat_handlers,\n        safe_load_json=lambda path, default: _safe_load_json(path, default),\n        chat_archive_file=lambda: CHAT_ARCHIVE_FILE,\n        chat_attach_dir=lambda: CHAT_ATTACH_DIR,\n        chat_reaction_options=CHAT_REACTION_OPTIONS,\n        load_roles=lambda: _load_roles(),\n        send_telegram_message=lambda chat_id, text: send_telegram_message(chat_id, text),\n        find_object_row_by_id=lambda object_id: _find_object_row_by_id(object_id),\n        append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(*args, **kwargs),\n    )\n)\napp.include_router(_chat_actions_router)\n\n# Temporary compatibility aliases; implementations live in chat_actions.py.\n_normalize_chat_location = _chat_actions_handlers.normalize_chat_location\n_reply_snapshot = _chat_actions_handlers.reply_snapshot\n_chat_thread_participants = _chat_actions_handlers.chat_thread_participants\n_check_message_access = _chat_actions_handlers.check_message_access\n_thread_user_prefs = _chat_handlers.thread_user_prefs\nget_chat_attachment = _chat_actions_handlers.get_chat_attachment\nsend_manager_broadcast = _chat_actions_handlers.send_manager_broadcast\npost_chat_message = _chat_actions_handlers.post_chat_message\nforward_chat_message = _chat_actions_handlers.forward_chat_message\ndelete_chat_message = _chat_actions_handlers.delete_chat_message\ndelete_chat_thread = _chat_actions_handlers.delete_chat_thread\ntoggle_chat_reaction = _chat_actions_handlers.toggle_chat_reaction\nset_chat_thread_prefs = _chat_actions_handlers.set_chat_thread_prefs\nclose_chat_thread = _chat_actions_handlers.close_chat_thread\nreopen_chat_thread = _chat_actions_handlers.reopen_chat_thread\nget_chat_thread_status = _chat_actions_handlers.get_chat_thread_status\n\n\n'''
new_text = text[:start] + replacement + text[end:]
if len(new_text) >= len(text):
    raise SystemExit('expected main.py to shrink')
path.write_text(new_text, encoding='utf-8')
print(f'main.py: {text.count(chr(10)) + 1} -> {new_text.count(chr(10)) + 1} lines')
