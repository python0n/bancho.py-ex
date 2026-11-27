"""Records what the osu! client sends for anticheat, without changing anything.

Two channels:
- the `token` header on every score submission (encrypted by osu!auth; we only
  describe it from the outside: size, base64, entropy, SHA-256, repeats),
- /web/lastfm.php calls (the `b` parameter can carry old anticheat flags).

Both go to daily JSON-line logs in .data/logs (owner-only, kept 30 days).
The player's password hash is never written. A failure here never breaks
the request itself.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import logging.handlers
import math
import os
import time
from collections import Counter
from typing import Any

import orjson

import app.state
from app.logging import Ansi
from app.logging import log

LOGS_DIR = ".data/logs"
KEEP_DAYS = 30
REPEAT_KEY = "tsg:ac_token:{}"
REPEAT_TTL = KEEP_DAYS * 24 * 3600
# a client playing on a private server sends a short fixed value instead of a
# real osu!auth token; such a placeholder is written as is and never checked
# for repeats (it is the same for every score)
PLACEHOLDER_MAX_LEN = 32

# raw tokens contain (encrypted) hardware hashes of players, so off by default
KEEP_RAW = os.environ.get("AC_TOKEN_KEEP_RAW", "false").strip().lower() in ("1", "true", "yes")
# experiment: answer plain lastfm.php calls with an empty body instead of -3
LASTFM_NO_STOP = os.environ.get("LASTFM_NO_STOP", "false").strip().lower() in ("1", "true", "yes")

# query parameters that are never written
SECRET_PARAMS = {"us", "ha", "u", "h", "p", "pass", "password"}


class _OwnerOnlyDailyHandler(logging.handlers.TimedRotatingFileHandler):
    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


_loggers: dict[str, logging.Logger] = {}


def _write(name: str, record: dict[str, Any]) -> None:
    try:
        lg = _loggers.get(name)
        if lg is None:
            os.makedirs(LOGS_DIR, exist_ok=True)
            handler = _OwnerOnlyDailyHandler(
                os.path.join(LOGS_DIR, name),
                when="midnight",
                backupCount=KEEP_DAYS,
                encoding="utf-8",
                utc=True,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            lg = logging.getLogger(f"bancho.trace.{name}")
            lg.setLevel(logging.INFO)
            lg.propagate = False
            lg.addHandler(handler)
            _loggers[name] = lg
        lg.info(orjson.dumps(record, default=str).decode())
    except Exception as exc:  # never break the request
        log(f"Could not write {name}: {exc!r}", Ansi.LRED)


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    n = len(data)
    return round(-sum(c / n * math.log2(c / n) for c in Counter(data).values()), 4)


def _player_state(player: Any) -> dict[str, Any]:
    try:
        st = player.status
        return {
            "action": int(st.action),
            "map_id": st.map_id,
            "map_md5": st.map_md5,
            "mods": int(st.mods),
        }
    except Exception:
        return {}


async def trace_token(player: Any, bmap: Any, osu_version: str, token: str | None, score_checksum: str) -> None:
    """Describe the score token and warn when the same token comes with another score."""
    try:
        raw = token or ""
        try:
            decoded = base64.b64decode(raw, validate=True)
            b64_ok = True
        except (binascii.Error, ValueError):
            decoded = raw.encode("utf-8", "replace")
            b64_ok = False
        sha = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
        placeholder = len(raw) <= PLACEHOLDER_MAX_LEN and not (b64_ok and len(decoded) >= 16)

        repeated_with = None
        if raw and not placeholder:
            key = REPEAT_KEY.format(sha)
            redis = app.state.services.redis
            first = await redis.set(key, score_checksum, nx=True, ex=REPEAT_TTL)
            if not first:
                prev = await redis.get(key)
                if isinstance(prev, bytes):
                    prev = prev.decode("utf-8", "replace")
                # the same score sent again after an error is not suspicious
                if prev and prev != score_checksum:
                    repeated_with = prev
                    log(
                        f"{player} sent a score token already used with another score ({sha[:12]})",
                        Ansi.LRED,
                    )

        record = {
            "t": int(time.time()),
            "user_id": player.id,
            "name": player.name,
            "map_id": getattr(bmap, "id", None),
            "map_md5": getattr(bmap, "md5", None),
            "score_checksum": score_checksum,
            "osu_version": osu_version,
            "present": bool(raw),
            "length": len(raw),
            "base64": b64_ok,
            "decoded_size": len(decoded) if b64_ok else None,
            "decoded_mod16": (len(decoded) % 16) if b64_ok else None,
            "entropy": _entropy(decoded),
            "sha256": sha,
            "placeholder": placeholder,
            # a placeholder is a fixed value with nothing inside, safe to keep
            "value": raw if placeholder else None,
            "repeated_with": repeated_with,
        }
        _write("ac_token.log", record)
        if KEEP_RAW and raw:
            _write("ac_token_raw.log", {"t": record["t"], "user_id": player.id, "sha256": sha, "token": raw})
    except Exception as exc:
        log(f"Score token trace failed: {exc!r}", Ansi.LRED)


def trace_lastfm(player: Any, query: Any, flags: int | None, response: bytes) -> None:
    try:
        params = {k: v for k, v in query.items() if k.lower() not in SECRET_PARAMS}
        record: dict[str, Any] = {
            "t": int(time.time()),
            "user_id": player.id,
            "name": player.name,
            "params": params,
            "state": _player_state(player),
            "response": response.decode("utf-8", "replace"),
        }
        if flags is not None:
            try:
                from app.constants.clientflags import LastFMFlags

                known = 0
                names = []
                for f in LastFMFlags:
                    if f.value and flags & f.value:
                        names.append(f.name)
                        known |= f.value
                record["flags"] = flags
                record["flag_names"] = names
                record["unknown_bits"] = [b for b in range(64) if (flags & ~known) >> b & 1]
            except Exception:
                record["flags"] = flags
        try:
            record["osu_version"] = str(player.client_details.osu_version.date.strftime("%Y%m%d"))
        except Exception:
            pass
        _write("lastfm.log", record)
    except Exception as exc:
        log(f"lastfm trace failed: {exc!r}", Ansi.LRED)
