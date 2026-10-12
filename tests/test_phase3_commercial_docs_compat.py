"""Migration guard: Commercial Docs execution must not depend on main aliases."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main as backend  # noqa: E402
import routes.commercial_docs as commercial  # noqa: E402


def test_angebot_handler_uses_module_owned_helpers_not_main_aliases(monkeypatch):
    calls = {"generate": [], "filename": [], "telegram": []}

    def fail_main_helper(*args, **kwargs):
        raise AssertionError("extracted endpoint reached back through backend.main")

    # Legacy aliases may remain temporarily for direct-call consumers, but endpoint
    # execution must not resolve through them anymore.
    monkeypatch.setattr(backend, "_run_pdf_generator", fail_main_helper)
    monkeypatch.setattr(backend, "_safe_pdf_filename", fail_main_helper)
    monkeypatch.setattr(backend, "send_pdf_to_chat", fail_main_helper)

    def fake_generate(script_path, config, timeout=30):
        calls["generate"].append((script_path, dict(config), timeout))
        with open(config["outPath"], "wb") as handle:
            handle.write(b"%PDF-1.4\n")

    def fake_filename(prefix, customer):
        calls["filename"].append((prefix, customer))
        return "module-owned.pdf"

    class _FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout=20):
        calls["telegram"].append((request.full_url, timeout))
        return _FakeResponse()

    monkeypatch.setattr(commercial, "run_pdf_generator", fake_generate)
    monkeypatch.setattr(commercial, "safe_pdf_filename", fake_filename)
    monkeypatch.setattr(commercial.urlreq, "urlopen", fake_urlopen)

    body = backend.AngebotBody(
        kunde=backend.AngebotKunde(name="Patch Test"),
        positionen=[backend.AngebotPosition(titel="Arbeit", menge=1, preis=10)],
    )
    response = backend.create_angebot(body=body, user={"id": 77}, _=None)

    assert calls["generate"]
    assert calls["filename"] == [("Angebot", "Patch Test")]
    assert calls["telegram"], "module-owned Telegram delivery was not called"
    assert response.filename == "module-owned.pdf"
