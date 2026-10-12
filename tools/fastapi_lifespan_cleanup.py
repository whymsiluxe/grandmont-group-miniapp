#!/usr/bin/env python3
from pathlib import Path

path = Path('backend/main.py')
text = path.read_text(encoding='utf-8')

old_import = 'from contextvars import ContextVar\n'
new_import = 'from contextvars import ContextVar\nfrom contextlib import asynccontextmanager\n'
if text.count(old_import) != 1:
    raise SystemExit(f'context import marker count={text.count(old_import)}')
text = text.replace(old_import, new_import, 1)

setup_start = 'app = FastAPI(title="Grandmont Group Mini App", docs_url=None, redoc_url=None, openapi_url=None)\n'
startup_marker = '@app.on_event("startup")\n'
if text.count(setup_start) != 1 or text.count(startup_marker) != 1:
    raise SystemExit('app/startup markers not unique')
start = text.index(setup_start)
startup_at = text.index(startup_marker)
setup_block = text[start:startup_at]
text = text[:start] + text[startup_at:]

if text.count(startup_marker) != 1:
    raise SystemExit('startup decorator marker changed unexpectedly')
text = text.replace(startup_marker, '', 1)
shutdown_marker = '@app.on_event("shutdown")\n'
if text.count(shutdown_marker) != 1:
    raise SystemExit(f'shutdown decorator count={text.count(shutdown_marker)}')
text = text.replace(shutdown_marker, '', 1)

exception_marker = '@app.exception_handler(CorruptJsonError)\n'
if text.count(exception_marker) != 1:
    raise SystemExit(f'exception marker count={text.count(exception_marker)}')

old_fastapi_line = 'app = FastAPI(title="Grandmont Group Mini App", docs_url=None, redoc_url=None, openapi_url=None)\n'
new_fastapi_line = 'app = FastAPI(title="Grandmont Group Mini App", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)\n'
if setup_block.count(old_fastapi_line) != 1:
    raise SystemExit('FastAPI constructor not found exactly once in setup block')
setup_block = setup_block.replace(old_fastapi_line, new_fastapi_line, 1)

lifespan_block = '''@asynccontextmanager\nasync def _lifespan(_app):\n    """FastAPI lifespan wrapper preserving the legacy startup/shutdown behavior."""\n    await _on_startup()\n    try:\n        yield\n    finally:\n        await _on_shutdown()\n\n\n'''
insert = lifespan_block + setup_block
text = text.replace(exception_marker, insert + exception_marker, 1)

if '@app.on_event(' in text:
    raise SystemExit('deprecated app.on_event decorator remains')
if 'lifespan=_lifespan' not in text:
    raise SystemExit('FastAPI lifespan wiring missing')

path.write_text(text, encoding='utf-8')
print('FastAPI lifespan migration applied')
