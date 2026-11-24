"""Durable vanilla replay staging and crash recovery (modes 0..3 only).

A passed replay is staged, flushed and fsynced BEFORE the score SQL commit.
After commit, publish using the explicit SQL replay link. Legacy checksum-only
recovery is disabled; unrelated pending files are kept for manual review.
This filesystem module does not run SQL migrations or modify RX/AP.
"""
from __future__ import annotations

import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CHECKSUM_RE = re.compile(r"[0-9a-fA-F]{32}\Z")


def _sync_dir(directory: Path) -> None:
    """Make creation/rename directory entries durable on Linux."""
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _spool_lock(pending_dir: Path):
    """Linux advisory directory lock for cooperative writers on one filesystem.

    Do not hold across any 'await': file operations here must remain synchronous.
    All replay staging, finalizing, discarding and recovery writers must use it.
    """
    fd = os.open(
        pending_dir,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _publish_staged_replay(staged: Path, destination: Path) -> None:
    """Atomically publish a durable replay without clobbering an existing name.

    The two paths must be on the same filesystem. The durable spool remains
    until the destination directory entry has been fsynced.
    """
    os.link(staged, destination, follow_symlinks=False)
    _sync_dir(destination.parent)
    staged.unlink()
    _sync_dir(staged.parent)


@dataclass(frozen=True)
class StagedVanillaReplay:
    path: Path
    replay_dir: Path

    def finalize(self, score_id: int) -> Path:
        if int(score_id) <= 0:
            raise ValueError("score id must be positive")
        destination = self.replay_dir / f"{int(score_id)}.osr"
        with _spool_lock(self.path.parent):
            # Never overwrite an existing score replay, even in a retry.
            if destination.exists():
                raise FileExistsError(f"score replay already exists: {destination.name}")
            _publish_staged_replay(self.path, destination)
        return destination

    def discard(self) -> None:
        with _spool_lock(self.path.parent):
            self.path.unlink(missing_ok=True)


def stage_vanilla_replay(replay_dir: Path, checksum: str, data: bytes) -> StagedVanillaReplay:
    """Raise before SQL COMMIT if replay cannot be durably staged."""
    if not _CHECKSUM_RE.fullmatch(checksum):
        raise ValueError("invalid score checksum")
    if len(data) < 24:
        raise ValueError("replay too short to stage")
    replay_dir = Path(replay_dir)
    pending_dir = replay_dir / ".vanilla-pending"
    pending_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _spool_lock(pending_dir):
        fd, name = tempfile.mkstemp(
            prefix=f"{checksum.lower()}.", suffix=".osr.writing", dir=pending_dir
        )
        writing = Path(name)
        staged = writing.with_suffix(".pending")  # ends with .osr.pending
        try:
            with os.fdopen(fd, "wb") as stream:
                # mkstemp is 0600; published hardlink must be readable by Shiina-Web.
                os.fchmod(stream.fileno(), 0o644)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Do not expose partially written files to the recovery worker.
            os.replace(writing, staged)
            _sync_dir(pending_dir)
        except BaseException:
            writing.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)
            raise
        return StagedVanillaReplay(staged, replay_dir)


async def recover_staged_vanilla_replays(
    db: Any, replay_dir: Path, *, limit: int = 25
) -> int:
    """Disabled legacy API: checksum alone cannot establish replay identity.

    No database queries, directory traversal, publication or cleanup. Retain
    all pending files for linked recovery or explicit manual review. Use
    recover_linked_vanilla_replays for actual recovery.
    """
    if not 1 <= limit <= 200:
        raise ValueError("limit out of range")
    return 0
