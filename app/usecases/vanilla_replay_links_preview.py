"""Vanilla replay identity and linked recovery.

Used by both submit handlers, manual approval, and the background worker.
DB links MUST be inserted in the same SQL transaction as the score.
Deployment requires the corresponding schema and a completed release review.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import hashlib
import os
from pathlib import Path
import re
from typing import Any

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_CHECKSUM = re.compile(r"[a-f0-9]{32}\Z")
_PENDING = re.compile(r"[a-f0-9]{32}\.[A-Za-z0-9_-]+\.osr\.pending\Z")


@dataclass(frozen=True)
class ReplayIntent:
    pending_name: str
    payload_sha256: str
    online_checksum: str


def _file_sha256(path: Path) -> str:
    """Hash via no-follow FD; files in the spool are cooperative/immutable."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        result = hashlib.sha256()
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(block)
        return result.hexdigest()
    finally:
        if fd >= 0:
            os.close(fd)


def make_replay_intent(staged: Any, checksum: str, payload: bytes) -> ReplayIntent:
    """Describe an already-durable staged replay, without trusting its name alone."""
    checksum = checksum.lower()
    path = Path(staged.path)
    expected_dir = Path(staged.replay_dir) / ".vanilla-pending"
    if (
        not _CHECKSUM.fullmatch(checksum)
        or not _PENDING.fullmatch(path.name)
        or path.name.split(".", 1)[0] != checksum
        or path.parent != expected_dir
        or path.is_symlink()
        or not path.is_file()
        or len(payload) < 24
    ):
        raise ValueError("invalid staged vanilla replay intent")
    digest = hashlib.sha256(payload).hexdigest()
    if _file_sha256(path) != digest:
        raise ValueError("staged replay bytes do not match supplied payload")
    return ReplayIntent(path.name, digest, checksum)


async def insert_replay_link(db: Any, score_id: int, intent: ReplayIntent) -> None:
    """Caller MUST invoke this inside score INSERT's existing db.transaction()."""
    if (
        int(score_id) <= 0
        or not _PENDING.fullmatch(intent.pending_name)
        or not _SHA256.fullmatch(intent.payload_sha256)
        or not _CHECKSUM.fullmatch(intent.online_checksum)
        or intent.pending_name.split(".", 1)[0] != intent.online_checksum
    ):
        raise ValueError("invalid replay link")
    await db.execute(
        "INSERT INTO vanilla_replay_links "
        "(score_id, pending_name, payload_sha256, online_checksum) "
        "VALUES (:score_id, :pending_name, :payload_sha256, :online_checksum)",
        {"score_id": int(score_id), "pending_name": intent.pending_name,
         "payload_sha256": intent.payload_sha256,
         "online_checksum": intent.online_checksum},
    )




# REVIEW 54.9q: durable, shared round-robin cursor for the linked-only worker.
# This state is kept in the same durable spool as staged replay files.
_CURSOR_FILE = ".vanilla-linked-recovery.cursor"
_CURSOR_FORMAT = re.compile(rb"v1 ([0-9]{1,20}) ([0-9]{1,20})\n\Z")
_MAX_CURSOR = (1 << 64) - 1
_MAX_SCANNED_PER_TICK = 2000


def _read_linked_cursor(pending_dir: Path) -> tuple[int, int]:
    """(generation, after_score_id). Reject malformed or redirected files."""
    import stat

    path = pending_dir / _CURSOR_FILE
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return 0, 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid linked recovery cursor type")
        data = os.read(fd, 96)
    finally:
        os.close(fd)
    match = _CURSOR_FORMAT.fullmatch(data)
    if match is None:
        raise ValueError("invalid linked recovery cursor contents")
    generation, after = (int(match.group(1)), int(match.group(2)))
    if generation > _MAX_CURSOR or after > _MAX_CURSOR:
        raise ValueError("linked recovery cursor out of range")
    return generation, after


