"""Safe resolution of persisted media paths inside an owned storage root."""
from __future__ import annotations

import os


def resolve_owned_media_path(base_dir: str, stored_path: str) -> str | None:
    """Return a canonical path only when it stays strictly inside ``base_dir``.

    Persisted metadata is treated as untrusted input: absolute paths, ``..``
    traversal, paths resolving to the root itself, cross-drive paths and symlink
    aliases are rejected.  The lexical path must equal its ``realpath`` so a
    symlink cannot redirect one media identity to another file, even when both
    files live under the same storage root.
    """
    if not isinstance(stored_path, str) or not stored_path.strip():
        return None

    base = os.path.realpath(os.path.abspath(base_dir))
    lexical_candidate = os.path.abspath(os.path.join(base, stored_path))
    candidate = os.path.realpath(lexical_candidate)
    try:
        if os.path.commonpath((base, candidate)) != base:
            return None
    except ValueError:
        return None
    if candidate == base:
        return None
    if candidate != lexical_candidate:
        return None
    return candidate
