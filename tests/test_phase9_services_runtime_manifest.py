from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_services_directory_is_transactional_runtime_artifact():
    manifest = (REPO_ROOT / "scripts" / "manifest.sh").read_text()
    runtime = (REPO_ROOT / "scripts" / "runtime_manifest.sh").read_text()

    assert 'BACKEND_SERVICES_DIR="services"' in manifest
    assert 'BACKEND_RUNTIME_ENTRIES+=("${BACKEND_SERVICES_DIR}/")' in runtime


def test_ci_package_smoke_and_static_checks_include_services():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "cp -r backend/services /tmp/ci-pkgtest/miniapp/" in ci
    assert "backend/services --select F" in ci
    assert "backend/services/*.py" in ci
