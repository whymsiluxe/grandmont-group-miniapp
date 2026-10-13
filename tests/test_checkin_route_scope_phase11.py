import ast
from pathlib import Path


def _factory_tree():
    source_path = Path(__file__).parents[1] / "backend" / "routes" / "checkin.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_checkin_router"
    )
    return tree, factory


def test_checkin_handlers_remain_direct_children_of_router_factory():
    _, factory = _factory_tree()
    direct_handlers = {
        node.name
        for node in factory.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = {
        "checkin_start",
        "checkin_pause",
        "checkin_finish",
        "export_stundenzettel",
        "list_checkins",
        "checkin_finish_context",
        "get_checkin_photo",
    }

    assert required <= direct_handlers


def test_checkin_handlers_do_not_escape_to_module_scope():
    tree, _ = _factory_tree()
    module_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    protected = {
        "checkin_start",
        "checkin_pause",
        "checkin_finish",
        "export_stundenzettel",
        "list_checkins",
        "checkin_finish_context",
        "get_checkin_photo",
    }

    assert protected.isdisjoint(module_functions)
