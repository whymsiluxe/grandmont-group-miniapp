"""cleanup_old_attachments cleanup behavior and retention coverage guards.

The configured retention period and covered data categories are policy choices.
These tests exercise the currently configured cleanup without expanding the set
of automatically deleted live-data categories.
"""
import errno
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import cleanup_old_attachments as cleanup  # noqa: E402

DAY = 86400


class CleanupDirTests(unittest.TestCase):
    def test_file_older_than_cutoff_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, 'old.jpg')
            with open(fpath, 'wb') as f:
                f.write(b'x')
            old_time = cleanup.CUTOFF - DAY
            os.utime(fpath, (old_time, old_time))

            removed = cleanup.cleanup_dir(tmp)

            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(fpath))

    def test_file_younger_than_cutoff_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, 'fresh.jpg')
            with open(fpath, 'wb') as f:
                f.write(b'x')

            removed = cleanup.cleanup_dir(tmp)

            self.assertEqual(removed, 0)
            self.assertTrue(os.path.exists(fpath))

    def test_nonexistent_directory_is_a_noop(self):
        removed = cleanup.cleanup_dir('/no/such/grandmont-group/dir')
        self.assertEqual(removed, 0)

    def test_unremovable_file_is_reported_without_aborting_the_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_time = cleanup.CUTOFF - DAY
            paths = []
            for name in ('a.jpg', 'b.jpg'):
                fpath = os.path.join(tmp, name)
                with open(fpath, 'wb') as f:
                    f.write(b'x')
                os.utime(fpath, (old_time, old_time))
                paths.append(fpath)

            real_remove = os.remove

            def flaky_remove(path):
                if path.endswith('a.jpg'):
                    raise OSError('simulated permission error')
                return real_remove(path)

            errors = []
            with patch.object(cleanup.os, 'remove', side_effect=flaky_remove):
                removed = cleanup.cleanup_dir(tmp, errors=errors)

            self.assertEqual(removed, 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0][0], paths[0])
            self.assertIn('simulated permission error', errors[0][1])
            self.assertTrue(os.path.exists(paths[0]))
            self.assertFalse(os.path.exists(paths[1]))

    def test_walk_error_is_reported(self):
        errors = []

        def fake_walk(path, onerror=None):
            locked = os.path.join(path, 'locked')
            exc = PermissionError(errno.EACCES, 'simulated scandir failure', locked)
            if onerror:
                onerror(exc)
            return []

        with patch.object(cleanup.os.path, 'isdir', return_value=True), patch.object(
            cleanup.os, 'walk', side_effect=fake_walk
        ):
            removed = cleanup.cleanup_dir('/retained', errors=errors)

        self.assertEqual(removed, 0)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], '/retained/locked')
        self.assertIn('simulated scandir failure', errors[0][1])


class CleanupMainObservabilityTests(unittest.TestCase):
    def test_main_returns_nonzero_and_does_not_claim_ok_on_filesystem_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, 'old.jpg')
            with open(fpath, 'wb') as f:
                f.write(b'x')
            old_time = cleanup.CUTOFF - DAY
            os.utime(fpath, (old_time, old_time))

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(cleanup, 'DIRS', [tmp]), patch.object(
                cleanup.os, 'remove', side_effect=OSError('permission denied')
            ), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
                result = cleanup.main()

            self.assertEqual(result, 1)
            self.assertNotIn('OK: nothing to clean', stdout.getvalue())
            self.assertIn('ERROR: cleanup failed for', stderr.getvalue())
            self.assertIn('permission denied', stderr.getvalue())
            self.assertIn('cleanup incomplete', stderr.getvalue())

    def test_main_returns_zero_when_nothing_needs_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(cleanup, 'DIRS', [tmp]), patch(
                'sys.stdout', stdout
            ), patch('sys.stderr', stderr):
                result = cleanup.main()

            self.assertEqual(result, 0)
            self.assertIn('OK: nothing to clean', stdout.getvalue())
            self.assertEqual(stderr.getvalue(), '')


class RetentionCoverageGapTests(unittest.TestCase):
    def test_transcribe_audio_dir_not_yet_covered_by_cleanup(self):
        """Keep draft retention categories out of automatic deletion by accident.

        ``docs/DATA_RETENTION_POLICY.md`` explicitly marks its periods as a draft
        needing owner/legal review.  If transcribe_audio is deliberately added to
        automatic cleanup, update the policy decision and this guard together.
        """
        covered_basenames = {os.path.basename(d.rstrip('/')) for d in cleanup.DIRS}
        self.assertNotIn(
            'transcribe_audio', covered_basenames,
            "transcribe_audio/ is now auto-deleted; record the retention-policy "
            "decision and update this guard intentionally."
        )


if __name__ == '__main__':
    unittest.main()
