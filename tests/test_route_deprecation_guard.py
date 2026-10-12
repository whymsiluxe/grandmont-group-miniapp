from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cleaned_route_deprecations_do_not_return():
    files = {
        "workforce_profiles": ROOT / "backend/routes/workforce_profiles.py",
        "photo_feed": ROOT / "backend/routes/photo_feed.py",
        "mangel": ROOT / "backend/routes/mangel.py",
        "objects": ROOT / "backend/routes/objects.py",
    }
    sources = {name: path.read_text(encoding="utf-8") for name, path in files.items()}

    for name in ("workforce_profiles", "photo_feed", "mangel"):
        assert "datetime.utcnow(" not in sources[name], name

    assert "body.dict(exclude_unset=True)" not in sources["objects"]
    assert "body.model_dump(exclude_unset=True)" in sources["objects"]
