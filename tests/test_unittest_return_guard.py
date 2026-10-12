import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_unittest_test_methods_do_not_return_values():
    offenders = []
    for path in ROOT.glob('test_*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for func in (node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
                if not func.name.startswith('test_'):
                    continue
                for node in ast.walk(func):
                    if isinstance(node, ast.Return) and node.value is not None:
                        offenders.append(f'{path.name}:{cls.name}.{func.name}:{node.lineno}')
    assert not offenders, 'unittest-style test methods returned values: ' + ', '.join(offenders)
