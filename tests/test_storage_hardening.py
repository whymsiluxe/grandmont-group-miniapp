import asyncio
import io
import os
import sys
import tempfile
import unittest

from fastapi import UploadFile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.mangel import MangelRouteDeps, create_mangel_router  # noqa: E402


class _FailingMangelLib:
    def create_ticket(self, **kwargs):
        raise RuntimeError('ticket persistence failed')


class StorageHardeningTests(unittest.TestCase):
    def test_mangel_create_removes_fresh_photo_when_ticket_persistence_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _, handlers = create_mangel_router(MangelRouteDeps(
                get_current_user=lambda: {},
                get_role=lambda: 'worker',
                require_owner=lambda: None,
                load_mangel_lib=lambda: _FailingMangelLib(),
                load_objekte_lib=lambda: None,
                load_worker_profiles=lambda: {},
                sanitize_display_name=lambda value, fallback: value or fallback,
                sniff_image=lambda raw: 'image/jpeg',
                allowed_image_mime_ext={'image/jpeg': 'jpg'},
                mangel_photo_dir=lambda: td,
                photo_dir=lambda: td,
                can_access_object=lambda user, role, object_id: True,
                append_object_history_best_effort=lambda *args, **kwargs: None,
            ))
            upload = UploadFile(filename='defect.jpg', file=io.BytesIO(b'\xff\xd8\xfftest'))

            with self.assertRaisesRegex(RuntimeError, 'ticket persistence failed'):
                asyncio.run(handlers.create_mangel_ticket(
                    object_id='OBJ-1',
                    description='Broken wall',
                    assigned_worker_id='',
                    file=upload,
                    user={'id': 10},
                    role='worker',
                ))

            self.assertEqual(os.listdir(td), [])


if __name__ == '__main__':
    unittest.main()
