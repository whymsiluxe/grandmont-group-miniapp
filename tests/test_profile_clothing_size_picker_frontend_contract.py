"""Static contract for the 2026-09-27 clothing-size UX fix
(frontend/js/profile.js): pants/shirt size no longer require typing -- both
became native <select> pickers (no keyboard, standard OS picker UI) with
predefined size lists, while preserving/displaying a legacy stored value
that isn't one of the predefined options instead of silently dropping it.
Shoe size is intentionally left as free text -- out of this task's scope.
"""
import os

PROFILE_JS = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'js', 'profile.js')


def _read():
    with open(PROFILE_JS, encoding='utf-8') as f:
        return f.read()


class TestPredefinedSizeLists:
    def test_shirt_sizes_cover_xs_through_5xl(self):
        src = _read()
        idx = src.index('const PROFILE_SHIRT_SIZES')
        line = src[idx:src.index('\n', idx)]
        for size in ['XS', 'S', 'M', 'L', 'XL', 'XXL', '3XL', '4XL', '5XL']:
            assert f"'{size}'" in line

    def test_pants_sizes_are_numeric(self):
        src = _read()
        idx = src.index('const PROFILE_PANTS_SIZES')
        line = src[idx:src.index('\n', idx)]
        for size in ['44', '46', '48', '50', '52', '54', '56', '58', '60']:
            assert f"'{size}'" in line


class TestNoKeyboardEntryForSize:
    def test_pants_and_shirt_are_select_elements_not_text_inputs(self):
        src = _read()
        assert '<select id="profile-size-pants"' in src
        assert '<select id="profile-size-shirt"' in src
        assert '<input id="profile-size-pants"' not in src
        assert '<input id="profile-size-shirt"' not in src

    def test_shoe_size_intentionally_left_as_text_input(self):
        src = _read()
        assert '<input id="profile-size-shoe"' in src


class TestLegacyValuePreserved:
    def test_fill_size_select_injects_unknown_value_as_its_own_option(self):
        src = _read()
        fn_start = src.index('function _fillSizeSelect(')
        body = src[fn_start:fn_start + 700]
        assert 'opts.includes(currentValue)' in body
        assert 'opts.unshift(currentValue)' in body

    def test_fill_size_select_marks_legacy_value_selected(self):
        src = _read()
        fn_start = src.index('function _fillSizeSelect(')
        body = src[fn_start:fn_start + 700]
        assert "v === currentValue ? ' selected'" in body

    def test_load_applies_predefined_lists_via_helper(self):
        src = _read()
        idx = src.index("_fillSizeSelect('profile-size-pants'")
        assert 'PROFILE_PANTS_SIZES' in src[idx:idx + 120]
        idx = src.index("_fillSizeSelect('profile-size-shirt'")
        assert 'PROFILE_SHIRT_SIZES' in src[idx:idx + 120]


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
