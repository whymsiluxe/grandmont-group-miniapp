from pathlib import Path


def test_storage_uses_non_deprecated_utc_timestamp():
    source = (Path(__file__).resolve().parents[1] / "backend/core/storage.py").read_text(encoding="utf-8")
    assert "datetime.utcnow(" not in source
    assert "datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'" in source