def _write_linked_cursor(pending_dir: Path, generation: int, after: int) -> None:
    """Atomic replacement + file and directory fsync; caller holds spool lock."""
    import tempfile

    if not (0 <= generation <= _MAX_CURSOR and 0 <= after <= _MAX_CURSOR):
        raise ValueError("linked recovery cursor out of range")
    fd, temp_name = tempfile.mkstemp(
        prefix=".vanilla-linked-cursor.", suffix=".writing", dir=pending_dir
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(f"v1 {generation} {after}\n".encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, pending_dir / _CURSOR_FILE)
        dirfd = os.open(pending_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        temp.unlink(missing_ok=True)


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _hash_snapshot(path: Path):
    """Hash a regular no-follow FD outside flock, then capture its identity.

    Cooperative writers never edit published/staged bytes in place. Revalidate
    identity under flock before linking/ACK. Not protection against a hostile
    writer with unrestricted access to the replay directory.
    """
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular replay")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
        if _identity(before) != _identity(after):
            raise OSError("replay changed while hashing")
        return digest.hexdigest(), _identity(after)


def _same_snapshot(path: Path, identity) -> bool:
    try:
        return _identity(path.lstat()) == identity
    except FileNotFoundError:
        return False


def _recover_one(pending_dir, staged, dest, digest, spool_ops):
    # Hash outside flock. A concurrent publication may make this attempt stale;
    # defer to next tick rather than guessing or replacing the destination.
    try:
        existing = dest.exists() or dest.is_symlink()
        path = dest if existing else staged
        actual, identity = _hash_snapshot(path)
    except OSError:
        return False, False
    if actual != digest:
        return False, False
    with spool_ops._spool_lock(pending_dir):
        if not _same_snapshot(path, identity):
            return False, False
        if existing:
            return True, False
        if dest.exists() or dest.is_symlink():
            return False, False
        try:
            spool_ops._publish_staged_replay(staged, dest)
        except FileExistsError:
            return False, False
        return True, True


def _cursor_snapshot(pending_dir, spool_ops):
    with spool_ops._spool_lock(pending_dir):
        return _read_linked_cursor(pending_dir)


def _cursor_compare_and_set(pending_dir, spool_ops, expected, next_after):
    with spool_ops._spool_lock(pending_dir):
        if _read_linked_cursor(pending_dir) != expected:
            return False
        if expected[0] >= _MAX_CURSOR:
            raise ValueError("linked recovery cursor generation exhausted")
        _write_linked_cursor(pending_dir, expected[0] + 1, next_after)
        return True


async def _reserve_linked_recovery_batch(
    db: Any, pending_dir: Path, spool_ops: Any, size: int,
) -> list[Any]:
    """Reserve next SQL page with filesystem CAS, without awaits under flock.

    The cursor is advanced BEFORE filesystem publication: a crash can defer
    a candidate until the next wrap, but cannot publish the wrong replay.
    The generation prevents stale writers from moving the cursor backwards.
    """
    query = (
        "SELECT l.score_id, l.pending_name, l.payload_sha256, l.online_checksum "
        "FROM vanilla_replay_links AS l "
        "JOIN scores AS s ON s.id = l.score_id "
        "WHERE l.state=0 AND l.score_id > :after "
        "AND s.online_checksum = l.online_checksum "
        "AND s.mode IN (0,1,2,3) "
        "ORDER BY l.score_id ASC LIMIT :batch_size"
    )
    for _ in range(16):
        expected = await asyncio.to_thread(_cursor_snapshot, pending_dir, spool_ops)
        after = expected[1]
        rows = await db.fetch_all(query, {"after": after, "batch_size": size})
        if not rows and after != 0:
            # Round robin: retry older (potentially restored) files every cycle.
            rows = await db.fetch_all(query, {"after": 0, "batch_size": size})
        if not rows:
            return []
        next_after = int(rows[-1]["score_id"])
        if not 0 < next_after <= _MAX_CURSOR:
            raise ValueError("invalid recovery score id")
        if await asyncio.to_thread(
            _cursor_compare_and_set, pending_dir, spool_ops, expected, next_after
        ):
            return rows
    raise RuntimeError("linked recovery cursor contention; retry next tick")


async def recover_linked_vanilla_replays(
    db: Any, replay_dir: Path, *, limit: int = 25,
    spool_ops: Any = None,
) -> int:
    """Publish only explicitly linked replays with bounded FAIR scan.

    A durable round-robin cursor prevents stale/missing files at low score IDs
    from starving later scores. The cursor is shared by cooperating processes
    via the spool directory flock and survives process restarts.
    No unlink is performed on unrelated or unlinked legacy pending files.
    """
    if not 1 <= limit <= 200:
        raise ValueError("limit out of range")
    if spool_ops is None:
        from app.usecases import vanilla_replay as spool_ops

    replay_dir = Path(replay_dir)
    pending_dir = replay_dir / ".vanilla-pending"
    if not pending_dir.is_dir():
        return 0

    recovered = 0
    scanned = 0
    visited: set[int] = set()
    while recovered < limit and scanned < _MAX_SCANNED_PER_TICK:
        size = min(100, limit - recovered, _MAX_SCANNED_PER_TICK - scanned)
        rows = await _reserve_linked_recovery_batch(
            db, pending_dir, spool_ops, size
        )
        if not rows:
            break
        for row in rows:
            score_id = int(row["score_id"])
            if score_id in visited:
                # Finished one full scan cycle this tick. No tight retry loop.
                return recovered
            visited.add(score_id)
            scanned += 1
            name = str(row["pending_name"])
            checksum = str(row["online_checksum"])
            digest = str(row["payload_sha256"])
            if (
                score_id <= 0 or not _PENDING.fullmatch(name)
                or not _SHA256.fullmatch(digest) or not _CHECKSUM.fullmatch(checksum)
                or name.split(".", 1)[0] != checksum
            ):
                continue
            staged = pending_dir / name
            dest = replay_dir / f"{score_id}.osr"
            await_mark, published_now = await asyncio.to_thread(
                _recover_one, pending_dir, staged, dest, digest, spool_ops
            )
            # After releasing flock, an uncertain SQL ACK is safely retried.
            if await_mark:
                await db.execute(
                    "UPDATE vanilla_replay_links SET state=1 "
                    "WHERE score_id=:score_id AND state=0",
                    {"score_id": score_id},
                )
                if published_now:
                    recovered += 1
            if recovered >= limit:
                break
    return recovered




def finalize_linked_replay(staged: Any, score_id: int, intent: ReplayIntent,
                           *, spool_ops: Any = None) -> Path:
    """Post-COMMIT finalization: hash-check under same advisory spool lock.

    Failure leaves SQL link state=0 and staged file intact for recovery.
    """
    if spool_ops is None:
        from app.usecases import vanilla_replay as spool_ops
    if int(score_id) <= 0:
        raise ValueError("invalid score id")
    path = Path(staged.path)
    directory = Path(staged.replay_dir)
    if (path.parent != directory / ".vanilla-pending"
            or path.name != intent.pending_name
            or path.name.split(".", 1)[0] != intent.online_checksum):
        raise ValueError("invalid linked replay source")
    destination = directory / f"{int(score_id)}.osr"
    with spool_ops._spool_lock(path.parent):
        if path.is_symlink() or not path.is_file() or _file_sha256(path) != intent.payload_sha256:
            raise OSError("staged replay sha256 mismatch or file missing")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"replay already exists: {destination.name}")
        spool_ops._publish_staged_replay(path, destination)
    return destination
