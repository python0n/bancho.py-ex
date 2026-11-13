"""Password checks shared by the game login and the /web/ endpoints.

bcrypt is slow on purpose (~200 ms). Called directly it blocks the whole
server for that long, and nothing stopped a client from asking for it
thousands of times with wrong passwords. Everything that needs to check a
password goes through `verify()`, which:

  - answers from memory for a pair that was already verified (as before),
  - answers from memory for a pair that failed in the last few minutes
    (a client retrying an old saved password costs nothing),
  - allows only a few failed checks per account in a time window
    (password guessing, no matter from how many addresses),
  - runs bcrypt in a worker thread, a few at a time, so the server keeps
    answering other players meanwhile.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import bcrypt

import app.state
from app.logging import Ansi
from app.logging import log

# how many bcrypt checks may run at once, and how many may wait for a turn
MAX_PARALLEL_CHECKS = 2
MAX_WAITING_CHECKS = 16

# how long a failed (hash, password) pair is remembered
FAILED_PAIR_TTL = 10 * 60  # seconds
FAILED_PAIR_MAX = 20_000

# failed checks allowed per account in a window; a pair that already failed
# and is still remembered does not count again
ACCOUNT_FAIL_LIMIT = 10
ACCOUNT_FAIL_WINDOW = 10 * 60  # seconds
ACCOUNT_FAIL_MAX = 20_000

_failed_pairs: dict[bytes, float] = {}  # {pair key: expires at}
_account_fails: dict[bytes, list[float]] = {}  # {account key: [failure times]}
_waiting = 0
_last_overload_log = 0.0
_semaphore = asyncio.Semaphore(MAX_PARALLEL_CHECKS)


def pair_key(trusted_hashword: bytes, untrusted_password: bytes) -> bytes:
    """Cache key for a (bcrypt hash, password) pair; no password is stored.

    When the bcrypt hash changes (password reset), old entries can no
    longer be reached.
    """
    return hashlib.sha256(trusted_hashword + b":" + untrusted_password).digest()


def remember_verified(trusted_hashword: bytes, password: bytes) -> None:
    """Mark a pair as verified (used right after registration)."""
    app.state.cache.bcrypt[pair_key(trusted_hashword, password)] = True


def _check(trusted_hashword: bytes, password: bytes, also_double_md5: bool) -> bool:
    try:
        if bcrypt.checkpw(password, trusted_hashword):
            return True
        if also_double_md5:
            double = hashlib.md5(password).hexdigest().encode()
            return bcrypt.checkpw(double, trusted_hashword)
    except ValueError:
        # malformed hash in the database
        return False
    return False


def _prune(now: float) -> None:
    if len(_failed_pairs) >= FAILED_PAIR_MAX:
        for key in [k for k, expires in _failed_pairs.items() if expires <= now]:
            del _failed_pairs[key]
        if len(_failed_pairs) >= FAILED_PAIR_MAX:
            _failed_pairs.clear()
    if len(_account_fails) >= ACCOUNT_FAIL_MAX:
        cutoff = now - ACCOUNT_FAIL_WINDOW
        for key in [k for k, times in _account_fails.items() if times[-1] <= cutoff]:
            del _account_fails[key]
        if len(_account_fails) >= ACCOUNT_FAIL_MAX:
            _account_fails.clear()


async def verify(
    trusted_hashword: bytes,
    untrusted_password: bytes,
    *,
    also_double_md5: bool = False,
) -> bool:
    """Whether `untrusted_password` matches the account's bcrypt hash."""
    global _waiting

    key = pair_key(trusted_hashword, untrusted_password)
    if key in app.state.cache.bcrypt:  # already verified pair
        return True

    now = time.monotonic()

    expires = _failed_pairs.get(key)
    if expires is not None:
        if expires > now:
            return False
        del _failed_pairs[key]

    account = hashlib.sha256(trusted_hashword).digest()
    fails = _account_fails.get(account)
    if fails is not None:
        cutoff = now - ACCOUNT_FAIL_WINDOW
        fails[:] = [t for t in fails if t > cutoff]
        if not fails:
            del _account_fails[account]
        elif len(fails) >= ACCOUNT_FAIL_LIMIT:
            # too many wrong passwords for this account lately; refuse
            # without spending cpu. a session that is already verified
            # (cached above) is not affected.
            return False

    if _waiting >= MAX_WAITING_CHECKS:
        global _last_overload_log
        if now - _last_overload_log > 10:  # do not flood the log under load
            _last_overload_log = now
            log("Password checks refused: too many waiting.", Ansi.LYELLOW)
        return False

    _waiting += 1
    try:
        async with _semaphore:
            ok = await asyncio.to_thread(
                _check,
                trusted_hashword,
                untrusted_password,
                also_double_md5,
            )
    finally:
        _waiting -= 1

    now = time.monotonic()
    if ok:
        app.state.cache.bcrypt[key] = True
        _account_fails.pop(account, None)
        return True

    _prune(now)
    _failed_pairs[key] = now + FAILED_PAIR_TTL
    _account_fails.setdefault(account, []).append(now)
    return False


async def hash_new(password: bytes) -> bytes:
    """bcrypt a new password without blocking the server."""
    async with _semaphore:
        return await asyncio.to_thread(bcrypt.hashpw, password, bcrypt.gensalt())
