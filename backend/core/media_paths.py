"""Safe resolution of persisted media paths inside an owned storage root."""
from __future__ import annotations

import os


def resolve_owned_media_path(base_dir: str, stored_path: str) -> str | None:
    """Return a canonical path only when it stays strictly inside ``base_dir``.

    Persisted metadata is treated as untrusted input: absolute paths, ``..``
    traversal, paths resolving to the root itself, cross-drive paths and symlink
    escapes are rejected.  ``realpath`` is intentional so an in-root symlink to
    an external file cannot bypass the containment check.
    """
    if not isinstance(stored_path, str) or not stored_path.strip():
        return None

    base = os.path.realpath(os.path.abspath(base_dir))
    candidate = os.path.realpath(os.path.join(base, stored_path))
    try:
        if os.path.commonpath((base, candidate)) != base:
            return None
    except ValueError:
        return None
    if candidate == base:
        return None
    return candidate
