"""Cache key for the Document JSON cache.

Phase 4f-2 introduced caching of Whisper inference: a freshly-transcribed
:class:`~core.document.Document` is written to disk as a JSON sidecar and
reused on subsequent runs against the same source media file. The cache
key is a sha256 of ``absolute_path || mtime_int || size_int`` — a
"is this the same file" heuristic that's wrong only when a user replaces
a media file in-place with the exact same byte count and mtime, an edge
case where requiring an explicit re-transcribe is acceptable.

Why not a content hash: full sha256 of a multi-GB media file is slow.
Path+mtime+size is the same heuristic ``rsync`` uses by default.

Why include the absolute path: a moved or renamed file is a different
project context and should miss the cache.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# A NUL byte cannot appear in any of the three components — it's not
# valid in POSIX paths, and stat() returns ints — so it's a safe field
# separator that prevents adjacent fields from running together.
_FIELD_SEP = b"\x00"


def cache_key(source_path: Path) -> str:
    """Return the cache key for ``source_path``.

    The key is the hex sha256 digest of
    ``absolute_path_bytes || NUL || mtime_int || NUL || size_int``.
    Raises :class:`FileNotFoundError` if the path doesn't exist.
    """
    stat = os.stat(source_path)
    abs_path_bytes = bytes(Path(source_path).absolute())
    mtime_bytes = str(int(stat.st_mtime)).encode("ascii")
    size_bytes = str(int(stat.st_size)).encode("ascii")
    h = hashlib.sha256()
    h.update(abs_path_bytes)
    h.update(_FIELD_SEP)
    h.update(mtime_bytes)
    h.update(_FIELD_SEP)
    h.update(size_bytes)
    return h.hexdigest()
