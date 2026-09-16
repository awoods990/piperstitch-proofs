"""Artifact storage: a directory tree on the volume today, keyed the same
way an S3 bucket with object lock will be (PRD v1.1 change 2). Nothing
is ever overwritten: a key is written once, and a write to an existing
key with different bytes is refused.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config


class ImmutableKeyError(Exception):
    pass


def _path(key: str) -> Path:
    if ".." in key or key.startswith("/"):
        raise ValueError("bad storage key")
    return Path(config.ARTIFACT_DIR) / key


def put(key: str, data: bytes) -> str:
    p = _path(key)
    if p.exists():
        if p.read_bytes() == data:
            return key
        raise ImmutableKeyError(f"{key} already exists with different content")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return key


def get(key: str) -> bytes:
    return _path(key).read_bytes()


def exists(key: str) -> bool:
    return _path(key).exists()
