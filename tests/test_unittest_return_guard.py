import ast
from pathlib import Path


def test_daily_plan_owner_test_does_not_return_a_value():
    path = Path(__file__).resolve().parent / 'test_daily_plan.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))

    target = None
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        if cls.name != 'DailyPlanRouteTests':
            continue
        target = next(
            (
                node for node in cls.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == 'test_create_and_get_plan_as_owner'
            ),
            None,
        )
        break

    assert target is not None
    direct_returns = [node for node in target.body if isinstance(node, ast.Return)]
    assert not direct_returns, 'unittest test method must not return a value'
