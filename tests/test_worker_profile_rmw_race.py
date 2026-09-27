"""Concurrency regression tests for worker_profiles.json RMW races (Issue 1,
fix/miniapp-profile-rmw-outbox-retry).

storage.py documents that `data = _safe_load_json(...); mutate(data);
_atomic_write_json(...)` is NOT safe for read-modify-write -- two concurrent
requests can both read the same "before" state and the second silently
overwrites the first's change (lost update). Before this fix, the four
worker-profile mutation paths below used exactly that unsafe pattern:
  - _get_worker_skills_v2 (legacy skills_v2 normalization/migration)
  - update_my_profile (PATCH /api/profile/me)
  - verify_worker_skill (PATCH /api/workers/{id}/skills/{id}/verification, owner-only)
  - upload_my_avatar (POST /api/profile/me/avatar)

These tests fire two of these mutators concurrently against the SAME profile
and assert both changes survive (no lost update), matching the existing
test_json_transaction.py style: update_json_transaction's per-path lock
serializes the two threads, so we don't force literal interleaving inside the
critical section (that would just contend the same lock) -- we assert the
end state has both changes, which is what "no lost update" actually means.

Run:
    cd grandmont-group-miniapp && python3 -m pytest tests/test_worker_profile_rmw_race.py -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
import profile_skills as pskills  # noqa: E402


class WorkerProfileRmwRaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='grandmont-group-test-profile-race-')
        # Same leak-guard pattern as test_ui_fix_round_tofu_names.py -- save/restore
        # so this test's isolated path never bleeds into a later test in the same
        # pytest process.
        self._saved_attrs = {
            name: getattr(backend, name) for name in ('WORKER_PROFILES_FILE',)
        }
        backend.WORKER_PROFILES_FILE = os.path.join(self._tmp, 'worker_profiles.json')

    def tearDown(self):
        for name, value in self._saved_attrs.items():
            setattr(backend, name, value)

    def _read_profiles(self):
        with open(backend.WORKER_PROFILES_FILE, encoding='utf-8') as f:
            return json.load(f)

    # ---------- A: profile update vs owner skill verification ----------

    def test_profile_update_and_skill_verification_concurrent_both_survive(self):
        uid = '111000001'
        seed = {
            uid: {
                'name': 'Original Name',
                'skills_v2': [{'skill_id': 'demolition', 'level': 'independent', 'verified': False}],
                'quiz_completed': True,
            }
        }
        backend._save_worker_profiles(seed)

        user = {'id': int(uid), 'first_name': 'Original Name'}
        body = backend.ProfileUpdateBody(shirt_size='L')
        verify_body = backend.SkillVerificationBody(verified=True)

        errors = []

        def _do_profile_update():
            try:
                backend.update_my_profile(body=body, user=user)
            except Exception as e:  # pragma: no cover - surfaced via errors list
                errors.append(e)

        def _do_verify():
            try:
                backend.verify_worker_skill(
                    user_id=uid, skill_id='demolition', body=verify_body,
                    user={'id': 999, 'first_name': 'Owner'}, _=None,
                )
            except Exception as e:  # pragma: no cover
                errors.append(e)

        t1 = threading.Thread(target=_do_profile_update)
        t2 = threading.Thread(target=_do_verify)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(errors, [], f"unexpected errors: {errors}")

        profile = self._read_profiles()[uid]
        # Issue-1 fix: neither the shirt_size PATCH nor the owner's verification
        # may disappear -- both mutated the same profile concurrently.
        self.assertEqual(profile.get('shirt_size'), 'L',
            'profile PATCH must survive a concurrent owner skill verification')
        skills_by_id = {s['skill_id']: s for s in profile.get('skills_v2', [])}
        self.assertTrue(skills_by_id['demolition']['verified'],
            'owner skill verification must survive a concurrent profile PATCH')

    # ---------- B: skills_v2 migration vs concurrent legitimate profile change ----------

    def test_skills_v2_migration_does_not_overwrite_concurrent_profile_change(self):
        uid = '111000002'
        # Legacy shape: only 'skills' (names), no skills_v2 yet -- triggers migration
        # inside _get_worker_skills_v2 on first read.
        seed = {
            uid: {
                'name': 'Legacy Worker',
                'skills': ['Abbruch'],
                'quiz_completed': True,
            }
        }
        backend._save_worker_profiles(seed)

        user = {'id': int(uid), 'first_name': 'Legacy Worker'}
        body = backend.ProfileUpdateBody(pants_size='M')

        errors = []

        def _do_migration():
            try:
                backend._get_worker_skills_v2(uid)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def _do_profile_update():
            try:
                backend.update_my_profile(body=body, user=user)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        t1 = threading.Thread(target=_do_migration)
        t2 = threading.Thread(target=_do_profile_update)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(errors, [], f"unexpected errors: {errors}")

        profile = self._read_profiles()[uid]
        # Migration must not silently drop the concurrent pants_size change, and the
        # concurrent PATCH must not drop the migrated skills_v2 either -- both writes
        # happen under the same file lock via update_json_transaction now.
        self.assertEqual(profile.get('pants_size'), 'M',
            'concurrent legitimate profile change must survive a skills_v2 migration')
        self.assertTrue(profile.get('skills_v2'),
            'skills_v2 migration result must survive a concurrent profile PATCH')

    # ---------- C: avatar/profile metadata mutation preserves unrelated fields ----------

    def test_avatar_upload_preserves_concurrent_unrelated_profile_field(self):
        uid = '111000003'
        seed = {
            uid: {
                'name': 'Avatar Test',
                'skills_v2': [{'skill_id': 'demolition', 'level': 'helper', 'verified': False}],
                'quiz_completed': True,
            }
        }
        backend._save_worker_profiles(seed)

        user = {'id': int(uid), 'first_name': 'Avatar Test'}
        verify_body = backend.SkillVerificationBody(verified=True)

        errors = []

        def _set_avatar_flag():
            # Exercise only the profile-store RMW half of upload_my_avatar (the
            # disk-file part is a separate resource, out of scope for this race).
            try:
                def _mutator(profiles):
                    profile = profiles.get(uid, {"skills": [], "quiz_completed": False})
                    profile['avatar'] = True
                    if not profile.get('name'):
                        profile['name'] = user.get('first_name', uid)
                    profiles[uid] = profile
                backend.update_json_transaction(backend.WORKER_PROFILES_FILE, {}, _mutator)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def _do_verify():
            try:
                backend.verify_worker_skill(
                    user_id=uid, skill_id='demolition', body=verify_body,
                    user={'id': 999, 'first_name': 'Owner'}, _=None,
                )
            except Exception as e:  # pragma: no cover
                errors.append(e)

        t1 = threading.Thread(target=_set_avatar_flag)
        t2 = threading.Thread(target=_do_verify)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(errors, [], f"unexpected errors: {errors}")

        profile = self._read_profiles()[uid]
        self.assertTrue(profile.get('avatar'),
            'avatar flag must survive a concurrent skill verification')
        skills_by_id = {s['skill_id']: s for s in profile.get('skills_v2', [])}
        self.assertTrue(skills_by_id['demolition']['verified'],
            'concurrent skill verification must survive an avatar flag update')
        self.assertEqual(profile.get('name'), 'Avatar Test',
            'unrelated pre-existing name field must be untouched by either mutation')


if __name__ == '__main__':
    unittest.main()
