"""Static contract for the 2026-09-27 skills-selector redesign
(frontend/js/skill-picker.js) and the modal-close-bug fix
(frontend/js/swipe-nav.js) that shares the same bottom-sheet host.

Covers: no separate "Часто используемые" block, per-device usage-based
ordering frozen once per picker open (never re-sorted mid-session), whole-
section tri-state select-all/clear, individual skills still independently
toggleable, and the swipe-nav gesture handler excluding open bottom sheets
so a scroll/drag inside one can never look like the sheet "closing".
"""
import os

PICKER_JS = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'js', 'skill-picker.js')
SWIPE_JS = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'js', 'swipe-nav.js')
APP_HTML = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'app.html')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestNoSeparateFrequentlyUsedBlock:
    def test_featured_grid_markup_removed(self):
        src = _read(PICKER_JS)
        assert 'skill-picker-featured-grid' not in src
        assert 'skill-picker-featured-card' not in src
        # catalog.featured itself still legitimately resolves display names
        # in createSkillLevelPicker -- only the removed picker UI block (the
        # featuredHtml grid render) must be gone.
        assert 'featuredHtml' not in src


class TestFrequencyOrderingFrozenPerOpen:
    def test_usage_snapshot_read_once_outside_render(self):
        src = _read(PICKER_JS)
        # The snapshot must be captured once when createSkillPicker() runs
        # (i.e. once per open), not inside render(), which runs on every
        # toggle -- otherwise the list would re-sort under the user's finger.
        create_idx = src.index('async function createSkillPicker')
        render_idx = src.index('function render()', create_idx)
        between = src[create_idx:render_idx]
        assert 'const usageSnapshot = _loadSkillUsage();' in between

    def test_toggle_does_not_recompute_the_sort_order(self):
        src = _read(PICKER_JS)
        toggle_start = src.index('function toggle(id)')
        toggle_body = src[toggle_start:toggle_start + 400]
        assert 'sortedGroups' not in toggle_body
        assert '.sort(' not in toggle_body

    def test_toggle_bumps_usage_only_on_select_not_deselect(self):
        src = _read(PICKER_JS)
        toggle_start = src.index('function toggle(id)')
        toggle_end = src.index('\n  }', toggle_start)
        toggle_body = src[toggle_start:toggle_end]
        delete_idx = toggle_body.index('selected.delete(id)')
        first_bump_idx = toggle_body.index('_bumpSkillUsage(id)')
        assert delete_idx < first_bump_idx, "usage must not be bumped on the deselect branch"

    def test_usage_persisted_to_localstorage_not_sent_to_backend(self):
        src = _read(PICKER_JS)
        fn_start = src.index('function _bumpSkillUsage(id)')
        fn_end = src.index('\n}', fn_start)
        body = src[fn_start:fn_end]
        assert 'localStorage.setItem' in body
        assert "api(" not in body


class TestWholeSectionSelect:
    def test_group_state_is_tristate(self):
        src = _read(PICKER_JS)
        fn_start = src.index('function _groupState(g)')
        body = src[fn_start:fn_start + 300]
        assert "'none'" in body and "'all'" in body and "'partial'" in body

    def test_toggle_group_selects_all_when_not_all_selected(self):
        src = _read(PICKER_JS)
        fn_start = src.index('function toggleGroup(g)')
        body = src[fn_start:fn_start + 400]
        assert "state === 'all'" in body
        assert 'selected.delete(w.id)' in body
        assert 'selected.add(w.id)' in body

    def test_individual_rows_still_independently_toggleable(self):
        src = _read(PICKER_JS)
        assert "toggle(el.dataset.skillId)" in src

    def test_tristate_glyphs_present_in_render(self):
        src = _read(PICKER_JS)
        assert '☑' in src and '◐' in src and '☐' in src


class TestBottomSheetExcludedFromSwipeGesture:
    def test_bottom_sheet_overlay_in_exclusion_selector(self):
        src = _read(SWIPE_JS)
        fn_start = src.index('function _isExcludedSwipeTarget')
        body = src[fn_start:fn_start + 800]
        assert '.bottom-sheet-overlay' in body

    def test_touchstart_records_exclusion_before_touchend_checks_it(self):
        src = _read(SWIPE_JS)
        touchstart_idx = src.index("addEventListener('touchstart'")
        touchend_idx = src.index("addEventListener('touchend'")
        assert touchstart_idx < touchend_idx
        touchend_body = src[touchend_idx:touchend_idx + 200]
        assert '_touchStartOnExcludedEl' in touchend_body


class TestStickyDoneButton:
    def test_skills_sheet_submit_bar_is_sticky(self):
        src = _read(APP_HTML)
        idx = src.index('.profile-skills-sheet .form-submit-bar')
        rule = src[idx:idx + 200]
        assert 'position: sticky' in rule
        assert 'bottom: 0' in rule


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
