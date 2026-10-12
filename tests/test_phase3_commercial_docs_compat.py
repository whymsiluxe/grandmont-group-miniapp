"""Migration-compatibility guard for late-bound commercial-document helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main as backend  # noqa: E402


def test_angebot_handler_uses_late_bound_main_helpers(monkeypatch):
    calls = {"generate": [], "send": [], "filename": []}

    def fake_generate(script_path, config, timeout=30):
        calls["generate"].append((script_path, dict(config), timeout))
        # FileResponse may inspect/use this path after construction; make the
        # fake faithful enough that the endpoint remains executable.
        with open(config["outPath"], "wb") as handle:
            handle.write(b"%PDF-1.4\n")

    def fake_filename(prefix, customer):
        calls["filename"].append((prefix, customer))
        return "patched.pdf"

    def fake_send(chat_id, file_path, filename, caption):
        calls["send"].append((chat_id, file_path, filename, caption))

    monkeypatch.setattr(backend, "_run_pdf_generator", fake_generate)
    monkeypatch.setattr(backend, "_safe_pdf_filename", fake_filename)
    monkeypatch.setattr(backend, "send_pdf_to_chat", fake_send)

    body = backend.AngebotBody(
        kunde=backend.AngebotKunde(name="Patch Test"),
        positionen=[backend.AngebotPosition(titel="Arbeit", menge=1, preis=10)],
    )
    response = backend.create_angebot(body=body, user={"id": 77}, _=None)

    assert calls["generate"], "legacy _run_pdf_generator monkey-patch was bypassed"
    assert calls["filename"] == [("Angebot", "Patch Test")]
    assert calls["send"] and calls["send"][0][0] == 77
    assert calls["send"][0][2] == "patched.pdf"
    assert response.filename == "patched.pdf"
