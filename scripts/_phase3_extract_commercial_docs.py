#!/usr/bin/env python3
"""One-shot bounded transform for Phase 3 commercial-document extraction."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "backend" / "main.py"
WORKFLOW = ROOT / ".github" / "workflows" / "phase3-commercial-docs-transform.yml"

text = MAIN.read_text(encoding="utf-8")

angebot_start_marker = "def send_pdf_to_chat(chat_id, file_path, filename, caption):"
angebot_end_marker = "# ---------- Aufgaben (tasks) ----------"
rechnung_start_marker = "# ---------- Rechnung generator ----------"
rechnung_end_marker = "# ---------- Weather feed ----------"

for marker in (angebot_start_marker, angebot_end_marker, rechnung_start_marker, rechnung_end_marker):
    if text.count(marker) != 1:
        raise SystemExit(f"expected exactly one marker {marker!r}, found {text.count(marker)}")

angebot_start = text.index(angebot_start_marker)
angebot_end = text.index(angebot_end_marker, angebot_start)
rechnung_start = text.index(rechnung_start_marker, angebot_end)
rechnung_end = text.index(rechnung_end_marker, rechnung_start)

angebot_legacy = text[angebot_start:angebot_end]
rechnung_legacy = text[rechnung_start:rechnung_end]

for token in (
    "def send_pdf_to_chat(",
    "def _run_pdf_generator(",
    "def _safe_pdf_filename(",
    "def _require_server_script(",
    "class AngebotKunde(BaseModel):",
    "class AngebotPosition(BaseModel):",
    "class AngebotBody(BaseModel):",
    '@app.post("/api/angebot")',
):
    if token not in angebot_legacy:
        raise SystemExit(f"refusing transform: Angebot token missing: {token}")

for token in (
    "class RechnungKunde(BaseModel):",
    "class RechnungPosition(BaseModel):",
    "class RechnungBody(BaseModel):",
    '@app.post("/api/rechnung")',
):
    if token not in rechnung_legacy:
        raise SystemExit(f"refusing transform: Rechnung token missing: {token}")

replacement = '''def _require_server_script(script_path: str, label: str):
    # Shared infrastructure helper: Objects also depends on this, so it stays in
    # the composition/core area rather than being owned by commercial documents.
    if not script_path or not os.path.isfile(script_path):
        raise HTTPException(500, f'{label} не найден: {script_path or "-"}')
    return script_path


ANGEBOT_SCRIPT = os.path.join(BACKEND_DIR, 'angebot_free.js')
RECHNUNG_SCRIPT = os.path.join(BACKEND_DIR, 'rechnung.js')
# moved to core/paths.py -- ANGEBOT_OUT_DIR / RECHNUNG_OUT_DIR
os.makedirs(ANGEBOT_OUT_DIR, exist_ok=True)
os.makedirs(RECHNUNG_OUT_DIR, exist_ok=True)


# ---------- Commercial documents domain (Angebot + Rechnung) ----------
if __package__:
    from .routes.commercial_docs import (
        AngebotBody,
        AngebotKunde,
        AngebotPosition,
        CommercialDocsRouteDeps,
        RechnungBody,
        RechnungKunde,
        RechnungPosition,
        create_commercial_docs_router,
    )
else:
    from routes.commercial_docs import (  # noqa: E402
        AngebotBody,
        AngebotKunde,
        AngebotPosition,
        CommercialDocsRouteDeps,
        RechnungBody,
        RechnungKunde,
        RechnungPosition,
        create_commercial_docs_router,
    )

_commercial_docs_router, _commercial_docs_handlers = create_commercial_docs_router(
    CommercialDocsRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        bot_token=lambda: BOT_TOKEN,
        angebot_script=lambda: ANGEBOT_SCRIPT,
        rechnung_script=lambda: RECHNUNG_SCRIPT,
        angebot_out_dir=lambda: ANGEBOT_OUT_DIR,
        rechnung_out_dir=lambda: RECHNUNG_OUT_DIR,
    )
)
app.include_router(_commercial_docs_router)

# Temporary direct-call compatibility while main.py is decomposed. These are
# aliases only; all commercial-document implementation lives in the route module.
require_angebot_access = _commercial_docs_handlers.require_angebot_access
send_pdf_to_chat = _commercial_docs_handlers.send_pdf_to_chat
_run_pdf_generator = _commercial_docs_handlers.run_pdf_generator
_safe_pdf_filename = _commercial_docs_handlers.safe_pdf_filename
create_angebot = _commercial_docs_handlers.create_angebot
create_rechnung = _commercial_docs_handlers.create_rechnung


'''

# Replace the later block first so offsets for the earlier block stay valid.
updated = text[:rechnung_start] + text[rechnung_end:]
# Recompute earlier indices against the original prefix, which is unchanged by the later deletion.
updated = updated[:angebot_start] + replacement + updated[angebot_end:]

# Structural postconditions.
if updated.count("create_commercial_docs_router(") != 1:
    raise SystemExit("unexpected commercial router factory count")
for token in ('@app.post("/api/angebot")', '@app.post("/api/rechnung")'):
    if token in updated:
        raise SystemExit(f"direct commercial route remained in main.py: {token}")
if updated.count("def _require_server_script(") != 1:
    raise SystemExit("shared _require_server_script was lost or duplicated")
if "require_server_script=lambda script_path, label: _require_server_script" not in updated:
    raise SystemExit("Objects shared-script dependency no longer references _require_server_script")
if len(updated) >= len(text):
    raise SystemExit("main.py did not shrink; refusing transform")

MAIN.write_text(updated, encoding="utf-8")

# Self-remove all migration machinery from the final tree.
Path(__file__).unlink()
if WORKFLOW.exists():
    WORKFLOW.unlink()

print(f"commercial docs extraction wired; main.py shrank by {len(text) - len(updated)} bytes")
