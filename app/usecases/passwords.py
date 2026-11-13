"""Password checks shared by the game login and the /web/ endpoints.

bcrypt is slow on purpose (~200 ms). Called directly it blocks the whole
server for that long, and nothing stopped a client from asking for it
thousands of times with wrong passwords. Everything that needs to check a
password goes through `verify()`, which:

  - answers from memory for a pair that was already verified (as before),
  - answers from memory for a pair that failed in the last few minutes
    (a client retrying an old saved password costs nothing),
  - allows only a few failed checks per account from one address in a
    time window, and a higher number per account from all addresses
    together (password guessing from many addresses). the game login and
    the /web/ endpoints count separately, so wrong passwords sent to
    /web/ can never lock anyone out of logging in,
  - runs bcrypt in a worker thread, a few at a time, so the server keeps
    answering other players meanwhile.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
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

# failed checks allowed in a window; a pair that already failed and is
# still remembered does not count again
FAIL_WINDOW = 10 * 60  # seconds
ADDRESS_FAIL_LIMIT = 10  # per (account, address)
ACCOUNT_FAIL_LIMIT = 50  # per account, all addresses together
FAIL_COUNTERS_MAX = 20_000

# which counters a check uses; they never mix
SCOPE_LOGIN = "login"  # game login
SCOPE_WEB = "web"  # /web/ endpoints (score submission, leaderboards, ...)


class _Counter:
    __slots__ = ("times", "logged")

    def __init__(self) -> None:
        self.times: list[float] = []  # failure times inside the window
        self.logged = False  # the "limit reached" line was already written


_failed_pairs: dict[bytes, float] = {}  # {pair key: expires at}
_fail_counters: dict[tuple[str, bytes, str], _Counter] = {}  # {(scope, account, address): ...}
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
    if len(_fail_counters) >= FAIL_COUNTERS_MAX:
        cutoff = now - FAIL_WINDOW
        for ckey in [k for k, c in _fail_counters.items() if not c.times or c.times[-1] <= cutoff]:
            del _fail_counters[ckey]
        if len(_fail_counters) >= FAIL_COUNTERS_MAX:
            _fail_counters.clear()


def _address_bucket(ip: str | None) -> str:
    """The address the per-address limit counts by.

    A single IPv6 customer usually holds a whole /64, so those count as one.
    """
    if not ip:
        return "unknown"
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


def _limit_reached(
    ckey: tuple[str, bytes, str],
    limit: int,
    now: float,
    account_name: str,
    address: str,
) -> bool:
    """Whether the counter is at its limit; writes one log line per window."""
    counter = _fail_counters.get(ckey)
    if counter is None:
        return False

    cutoff = now - FAIL_WINDOW
    counter.times[:] = [t for t in counter.times if t > cutoff]
    if not counter.times:
        del _fail_counters[ckey]
        return False

    if len(counter.times) < limit:
        return False

    if not counter.logged:
        counter.logged = True
        where = f"from {address}" if ckey[2] else "from all addresses together"
        log(
            f"Password check limit reached ({ckey[0]}): account {account_name!r} "
            f"{where}, {len(counter.times)} wrong passwords in "
            f"{FAIL_WINDOW // 60} min (last address: {address}). "
            "Refusing further attempts until the window passes.",
            Ansi.LYELLOW,
        )
    return True


async def verify(
    trusted_hashword: bytes,
    untrusted_password: bytes,
    *,
    also_double_md5: bool = False,
    scope: str = SCOPE_LOGIN,
    ip: str | None = None,
    account_name: str = "?",
) -> bool:
    """Whether `untrusted_password` matches the account's bcrypt hash.

    `scope` picks the set of failure counters, `ip` is the client address
    (None when the caller does not know it; such checks share one counter),
    `account_name` is only used in the log.
    """
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

    # too many wrong passwords lately -> refuse without spending cpu.
    # a pair that is already verified (cached above) is never affected.
    account = hashlib.sha256(trusted_hashword).digest()
    address = _address_bucket(ip)
    address_key = (scope, account, address)
    account_key = (scope, account, "")
    if _limit_reached(address_key, ADDRESS_FAIL_LIMIT, now, account_name, address):
        return False
    if _limit_reached(account_key, ACCOUNT_FAIL_LIMIT, now, account_name, address):
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
        # the right password clears this address; the account-wide counter
        # stays, so one success elsewhere does not reset a spread-out guess
        _fail_counters.pop(address_key, None)
        return True

    _prune(now)
    _failed_pairs[key] = now + FAILED_PAIR_TTL
    for ckey in (address_key, account_key):
        counter = _fail_counters.get(ckey)
        if counter is None:
            counter = _fail_counters[ckey] = _Counter()
        counter.times.append(now)
    return False


async def hash_new(password: bytes) -> bytes:
    """bcrypt a new password without blocking the server."""
    async with _semaphore:
        return await asyncio.to_thread(bcrypt.hashpw, password, bcrypt.gensalt())
