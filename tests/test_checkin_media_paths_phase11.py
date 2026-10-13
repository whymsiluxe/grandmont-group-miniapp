import os
from pathlib import Path

import pytest

from core.checkin_support import CheckinSupportDeps, CheckinSupportRuntime
from core.media_paths import resolve_owned_media_path


def _runtime(photo_base: Path) -> CheckinSupportRuntime:
    return CheckinSupportRuntime(CheckinSupportDeps(
        meta_file=lambda: str(photo_base / "meta.json"),
        photo_base=lambda: str(photo_base),
        max_bytes=1024,
        safe_load_json=lambda path, default: default,
        atomic_write_json=lambda path, data: None,
        sniff_image=lambda raw: None,
        allowed_image_mime_ext={},
        load_assignments=lambda: {},
        assignment_status=lambda assignment: "accepted",
    ))


def test_owned_media_path_allows_normal_nested_file(tmp_path):
    base = tmp_path / "checkin"
    nested = base / "OBJ-1" / "2026-10-13"
    nested.mkdir(parents=True)
    photo = nested / "photo.jpg"
    photo.write_bytes(b"photo")

    resolved = resolve_owned_media_path(str(base), "OBJ-1/2026-10-13/photo.jpg")

    assert resolved == str(photo.resolve())


@pytest.mark.parametrize("stored_path", ["../secret.jpg", "../../etc/passwd"])
def test_owned_media_path_rejects_parent_traversal(tmp_path, stored_path):
    base = tmp_path / "checkin"
    base.mkdir()

    assert resolve_owned_media_path(str(base), stored_path) is None


def test_owned_media_path_rejects_absolute_external_path(tmp_path):
    base = tmp_path / "checkin"
    base.mkdir()
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")

    assert resolve_owned_media_path(str(base), str(outside)) is None


def test_owned_media_path_rejects_symlink_escape(tmp_path):
    base = tmp_path / "checkin"
    base.mkdir()
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    link = base / "linked.jpg"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    assert resolve_owned_media_path(str(base), "linked.jpg") is None


def test_cleanup_never_deletes_file_outside_media_root(tmp_path):
    base = tmp_path / "checkin"
    base.mkdir()
    outside = tmp_path / "keep.txt"
    outside.write_text("keep", encoding="utf-8")
    runtime = _runtime(base)

    runtime.cleanup_photo_files(["../keep.txt", str(outside)])

    assert outside.read_text(encoding="utf-8") == "keep"


def test_cleanup_deletes_valid_owned_file(tmp_path):
    base = tmp_path / "checkin"
    nested = base / "OBJ-1"
    nested.mkdir(parents=True)
    owned = nested / "remove.jpg"
    owned.write_bytes(b"photo")
    runtime = _runtime(base)

    runtime.cleanup_photo_files([os.path.join("OBJ-1", "remove.jpg")])

    assert not owned.exists()


def test_checkin_photo_route_uses_owned_media_resolver():
    route_source = (
        Path(__file__).parents[1] / "backend" / "routes" / "checkin.py"
    ).read_text(encoding="utf-8")

    assert "resolve_owned_media_path" in route_source
    assert "os.path.join(deps.checkin_photo_base(), photos[index])" not in route_source
