"""Worker UX V2, Этап 6 — contextual quick actions (Фото/Потребность/Дефект/Чат).

Source-assertion style, same as the rest of this repo's *_frontend_contract.py
suite. Covers: context resolution priority order matches the plan exactly
(active shift -> Object Detail -> single eligible assignment -> picker), and
each of the 4 actions reuses an EXISTING canonical flow/endpoint rather than
building a second implementation.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QA_JS = ROOT / "frontend" / "js" / "worker-quick-actions.js"
FEED_JS = ROOT / "frontend" / "js" / "feed.js"
HOME_JS = ROOT / "frontend" / "js" / "home.js"
APP_HTML = ROOT / "frontend" / "app.html"
PHOTO_FEED_PY = ROOT / "backend" / "routes" / "photo_feed.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fn(src: str, signature: str) -> str:
    start = src.index(signature)
    depth = 0
    i = src.index("{", start)
    j = i
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1


def test_context_resolution_priority_order_matches_plan():
    src = _source(QA_JS)
    body = _fn(src, "async function resolveWorkerActionObject(")

    active_idx = body.index("active_shift")
    detail_idx = body.index("object_detail")
    single_idx = body.index("single_assignment")
    ambiguous_idx = body.index("ambiguous")

    assert active_idx < detail_idx < single_idx < ambiguous_idx
    assert "resolveWorkerShiftState" in body
    assert "_workerActionCurrentObjectDetailId" in body
    assert "eligible.length === 1" in body


def test_object_detail_context_requires_view_objects_active_and_stages_view_open():
    src = _source(QA_JS)
    body = _fn(src, "function _workerActionCurrentObjectDetailId(")
    assert "document.getElementById('view-objects')" in body
    assert "viewObjects.classList.contains('active')" in body
    assert "document.getElementById('stages-view')" in body
    assert "stagesView.classList.contains('open')" in body
    assert "_stagesCurrentObjectId" in body

    view_objects_idx = body.index("viewObjects.classList.contains('active')")
    stages_view_idx = body.index("stagesView.classList.contains('open')")
    assert view_objects_idx < stages_view_idx


def test_object_detail_context_also_supports_the_owner_style_detail_panel_id():
    src = _source(QA_JS)
    body = _fn(src, "function _workerActionCurrentObjectDetailId(")
    assert "_objDetailCurrentId" in body


def test_ambiguous_context_opens_picker_not_silent_attach():
    src = _source(QA_JS)
    body = _fn(src, "async function runWorkerQuickAction(")
    assert "_openWorkerQuickActionObjectPicker" in body
    assert "if (ctx.objectId) {" in body


def test_photo_action_reuses_existing_feed_upload_endpoint():
    qa_body = _fn(_source(QA_JS), "function _workerQuickActionPhoto(")
    assert "_uploadFeedPhoto(" in qa_body
    assert "runWorkerQuickAction(" in qa_body

    feed_src = _source(FEED_JS)
    assert "async function _uploadFeedPhoto(files, objectId)" in feed_src
    assert "formData.append('object_id', objectId)" in feed_src

    # Backend canonical endpoint now lives in the extracted photo-feed domain.
    photo_src = _source(PHOTO_FEED_PY)
    start = photo_src.index("async def upload_feed_photo(")
    end = photo_src.index("\n\n", start)
    assert "object_id: str = Form('')" in photo_src[start:end]
    assert "@router.post('/api/feed/photos')" in photo_src


def test_need_action_reuses_existing_tasks_form_no_duplicate_validation():
    body = _fn(_source(QA_JS), "function _workerQuickActionNeed(")
    assert "switchView('tasks')" in body
    assert "_populateTasksObjectSelect" in body
    assert "tasks-object-select" in body
    assert "tasks-new-btn" in body
    assert "api('/api/tasks'" not in body


def test_defect_action_reuses_existing_mangel_form_no_duplicate_validation():
    body = _fn(_source(QA_JS), "function _workerQuickActionDefect(")
    assert "switchView('mangel')" in body
    assert "_populateMangelObjectSelect" in body
    assert "mangel-object-select" in body
    assert "_openMangelForm" in body
    assert "api('/api/mangel'" not in body


def test_chat_action_reuses_existing_object_chat_open_path():
    body = _fn(_source(QA_JS), "function _workerQuickActionChat(")
    assert "openObjectOrMangelChat" in body
    assert "`obj:${objectId}`" in body


def test_quick_actions_registered_in_script_load_order_after_dependencies():
    src = _source(APP_HTML)
    qa_idx = src.index('<script src="js/worker-quick-actions.js">')
    shift_state_idx = src.index('<script src="js/worker-shift-state.js">')
    objects_idx = src.index('<script src="js/objects.js">')
    assert shift_state_idx < qa_idx
    assert objects_idx < qa_idx


def test_home_renders_four_quick_action_tiles_wired_to_the_four_handlers():
    src = _source(HOME_JS)
    body = _fn(src, "async function initWorkerHomeView(")
    assert body.count("worker-home-quick-action") >= 4
    assert "_workerQuickActionPhoto" in body
    assert "_workerQuickActionNeed" in body
    assert "_workerQuickActionDefect" in body
    assert "_workerQuickActionChat" in body
