"""Structural/API contract guard for the Phase 7 Chat core extraction."""
from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402


EXPECTED = {
    ('GET', '/api/chat/my_threads'): 'get_my_chat_threads',
    ('GET', '/api/chat/threads'): 'get_normalized_chat_threads',
    ('GET', '/api/chat/messages'): 'get_chat_messages',
    ('GET', '/api/chat/unread_count'): 'get_unread_count',
    ('GET', '/api/chat/unread_by_thread'): 'get_unread_by_thread',
    ('POST', '/api/chat/read'): 'mark_chat_read',
    ('POST', '/api/chat/messages/attachment'): 'post_chat_attachment',
}


def _flatten_routes(router):
    """Flatten FastAPI 0.139 included-router wrappers recursively."""
    seen = set()

    def walk(routes):
        for route in routes:
            nested = getattr(route, 'routes', None)
            if nested is not None and id(route) not in seen:
                seen.add(id(route))
                yield from walk(nested)
            if getattr(route, 'path', None) and getattr(route, 'methods', None):
                yield route

    return list(walk(router.routes))


def test_chat_core_routes_registered_once_and_owned_by_extracted_module():
    routes = _flatten_routes(backend.app.router)
    for (method, path), expected_name in EXPECTED.items():
        matches = [r for r in routes if r.path == path and method in r.methods]
        assert len(matches) == 1, f'{method} {path}: expected one route, got {len(matches)}'
        endpoint = matches[0].endpoint
        assert endpoint.__name__ == expected_name
        assert endpoint.__module__.endswith('routes.chat'), endpoint.__module__
        assert getattr(backend, expected_name) is endpoint


def test_chat_core_has_no_back_import_to_main():
    repo = Path(__file__).resolve().parent.parent
    source = (repo / 'backend' / 'routes' / 'chat.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name not in {'main', 'backend.main', 'miniapp.main'} for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in {'main', 'backend.main', 'miniapp.main'}


def test_main_no_longer_defines_extracted_chat_routes_directly():
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


def test_main_chat_helpers_are_aliases_not_implementations():
    aliases = {
        '_load_chat': 'load_chat',
        '_save_chat': 'save_chat',
        '_check_thread_access': 'check_thread_access',
        '_chat_thread_id': 'chat_thread_id',
        '_message_preview': 'message_preview',
        '_load_chat_thread_meta': 'load_chat_thread_meta',
        '_load_chat_reactions': 'load_chat_reactions',
    }
    for public_name, handler_name in aliases.items():
        assert getattr(backend, public_name) is getattr(backend._chat_handlers, handler_name)
        assert inspect.getmodule(getattr(backend, public_name)).__name__.endswith('routes.chat')
