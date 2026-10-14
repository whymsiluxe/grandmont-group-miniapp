"""Phase 20 regression: GET /profile/me must not overwrite a concurrent PATCH."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from unittest.mock import patch

import main as backend


def test_profile_name_auto_heal_preserves_fresh_concurrent_profile_state():
    with tempfile.TemporaryDirectory() as tmp:
        profiles_file = os.path.join(tmp, 'worker_profiles.json')
        stale_snapshot = {
            '42': {
                'skills': [],
                'quiz_completed': False,
            }
        }
        concurrent_state = {
            '42': {
                'name': 'User Chosen Name',
                'skills': [],
                'quiz_completed': False,
                'pants_size': 'XL',
                'birthday': '1990-01-02',
            }
        }
        with open(profiles_file, 'w', encoding='utf-8') as handle:
            json.dump(stale_snapshot, handle)

        def stale_read_after_concurrent_patch():
            # Model the race precisely: GET has already obtained an old snapshot,
            # while a PATCH commits newer profile fields before GET heals the name.
            with open(profiles_file, 'w', encoding='utf-8') as handle:
                json.dump(concurrent_state, handle)
            return copy.deepcopy(stale_snapshot)

        with (
            patch.object(backend, 'WORKER_PROFILES_FILE', profiles_file),
            patch.object(
                backend,
                '_load_worker_profiles',
                side_effect=stale_read_after_concurrent_patch,
            ),
            patch.object(backend, '_load_roles', return_value={'42': 'worker'}),
        ):
            result = backend.get_my_profile(
                user={'id': '42', 'first_name': 'Telegram Name'}
            )

        with open(profiles_file, encoding='utf-8') as handle:
            persisted = json.load(handle)['42']

        assert persisted['name'] == 'User Chosen Name'
        assert persisted['pants_size'] == 'XL'
        assert persisted['birthday'] == '1990-01-02'
        assert result['name'] == 'User Chosen Name'
        assert result['pants_size'] == 'XL'
        assert result['birthday'] == '1990-01-02'
