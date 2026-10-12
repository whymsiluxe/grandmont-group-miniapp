#!/usr/bin/env python3
"""One-shot compatibility wiring for Phase 3 helper aliases."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "backend" / "main.py"
WORKFLOW = ROOT / ".github" / "workflows" / "phase3-commercial-helper-wiring.yml"
text = MAIN.read_text(encoding="utf-8")

old = '''        angebot_out_dir=lambda: ANGEBOT_OUT_DIR,
        rechnung_out_dir=lambda: RECHNUNG_OUT_DIR,
    )
)'''
new = '''        angebot_out_dir=lambda: ANGEBOT_OUT_DIR,
        rechnung_out_dir=lambda: RECHNUNG_OUT_DIR,
        # Late-bound compatibility: callers/tests that temporarily replace the
        # legacy main.py helper names still affect the extracted endpoints.
        send_pdf=lambda *args, **kwargs: send_pdf_to_chat(*args, **kwargs),
        generate_pdf=lambda *args, **kwargs: _run_pdf_generator(*args, **kwargs),
        make_pdf_filename=lambda prefix, customer: _safe_pdf_filename(prefix, customer),
    )
)'''
if text.count(old) != 1:
    raise SystemExit(f"expected exactly one commercial dependency block, found {text.count(old)}")
updated = text.replace(old, new, 1)
if updated.count("generate_pdf=lambda *args") != 1:
    raise SystemExit("late-bound generator hook missing after transform")
MAIN.write_text(updated, encoding="utf-8")
Path(__file__).unlink()
if WORKFLOW.exists():
    WORKFLOW.unlink()
print("commercial helper aliases are now late-bound")
