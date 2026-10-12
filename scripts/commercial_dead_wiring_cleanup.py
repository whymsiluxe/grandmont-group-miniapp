from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# 1) Remove migration-only dependency fields from the extracted Commercial Docs domain.
replace_once(
    "backend/routes/commercial_docs.py",
    '''    rechnung_out_dir: Callable[[], str]\n    # Deprecated migration-only fields. They are intentionally NOT consulted by\n    # endpoint execution anymore. main.py still passes them temporarily so this\n    # cleanup can land without editing the monolith again, but production flow is\n    # now fully owned by this module and no longer reaches back through main aliases.\n    send_pdf: Callable[..., None] | None = None\n    generate_pdf: Callable[..., subprocess.CompletedProcess] | None = None\n    make_pdf_filename: Callable[[str, str], str] | None = None\n''',
    '''    rechnung_out_dir: Callable[[], str]\n''',
    "commercial deps",
)

replace_once(
    "backend/routes/commercial_docs.py",
    '''        # Production execution deliberately resolves helpers in THIS module.\n        # Deprecated deps.* compatibility callbacks are ignored so the route can\n        # no longer call back through aliases in backend.main.\n        run_pdf_generator(deps.angebot_script(), config)\n''',
    '''        # PDF generation, filename creation and Telegram delivery are owned by\n        # this extracted domain; there is no runtime callback through backend.main.\n        run_pdf_generator(deps.angebot_script(), config)\n''',
    "commercial execution comment",
)

# 2) Remove the dead callbacks from composition-root wiring.
replace_once(
    "backend/main.py",
    '''        angebot_out_dir=lambda: ANGEBOT_OUT_DIR,\n        rechnung_out_dir=lambda: RECHNUNG_OUT_DIR,\n        # Late-bound compatibility: callers/tests that temporarily replace the\n        # legacy main.py helper names still affect the extracted endpoints.\n        send_pdf=lambda *args, **kwargs: send_pdf_to_chat(*args, **kwargs),\n        generate_pdf=lambda *args, **kwargs: _run_pdf_generator(*args, **kwargs),\n        make_pdf_filename=lambda prefix, customer: _safe_pdf_filename(prefix, customer),\n    )\n)\n''',
    '''        angebot_out_dir=lambda: ANGEBOT_OUT_DIR,\n        rechnung_out_dir=lambda: RECHNUNG_OUT_DIR,\n    )\n)\n''',
    "commercial main callbacks",
)

# 3) Retain only the direct-call endpoint/auth aliases that are still part of the
# compatibility surface. The helper aliases are dead and must not reappear.
replace_once(
    "backend/main.py",
    '''# Temporary direct-call compatibility while main.py is decomposed. These are\n# aliases only; all commercial-document implementation lives in the route module.\nrequire_angebot_access = _commercial_docs_handlers.require_angebot_access\nsend_pdf_to_chat = _commercial_docs_handlers.send_pdf_to_chat\n_run_pdf_generator = _commercial_docs_handlers.run_pdf_generator\n_safe_pdf_filename = _commercial_docs_handlers.safe_pdf_filename\ncreate_angebot = _commercial_docs_handlers.create_angebot\ncreate_rechnung = _commercial_docs_handlers.create_rechnung\n''',
    '''# Direct-call compatibility for the public endpoint/auth handlers. All helper\n# implementation stays private to routes.commercial_docs.\nrequire_angebot_access = _commercial_docs_handlers.require_angebot_access\ncreate_angebot = _commercial_docs_handlers.create_angebot\ncreate_rechnung = _commercial_docs_handlers.create_rechnung\n''',
    "commercial main helper aliases",
)

# 4) Replace the migration test with a regression guard for the final architecture.
Path("tests/test_phase3_commercial_docs_compat.py").write_text(
    '''"""Commercial Docs must remain independent from backend.main helper wiring."""\nimport os\nimport sys\n\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))\nimport main as backend  # noqa: E402\nimport routes.commercial_docs as commercial  # noqa: E402\n\n\ndef test_dead_commercial_main_wiring_is_absent():\n    for name in ("send_pdf_to_chat", "_run_pdf_generator", "_safe_pdf_filename"):\n        assert not hasattr(backend, name), f"dead Commercial Docs alias returned: {name}"\n\n    fields = set(commercial.CommercialDocsRouteDeps.__dataclass_fields__)\n    assert not {"send_pdf", "generate_pdf", "make_pdf_filename"} & fields\n\n\ndef test_angebot_handler_uses_module_owned_helpers(monkeypatch):\n    calls = {"generate": [], "filename": [], "telegram": []}\n\n    def fake_generate(script_path, config, timeout=30):\n        calls["generate"].append((script_path, dict(config), timeout))\n        with open(config["outPath"], "wb") as handle:\n            handle.write(b"%PDF-1.4\\n")\n\n    def fake_filename(prefix, customer):\n        calls["filename"].append((prefix, customer))\n        return "module-owned.pdf"\n\n    class _FakeResponse:\n        def __enter__(self):\n            return self\n        def __exit__(self, exc_type, exc, tb):\n            return False\n\n    def fake_urlopen(request, timeout=20):\n        calls["telegram"].append((request.full_url, timeout))\n        return _FakeResponse()\n\n    monkeypatch.setattr(commercial, "run_pdf_generator", fake_generate)\n    monkeypatch.setattr(commercial, "safe_pdf_filename", fake_filename)\n    monkeypatch.setattr(commercial.urlreq, "urlopen", fake_urlopen)\n\n    body = backend.AngebotBody(\n        kunde=backend.AngebotKunde(name="Patch Test"),\n        positionen=[backend.AngebotPosition(titel="Arbeit", menge=1, preis=10)],\n    )\n    response = backend.create_angebot(body=body, user={"id": 77}, _=None)\n\n    assert calls["generate"]\n    assert calls["filename"] == [("Angebot", "Patch Test")]\n    assert calls["telegram"], "module-owned Telegram delivery was not called"\n    assert response.filename == "module-owned.pdf"\n''',
    encoding="utf-8",
)

print("Commercial Docs dead wiring cleanup applied")
