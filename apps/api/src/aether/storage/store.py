"""Object storage port and the MVP local-filesystem implementation (ADR-018).

Durable objects are content-addressed: ``artifacts/{sha[:2]}/{sha256}.{ext}``. Keys never contain
user input. Writes go to ``tmp/``, are fsynced, hashed and atomically renamed.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

ALLOWED_EXT = frozenset({"tif", "gpkg", "json", "html"})


@dataclass(frozen=True)
class ArtifactRef:
    sha256: str
    uri: str
    bytes: int


class ObjectStore(Protocol):
    def put_file(self, local_path: Path, ext: str) -> ArtifactRef: ...
    def open_read_path(self, sha256: str, ext: str) -> str: ...
    def exists(self, sha256: str, ext: str) -> bool: ...
    def tmp_path(self, suffix: str) -> Path: ...
    def delete(self, sha256: str, ext: str) -> None: ...


def _check(sha256: str, ext: str) -> None:
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("invalid sha256 key")
    if ext not in ALLOWED_EXT:
        raise ValueError(f"extension {ext!r} not allowed")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalFilesystemStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(parents=True, exist_ok=True)

    def _final(self, sha256: str, ext: str) -> Path:
        _check(sha256, ext)
        return self.root / "artifacts" / sha256[:2] / f"{sha256}.{ext}"

    def tmp_path(self, suffix: str) -> Path:
        return self.root / "tmp" / f"{uuid.uuid4().hex}{suffix}"

    def put_file(self, local_path: Path, ext: str) -> ArtifactRef:
        with local_path.open("rb") as f:
            os.fsync(f.fileno())
        sha = sha256_file(local_path)
        final = self._final(sha, ext)
        final.parent.mkdir(parents=True, exist_ok=True)
        size = local_path.stat().st_size
        if final.exists():
            local_path.unlink()  # identical content already stored (content addressing = dedup)
        else:
            os.replace(local_path, final)
        return ArtifactRef(sha, f"file://{final}", size)

    def open_read_path(self, sha256: str, ext: str) -> str:
        p = self._final(sha256, ext)
        if not p.exists():
            raise FileNotFoundError(sha256)
        return str(p)

    def exists(self, sha256: str, ext: str) -> bool:
        return self._final(sha256, ext).exists()

    def delete(self, sha256: str, ext: str) -> None:
        self._final(sha256, ext).unlink(missing_ok=True)
