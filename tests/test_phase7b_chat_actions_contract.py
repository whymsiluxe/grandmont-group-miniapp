"""Structural/API contract guard for Phase 7B Chat write/action extraction."""
from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


EXPECTED = {
    ('GET', '/api/chat/attachments/{fname}'): 'get_chat_attachment',
    ('POST', '/api/manager/broadcast'): 'send_manager_broadcast',
    ('POST', '/api/chat/messages'): 'post_chat_message',
    ('POST', '/api/chat/messages/{msg_id}/forward'): 'forward_chat_message',
    ('DELETE', '/api/chat/messages/{msg_id}'): 'delete_chat_message',
    ('DELETE', '/api/chat/threads'): 'delete_chat_thread',
    ('POST', '/api/chat/messages/{msg_id}/reactions'): 'toggle_chat_reaction',
    ('POST', '/api/chat/threads/prefs'): 'set_chat_thread_prefs',
    ('POST', '/api/chat/threads/close'): 'close_chat_thread',
    ('POST', '/api/chat/threads/reopen'): 'reopen_chat_thread',
    ('GET', '/api/chat/threads/status'): 'get_chat_thread_status',
}

OWNER_ONLY = {
    ('POST', '/api/manager/broadcast'),
    ('DELETE', '/api/chat/threads'),
    ('POST', '/api/chat/threads/close'),
    ('POST', '/api/chat/threads/reopen'),
}


def _route(method: str, path: str):
    matches = [r for r in iter_app_routes(backend.app) if r.path == path and method in r.methods]
    assert len(matches) == 1, f'{method} {path}: expected one route, got {len(matches)}'
    return matches[0]


def test_chat_action_routes_registered_once_and_owned_by_extracted_module():
    for (method, path), expected_name in EXPECTED.items():
        route = _route(method, path)
        endpoint = route.endpoint
        assert endpoint.__name__ == expected_name
        assert endpoint.__module__.endswith('routes.chat_actions'), endpoint.__module__
        assert getattr(backend, expected_name) is endpoint


def test_owner_only_dependencies_preserved():
    for method, path in OWNER_ONLY:
        route = _route(method, path)
        dependency_calls = {dep.call for dep in route.dependant.dependencies}
        assert backend.require_owner in dependency_calls, f'{method} {path} lost require_owner'


def test_chat_actions_has_no_back_import_to_main():
    repo = Path(__file__).resolve().parent.parent
    source = (repo / 'backend' / 'routes' / 'chat_actions.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name not in {'main', 'backend.main', 'miniapp.main'} for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in {'main', 'backend.main', 'miniapp.main'}


def test_main_no_longer_defines_chat_action_routes_directly():
    repo = Path(__file__).resolve().parent.parent
    source = (repo / 'backend' / 'main.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    direct_paths = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != 'app':
                continue
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                direct_paths.add((decorator.func.attr.upper(), decorator.args[0].value))
    assert not (set(EXPECTED) & direct_paths), set(EXPECTED) & direct_paths


def test_main_action_helpers_are_aliases_not_implementations():
    action_aliases = {
        '_normalize_chat_location': 'normalize_chat_location',
        '_reply_snapshot': 'reply_snapshot',
        '_chat_thread_participants': 'chat_thread_participants',
        '_check_message_access': 'check_message_access',
    }
    for public_name, handler_name in action_aliases.items():
        value = getattr(backend, public_name)
        assert value is getattr(backend._chat_actions_handlers, handler_name)
        assert inspect.getmodule(value).__name__.endswith('routes.chat_actions')

    assert backend._thread_user_prefs is backend._chat_handlers.thread_user_prefs
    assert inspect.getmodule(backend._thread_user_prefs).__name__.endswith('routes.chat')
