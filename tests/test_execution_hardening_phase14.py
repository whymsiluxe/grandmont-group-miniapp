from __future__ import annotations

import ast
from pathlib import Path


def _execution_factory():
    path = Path(__file__).resolve().parents[1] / "backend" / "routes" / "execution.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_execution_router"
    )
    functions = {
        node.name: node
        for node in factory.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return path, functions


def test_manual_overlap_check_and_append_share_checkin_lock():
    _, functions = _execution_factory()
    handler = functions["checkin_manual"]

    lock_blocks = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Attribute)
            and isinstance(item.context_expr.value, ast.Name)
            and item.context_expr.value.id == "deps"
            and item.context_expr.attr == "checkin_lock"
            for item in node.items
        )
    ]
    assert len(lock_blocks) == 1
    lock_block = lock_blocks[0]

    calls_inside_lock = {
        node.func.id
        for node in ast.walk(lock_block)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_assert_no_manual_time_overlap" in calls_inside_lock

    append_inside_lock = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "items"
        for node in ast.walk(lock_block)
    )
    assert append_inside_lock

    # The overlap check must not also run outside the serialized section.
    overlap_calls_total = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_assert_no_manual_time_overlap"
    ]
    assert len(overlap_calls_total) == 1


def test_ai_photo_reads_resolve_persisted_paths_inside_owned_media_root():
    path, functions = _execution_factory()
    glm = functions["_call_glm_vision"]

    resolver_calls = [
        node
        for node in ast.walk(glm)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_owned_media_path"
    ]
    assert resolver_calls, "AI media reads must pass persisted paths through containment resolver"

    source = path.read_text(encoding="utf-8")
    assert "os.path.join(deps.checkin_photo_base(), p)" not in source
    assert "resolve_owned_media_path(deps.checkin_photo_base(), p)" in source
