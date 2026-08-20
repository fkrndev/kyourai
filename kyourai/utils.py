"""Utility helpers — atomic file writes, simple helpers.

Ported from Hermes' utils.py (atomic_write_text only — the rest is not needed).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path,
    content: str,
    *,
    tmp_prefix: str = ".tmp_",
    encoding: str = "utf-8",
) -> None:
    """Write text to *path* atomically via temp-file + rename.

    Readers always see either the previous complete file or the new complete
    file — never a partially-written one. The temp file is created in the
    same directory to guarantee the rename is atomic on the same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=tmp_prefix, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_json_write(path: Path, data, *, indent: int = 2, sort_keys: bool = False) -> None:
    """Write JSON atomically."""
    import json
    content = json.dumps(data, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
    atomic_write_text(path, content)
