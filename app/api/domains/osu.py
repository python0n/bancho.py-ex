"""osu: handle connections from web, api, and beyond?"""

import re

import orjson
import asyncio
import copy
from datetime import datetime, timezone
import base64
import hashlib
import os
import random
import secrets
import math
from collections import defaultdict
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from enum import IntEnum
from enum import unique
from functools import cache
from pathlib import Path as SystemPath
from typing import Any
from typing import Literal
from urllib.parse import unquote
from urllib.parse import unquote_plus

import bcrypt
from app.api.v2.common import json
from app.discord import Embed, Webhook
import app.metrics
from fastapi import status
from fastapi.datastructures import FormData
from fastapi.datastructures import UploadFile
from fastapi.exceptions import HTTPException
from fastapi.param_functions import Depends
from fastapi.param_functions import File
from fastapi.param_functions import Form
from fastapi.param_functions import Header
from fastapi.param_functions import Path
from fastapi.param_functions import Query
from fastapi.requests import Request
from fastapi.responses import FileResponse
from fastapi.responses import ORJSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.routing import APIRouter
from starlette.datastructures import UploadFile as StarletteUploadFile
from fastapi import Form, Request

import app.packets
import app.settings
import app.state
import app.utils
from app import encryption
from app._typing import UNSET
from app.constants import regexes
from app.constants.clientflags import LastFMFlags
from app.constants.gamemodes import GameMode
from app.constants.mods import Mods
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log
from app.objects import models
from app.objects.beatmap import Beatmap
from app.objects.beatmap import RankedStatus
from app.objects.beatmap import ensure_osu_file_is_available
from app.objects.player import Player
from app.objects.score import Grade
from app.objects.score import Score
from app.objects.score import SubmissionStatus
from app.repositories import clans as clans_repo
from app.repositories import comments as comments_repo
from app.repositories import favourites as favourites_repo
from app.repositories import mail as mail_repo
from app.repositories import maps as maps_repo
from app.repositories.maps import MapsTable
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from app.repositories import ratings as ratings_repo
from app.repositories import scores as scores_repo
from app.repositories import stats as stats_repo
from app.repositories import users as users_repo
from app.repositories.achievements import Achievement
from app.usecases import achievements as achievements_usecases
from app.usecases import user_achievements as user_achievements_usecases
from app.utils import escape_enum
from app.utils import pymysql_encode

BEATMAPS_PATH = SystemPath.cwd() / ".data/osu"
REPLAYS_PATH = SystemPath.cwd() / ".data/osr"
SCREENSHOTS_PATH = SystemPath.cwd() / ".data/ss"
SUBMISSIONS_PATH = SystemPath.cwd() / ".data/submissions"

# Ensure submissions directory exists
SUBMISSIONS_PATH.mkdir(parents=True, exist_ok=True)

# Private server beatmap IDs start from 2^30 to avoid conflicts
# with official osu! beatmap IDs. The client requires positive i32 IDs.
BSS_ID_OFFSET = 1_073_741_824

file_path = ".config/caps.json"

if not os.path.exists(".config"):
    os.makedirs(".config")

# Default data
default_data = {
    "enabled": False,
    "caps": {
        "0": 800,  # vn!std
        "4": 1400, # rx!std
        "8": 600   # ap!std
    }
}

# Ensure the file exists and is not empty
if not os.path.exists(file_path) or os.stat(file_path).st_size == 0:
    with open(file_path, "wb") as f:
        f.write(orjson.dumps(default_data))

# Load function with error handling
def load_json(file_path: str):
    try:
        with open(file_path, "rb") as f:
            return orjson.loads(f.read())
    except orjson.JSONDecodeError:
        # Reset file if JSON is invalid
        with open(file_path, "wb") as f:
            f.write(orjson.dumps(default_data))
        return default_data

capData = load_json(file_path)

# ---------------------------------------------------------------------------
# Score submission diagnostics & normalization
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y", "on"}

# Enable extra diagnostics for score submits (writes to .data/logs/strange_*.db).
DIAG_SCORE_SUBMIT = _env_flag("DIAG_SCORE_SUBMIT", "0")

# Normalize client hashes for comparison only (do NOT use for checksum calculation).
SANITIZE_CLIENT_HASH = _env_flag("SANITIZE_CLIENT_HASH", "1")


def _coerce_to_str(v: object) -> str:
    if isinstance(v, (bytes, bytearray)):
        return v.decode(errors="ignore")
    return str(v)


def _normalize_client_hash_for_compare(v: object) -> str:
    # Some clients (notably Wine builds) can send small formatting differences.
    # We normalize for *comparison only*; checksum calculation must use the raw value.
    s = _coerce_to_str(v).strip()
    s = s.strip("'").strip('"').strip()

    # Some builds append one or more trailing colons.
    while s.endswith(":"):
        s = s[:-1]

    parts = s.split(":")
    out_parts: list[str] = []
    for p in parts:
        p_stripped = p.strip()
        pl = p_stripped.lower()
        pl2 = pl.rstrip(".")  # e.g. "runningunderwin." -> "runningunderwin"
        if pl2 in ("runningunderwin", "runningunderwine"):
            out_parts.append("runningunderwine")
        else:
            out_parts.append(p_stripped)

    return ":".join(out_parts)


def _redact(s: object, keep: int = 8) -> str:
    v = _coerce_to_str(s)
    if len(v) <= keep * 2:
        return "<redacted>"
    return f"{v[:keep]}...{v[-keep:]}"





def _is_ranked_for_first_places(status: Any) -> bool:
    """Return True if a beatmap status should count towards 'first places'.

    On this server, beatmaps.status uses:
      0 = unranked (hide from first places)
      2 = ranked, 3 = approved, 4 = qualified, 5 = loved (keep)
    """
    try:
        v = int(status)  # int(Enum) works too
    except Exception:
        v = getattr(status, "value", None)
        try:
            v = int(v)
        except Exception:
            return False

    return v in (2, 3, 4, 5)

def _fmt_mods_for_announce(mods: Any) -> str:
    """Format mod bitmask for chat/webhook announcements.

    Key rule: if NC is present, do not show DT, and keep NC before RX.
    """
    raw = f"{mods!r}"

    # Extract 2-char mod tokens (HD, HR, DT, NC, RX, etc.).
    # This stays compatible with bancho.py's custom Mods.__repr__ output.
    tokens = re.findall(r"[A-Z0-9]{2}", raw)
    if not tokens:
        return raw

    # NC implies DT; PF implies SD.
    if "NC" in tokens and "DT" in tokens:
        tokens = [t for t in tokens if t != "DT"]
    if "PF" in tokens and "SD" in tokens:
        tokens = [t for t in tokens if t != "SD"]

    # De-duplicate while preserving first occurrence.
    uniq: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t not in seen:
            uniq.append(t)
            seen.add(t)

    # Preferred display order (add more if you care about other mods).
    order: dict[str, int] = {
        "NF": 10,
        "EZ": 20,
        "TD": 30,
        "HD": 40,
        "HR": 50,
        "SD": 60,
        "PF": 61,
        "DT": 70,
        "NC": 71,
        "HT": 72,
        "FL": 80,
        "SO": 90,
        # custom serverside mods / special
        "RX": 100,
        "AP": 110,
        "FI": 120,
        "V2": 130,
        # mania keys (kept at the end unless you add ordering)
        "1K": 200,
        "2K": 201,
        "3K": 202,
        "4K": 203,
        "5K": 204,
        "6K": 205,
        "7K": 206,
        "8K": 207,
        "9K": 208,
        "CO": 209,
    }

    uniq.sort(key=lambda t: order.get(t, 1000))
    return "".join(uniq)

router = APIRouter(
    tags=["osu! web API"],
    default_response_class=Response,
)


@cache
def authenticate_player_session(
    param_function: Callable[..., Any],
    username_alias: str = "u",
    pw_md5_alias: str = "p",
    err: Any | None = None,
) -> Callable[[str, str], Awaitable[Player]]:
    async def wrapper(
        username: str = param_function(..., alias=username_alias),
        pw_md5: str = param_function(..., alias=pw_md5_alias),
    ) -> Player:
        player = await app.state.sessions.players.from_login(
            name=unquote(username),
            pw_md5=pw_md5,
        )
        if player:
            return player

        # player login incorrect
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=err,
        )

    return wrapper


""" /web/ handlers """

# Previously unhandled BSS endpoints — now implemented:
# POST /web/osu-osz2-bmsubmit-post.php
# POST /web/osu-osz2-bmsubmit-upload.php
# GET /web/osu-osz2-bmsubmit-getid.php
# GET /web/osu-get-beatmap-topic.php

# Remaining unhandled:
# POST /web/osu-error.php
# POST /web/osu-session.php


# ===================== BSS Helper Functions =====================

async def _bss_get_next_set_id() -> int:
    """Get the next available beatmapset ID for BSS."""
    stmt = sa_select(sa_func.max(MapsTable.set_id)).where(
        MapsTable.set_id >= BSS_ID_OFFSET,
    )
    result = await app.state.services.database.fetch_val(stmt)
    if result is None:
        return BSS_ID_OFFSET
    return result + 1


async def _bss_get_next_beatmap_ids(count: int) -> list[int]:
    """Get the next N available beatmap IDs for BSS."""
    stmt = sa_select(sa_func.max(MapsTable.id)).where(
        MapsTable.id >= BSS_ID_OFFSET,
    )
    result = await app.state.services.database.fetch_val(stmt)
    start_id = BSS_ID_OFFSET if result is None else result + 1
    return list(range(start_id, start_id + count))


def _parse_osu_file_metadata(content: bytes) -> dict[str, Any]:
    """Parse a .osu file and extract metadata."""
    metadata: dict[str, Any] = {
        "artist": "", "title": "", "version": "", "creator": "",
        "mode": 0, "bpm": 0.0, "cs": 0.0, "ar": 0.0, "od": 0.0,
        "hp": 0.0, "total_length": 0,
    }

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except Exception:
            return metadata

    section = ""
    timing_points: list[float] = []
    hit_objects_times: list[int] = []

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue

        if section == "General":
            if line.startswith("Mode:"):
                try:
                    metadata["mode"] = int(line.split(":")[1].strip())
                except (ValueError, IndexError):
                    pass

        elif section == "Metadata":
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if key == "Artist":
                    metadata["artist"] = value[:128]
                elif key == "ArtistUnicode" and not metadata["artist"]:
                    metadata["artist"] = value[:128]
                elif key == "Title":
                    metadata["title"] = value[:128]
                elif key == "TitleUnicode" and not metadata["title"]:
                    metadata["title"] = value[:128]
                elif key == "Version":
                    metadata["version"] = value[:128]
                elif key == "Creator":
                    metadata["creator"] = value[:19]

        elif section == "Difficulty":
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                try:
                    val = float(value)
                except ValueError:
                    continue
                if key == "CircleSize":
                    metadata["cs"] = val
                elif key == "OverallDifficulty":
                    metadata["od"] = val
                elif key == "ApproachRate":
                    metadata["ar"] = val
                elif key == "HPDrainRate":
                    metadata["hp"] = val

        elif section == "TimingPoints":
            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    beat_length = float(parts[1])
                    if beat_length > 0:  # uninherited timing point
                        timing_points.append(beat_length)
                except (ValueError, IndexError):
                    pass

        elif section == "HitObjects":
            parts = line.split(",")
            if len(parts) >= 3:
                try:
                    hit_objects_times.append(int(parts[2]))
                except (ValueError, IndexError):
                    pass

    if timing_points:
        if timing_points[0] > 0:
            metadata["bpm"] = round(60000.0 / timing_points[0], 2)

    if hit_objects_times:
        first_time = min(hit_objects_times)
        last_time = max(hit_objects_times)
        metadata["total_length"] = max(0, (last_time - first_time) // 1000)

    return metadata


def _bss_try_extract_zip(data: bytes, output_dir: SystemPath) -> list[tuple[str, bytes]]:
    """Try to extract .osu files from a zip/osz/osz2 file."""
    import zipfile
    import io

    osu_files: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith(".osu"):
                    file_data = zf.read(name)
                    safe = name.replace("/", "_").replace("\\", "_")
                    (output_dir / safe).write_bytes(file_data)
                    osu_files.append((name, file_data))
                elif not name.endswith("/"):
                    safe = name.replace("/", "_").replace("\\", "_")
                    (output_dir / safe).write_bytes(zf.read(name))
            if osu_files:
                log(f"[BSS] extracted {len(osu_files)} .osu files from archive", Ansi.LCYAN)
    except Exception:
        pass  # not a valid zip — encrypted osz2 or other format
    return osu_files


def _read_uleb128(data: bytes, offset: int) -> tuple[int, int]:
    """Read a ULEB128-encoded integer from data at offset."""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
        if shift > 35:  # safety limit
            break
    return result, offset


def _read_osu_string(data: bytes, offset: int) -> tuple[str, int]:
    """Read an osu! binary protocol string (0x00=null, 0x0B=string)."""
    if offset >= len(data):
        return "", offset
    marker = data[offset]
    offset += 1
    if marker == 0x00:
        return "", offset
    elif marker == 0x0B:
        length, offset = _read_uleb128(data, offset)
        if length > 0 and offset + length <= len(data):
            try:
                s = data[offset:offset + length].decode("utf-8", errors="replace")
            except Exception:
                s = ""
            offset += length
            return s, offset
        else:
            offset += min(length, len(data) - offset)
            return "", offset
    else:
        # Unknown marker — not a valid string field
        return "", offset - 1  # back up, might be a different field type


def _parse_osz2_metadata(data: bytes) -> dict[str, Any]:
    """Parse metadata from .osz2 file header.

    The .osz2 format stores metadata strings in the header using osu!'s
    binary protocol (0x0B + ULEB128 length + UTF-8 data).

    We try multiple parsing strategies since the format varies by version.
    """
    metadata: dict[str, Any] = {
        "artist": "", "title": "", "version": "", "creator": "",
        "mode": 0, "bpm": 0.0, "cs": 0.0, "ar": 0.0, "od": 0.0,
        "hp": 0.0, "total_length": 0, "filenames": [],
    }

    if len(data) < 20:
        return metadata

    # Strategy 1: Try to parse the osz2 header structure
    # The format typically starts with a few header bytes, then
    # contains osu! binary strings for metadata fields.
    try:
        offset = 0

        # Skip magic/version bytes (usually 2-4 bytes)
        # Look for the first 0x0B marker which starts a string
        scan_limit = min(50, len(data))
        first_string_offset = -1
        for i in range(scan_limit):
            if data[i] == 0x0B:
                # Verify it looks like a valid string
                test_len, test_off = _read_uleb128(data, i + 1)
                if 0 < test_len < 500 and test_off + test_len <= len(data):
                    first_string_offset = i
                    break

        if first_string_offset >= 0:
            offset = first_string_offset

            # Read strings in the typical osz2 order:
            # hash, artist, artist_unicode, title, title_unicode,
            # creator, version
            strings_read: list[str] = []
            for _ in range(10):  # read up to 10 strings
                if offset >= len(data) or offset > 4096:
                    break
                s, new_offset = _read_osu_string(data, offset)
                if new_offset == offset:
                    # Didn't advance — try skipping a byte
                    offset += 1
                    continue
                offset = new_offset
                strings_read.append(s)

            if len(strings_read) >= 6:
                # Typical order: hash, artist, artist_unicode,
                # title, title_unicode, creator, version
                metadata["artist"] = strings_read[1] or strings_read[2] or ""
                metadata["title"] = strings_read[3] or strings_read[4] or ""
                metadata["creator"] = strings_read[5] if len(strings_read) > 5 else ""
                metadata["version"] = strings_read[6] if len(strings_read) > 6 else ""
                log(
                    f"[BSS] osz2 header parsed: "
                    f"'{metadata['artist']} - {metadata['title']} [{metadata['version']}]' "
                    f"by {metadata['creator']}",
                    Ansi.LCYAN,
                )
    except Exception as e:
        log(f"[BSS] osz2 header parse error: {e}", Ansi.LYELLOW)

    # Strategy 2: If header parsing didn't work, scan for readable metadata
    if not metadata["artist"] and not metadata["title"]:
        try:
            # Scan for .osu filenames in the binary (they appear as strings)
            # Pattern: "Artist - Title (Creator) [Version].osu"
            import re
            text_chunks = data[:min(len(data), 65536)]
            # Look for .osu filename patterns in the binary
            osu_pattern = rb'([^\x00\x0B]{3,80}\s*-\s*[^\x00\x0B]{3,80}\s*\([^\x00\x0B]{2,30}\)\s*\[[^\x00\x0B]{1,60}\]\.osu)'
            matches = re.findall(osu_pattern, text_chunks)
            for match in matches:
                try:
                    fn = match.decode("utf-8", errors="replace")
                    metadata["filenames"].append(fn)
                    # Parse: "Artist - Title (Creator) [Version].osu"
                    fn_match = re.match(
                        r'(.+?)\s*-\s*(.+?)\s*\((.+?)\)\s*\[(.+?)\]\.osu',
                        fn,
                    )
                    if fn_match:
                        metadata["artist"] = fn_match.group(1).strip()[:128]
                        metadata["title"] = fn_match.group(2).strip()[:128]
                        metadata["creator"] = fn_match.group(3).strip()[:19]
                        metadata["version"] = fn_match.group(4).strip()[:128]
                        log(f"[BSS] osz2 filename parsed: '{fn}'", Ansi.LCYAN)
                except Exception:
                    pass
        except Exception as e:
            log(f"[BSS] osz2 binary scan error: {e}", Ansi.LYELLOW)

    return metadata


OSZ2_SERVICE_URL = os.environ.get("OSZ2_SERVICE_URL", "http://osz2-service:80")


async def _bss_decrypt_osz2(osz2_data: bytes) -> dict | None:
    """Send .osz2 to osz2-service for decryption, return extracted files.

    Returns dict with keys: metadata, beatmaps, files
    where files is {filename: base64_encoded_bytes}
    Returns None on failure.
    """
    import aiohttp

    url = f"{OSZ2_SERVICE_URL}/osz2/decrypt"

    try:
        form = aiohttp.FormData()
        form.add_field(
            "osz2", osz2_data,
            filename="upload.osz2",
            content_type="application/octet-stream",
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    log(f"[BSS] osz2-service returned {resp.status}: {error_text}", Ansi.LRED)
                    return None

                result = await resp.json()
                file_count = len(result.get("files", {}))
                osu_count = sum(1 for f in result.get("files", {}) if f.endswith(".osu"))
                log(
                    f"[BSS] osz2-service decrypted: {file_count} files ({osu_count} .osu)",
                    Ansi.LGREEN,
                )
                return result
    except Exception as e:
        log(f"[BSS] osz2-service error: {e}", Ansi.LRED)
        return None


async def _bss_create_entry_from_osz2(
    data: bytes,
    set_id: int,
    beatmap_id: int,
    player: Player,
) -> bool:
    """Create a database entry from .osz2 metadata when we can't extract .osu files."""
    meta = _parse_osz2_metadata(data)

    # Generate a pseudo-md5 from the osz2 data for uniqueness
    osz2_md5 = hashlib.md5(data).hexdigest()

    artist = meta["artist"] or "Unknown Artist"
    title = meta["title"] or "Unknown Title"
    version = meta["version"] or "Normal"
    creator = meta["creator"] or player.name

    osu_filename = (
        f"{artist} - {title} ({creator}) [{version}].osu"
    ).replace("/", "_").replace("\\", "_")

    log(
        f"[BSS] creating entry from osz2: id={beatmap_id} set={set_id} "
        f"'{artist} - {title} [{version}]' by {creator}",
        Ansi.LCYAN,
    )

    try:
        # Create mapsets entry (required for BeatmapSet.from_bsid to find the map)
        # Set last_osuapi_check far in the future so bancho-py won't try to
        # verify/update this map against the official osu!api.
        await app.state.services.database.execute(
            "REPLACE INTO mapsets (id, server, last_osuapi_check) "
            "VALUES (:id, :server, :last_osuapi_check)",
            {
                "id": set_id,
                "server": "osu!",
                "last_osuapi_check": datetime(2099, 1, 1),
            },
        )

        await maps_repo.create(
            id=beatmap_id,
            server="osu!",
            set_id=set_id,
            status=0,  # Pending
            md5=osz2_md5,
            artist=artist[:128],
            title=title[:128],
            version=version[:128],
            creator=creator[:19],
            filename=osu_filename[:256],
            last_update=datetime.now(tz=timezone.utc),
            total_length=meta["total_length"],
            max_combo=0,
            frozen=True,  # frozen so osu!api doesn't overwrite status
            plays=0,
            passes=0,
            mode=meta["mode"],
            bpm=meta["bpm"],
            cs=meta["cs"],
            ar=meta["ar"],
            od=meta["od"],
            hp=meta["hp"],
            diff=0.0,
        )
        log(f"[BSS] DB entry created for map {beatmap_id}!", Ansi.LGREEN)
        return True
    except Exception as e:
        log(f"[BSS] failed to create DB entry: {e}", Ansi.LRED)
        return False


async def _bss_process_osu_files(
    osu_files: list[tuple[str, bytes]],
    set_id: int,
    player: Player,
) -> None:
    """Process .osu files: parse metadata and insert/update database records."""

    # Ensure mapsets entry exists (required for BeatmapSet.from_bsid)
    try:
        await app.state.services.database.execute(
            "REPLACE INTO mapsets (id, server, last_osuapi_check) "
            "VALUES (:id, :server, :last_osuapi_check)",
            {
                "id": set_id,
                "server": "osu!",
                "last_osuapi_check": datetime(2099, 1, 1),
            },
        )
    except Exception as e:
        log(f"[BSS] failed to create mapsets entry: {e}", Ansi.LRED)

    for filename, file_data in osu_files:
        md5 = hashlib.md5(file_data).hexdigest()
        meta = _parse_osu_file_metadata(file_data)

        # Check if this map already exists (by md5)
        existing = await maps_repo.fetch_one(md5=md5)
        if existing:
            log(f"[BSS] map {md5} already exists as id={existing['id']}, updating", Ansi.LCYAN)
            await maps_repo.partial_update(
                id=existing["id"],
                set_id=set_id,
                server="osu!",
                artist=meta["artist"] or existing["artist"],
                title=meta["title"] or existing["title"],
                version=meta["version"] or existing["version"],
                creator=player.name,
                last_update=datetime.now(tz=timezone.utc),
                frozen=True,
                mode=meta["mode"],
                bpm=meta["bpm"],
                cs=meta["cs"],
                ar=meta["ar"],
                od=meta["od"],
                hp=meta["hp"],
                total_length=meta["total_length"],
            )
            osu_file_path = BEATMAPS_PATH / f"{existing['id']}.osu"
            osu_file_path.write_bytes(file_data)
            if md5 in app.state.cache.unsubmitted:
                app.state.cache.unsubmitted.discard(md5)
            continue

        assigned_ids = await _bss_get_next_beatmap_ids(1)
        beatmap_id = assigned_ids[0]

        osu_filename = (
            f"{meta['artist'] or 'Unknown'} - {meta['title'] or 'Unknown'} "
            f"({meta['creator'] or player.name}) "
            f"[{meta['version'] or 'Normal'}].osu"
        ).replace("/", "_").replace("\\", "_")

        log(
            f"[BSS] creating map: id={beatmap_id} set={set_id} "
            f"'{meta['artist']} - {meta['title']} [{meta['version']}]'",
            Ansi.LCYAN,
        )

        try:
            await maps_repo.create(
                id=beatmap_id,
                server="osu!",
                set_id=set_id,
                status=0,  # Pending
                md5=md5,
                artist=meta["artist"] or "Unknown Artist",
                title=meta["title"] or "Unknown Title",
                version=meta["version"] or "Normal",
                creator=meta["creator"] or player.name,
                filename=osu_filename,
                last_update=datetime.now(tz=timezone.utc),
                total_length=meta["total_length"],
                max_combo=0,
                frozen=True,
                plays=0,
                passes=0,
                mode=meta["mode"],
                bpm=meta["bpm"],
                cs=meta["cs"],
                ar=meta["ar"],
                od=meta["od"],
                hp=meta["hp"],
                diff=0.0,
            )
        except Exception as e:
            log(f"[BSS] failed to create map entry: {e!r}", Ansi.LRED)
            # Still save .osu file even if DB insert failed
            osu_file_path = BEATMAPS_PATH / f"{beatmap_id}.osu"
            osu_file_path.write_bytes(file_data)
            continue

        # Store .osu file for gameplay
        osu_file_path = BEATMAPS_PATH / f"{beatmap_id}.osu"
        osu_file_path.write_bytes(file_data)

        # Calculate star rating
        try:
            import akatsuki_pp_py as rosu
            bm = rosu.Beatmap(path=str(osu_file_path))
            calc = rosu.Calculator()
            perf = calc.performance(bm)
            sr = perf.difficulty.stars
            await maps_repo.partial_update(id=beatmap_id, diff=round(sr, 2))
            log(f"[BSS] SR for {beatmap_id}: {sr:.2f}", Ansi.LGREEN)
        except Exception as e:
            log(f"[BSS] SR calc failed for {beatmap_id}: {e}", Ansi.LYELLOW)

        # Clear cached unsubmitted state
        if md5 in app.state.cache.unsubmitted:
            app.state.cache.unsubmitted.discard(md5)

        log(f"[BSS] map {beatmap_id} saved!", Ansi.LGREEN)


# ===================== BSS Endpoints =====================

@router.get("/web/osu-osz2-bmsubmit-getid.php")
async def bmsubmitGetID(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    set_id: int = Query(0, alias="s"),
    beatmap_ids_str: str = Query("0", alias="b"),
    osz2_hash: str = Query("", alias="z"),
) -> Response:
    """BSS Step 1: Assign beatmapset and beatmap IDs."""
    log(f"[BSS] getid from {player}: set_id={set_id} b={beatmap_ids_str}", Ansi.LCYAN)

    try:
        bmap_ids = [int(x) for x in beatmap_ids_str.split(",") if x.strip()]
    except ValueError:
        bmap_ids = [0]

    num_diffs = max(len(bmap_ids), 1)

    if set_id <= 0:
        # New submission
        new_set_id = await _bss_get_next_set_id()
        new_bmap_ids = await _bss_get_next_beatmap_ids(num_diffs)
        full_submit = 1
        log(f"[BSS] new submission: set={new_set_id} maps={new_bmap_ids}", Ansi.LCYAN)
    else:
        # Update existing
        existing_maps = await maps_repo.fetch_many(set_id=set_id)
        if existing_maps:
            creator = existing_maps[0]["creator"]
            if creator.lower() != player.name.lower():
                log(f"[BSS] {player} tried to update set {set_id} owned by {creator}", Ansi.LYELLOW)
                return Response(content="1")

        new_set_id = set_id
        new_bmap_ids = list(bmap_ids)

        for i, bid in enumerate(new_bmap_ids):
            if bid <= 0:
                new_ids = await _bss_get_next_beatmap_ids(1)
                new_bmap_ids[i] = new_ids[0]

        full_submit = 1
        log(f"[BSS] update: set={new_set_id} maps={new_bmap_ids}", Ansi.LCYAN)

    response_lines = [
        "0",
        str(new_set_id),
        ",".join(str(bid) for bid in new_bmap_ids),
        str(full_submit),
        str(new_set_id),
        "0",
    ]
    return Response(content="\n".join(response_lines))


@router.post("/web/osu-osz2-bmsubmit-upload.php")
async def bmsubmitUpload(
    request: Request,
    player: Player = Depends(authenticate_player_session(Form, "u", "h")),
    upload_type: int = Form(1, alias="t"),
    set_id: int = Form(0, alias="s"),
) -> Response:
    """BSS Step 2: Receive uploaded beatmap files."""
    log(f"[BSS] upload from {player}: set_id={set_id} type={upload_type}", Ansi.LCYAN)

    if set_id <= 0:
        log("[BSS] upload with invalid set_id", Ansi.LRED)
        return Response(content="5")

    form = await request.form()

    BEATMAPS_PATH.mkdir(parents=True, exist_ok=True)
    submission_dir = SUBMISSIONS_PATH / str(set_id)
    submission_dir.mkdir(parents=True, exist_ok=True)

    osu_files_found: list[tuple[str, bytes]] = []
    osz2_data: bytes | None = None  # keep track of osz2 for fallback

    for field_name, field_value in form.items():
        if field_name in ("u", "h", "t", "s"):
            continue

        if isinstance(field_value, StarletteUploadFile):
            file_data = await field_value.read()
            filename = field_value.filename or field_name

            log(f"[BSS] received file: {filename} ({len(file_data)} bytes)", Ansi.LCYAN)

            safe_filename = filename.replace("/", "_").replace("\\", "_")
            (submission_dir / safe_filename).write_bytes(file_data)

            if filename.endswith(".osu") or field_name == "osu":
                osu_files_found.append((filename, file_data))
            elif filename.endswith((".osz2", ".osz")) or field_name in ("osz2", "osz"):
                (submission_dir / "upload.osz2").write_bytes(file_data)
                osz2_data = file_data
                extracted = _bss_try_extract_zip(file_data, submission_dir)
                osu_files_found.extend(extracted)

    if osu_files_found:
        await _bss_process_osu_files(osu_files_found, set_id, player)
    elif osz2_data is not None:
        # .osz2 is encrypted — try to decrypt via osz2-service
        log(f"[BSS] .osz2 is encrypted, sending to osz2-service...", Ansi.LYELLOW)
        decrypted = await _bss_decrypt_osz2(osz2_data)

        if decrypted and decrypted.get("files"):
            # Save all decrypted files (audio, images, .osu, etc.)
            for fname, file_b64 in decrypted["files"].items():
                try:
                    file_bytes = base64.b64decode(file_b64)
                except Exception:
                    log(f"[BSS] failed to decode file: {fname}", Ansi.LYELLOW)
                    continue

                safe_name = fname.replace("/", "_").replace("\\", "_")
                (submission_dir / safe_name).write_bytes(file_bytes)

                if fname.endswith(".osu"):
                    osu_files_found.append((fname, file_bytes))
                    log(f"[BSS] extracted .osu: {fname} ({len(file_bytes)} bytes)", Ansi.LCYAN)

            if osu_files_found:
                await _bss_process_osu_files(osu_files_found, set_id, player)
                log(f"[BSS] osz2 fully processed via osz2-service!", Ansi.LGREEN)
            else:
                log(f"[BSS] osz2-service returned files but no .osu — falling back", Ansi.LYELLOW)
                existing_maps = await maps_repo.fetch_many(set_id=set_id)
                if not existing_maps:
                    assigned_ids = await _bss_get_next_beatmap_ids(1)
                    beatmap_id = assigned_ids[0]
                    await _bss_create_entry_from_osz2(osz2_data, set_id, beatmap_id, player)
        else:
            # osz2-service unavailable or failed — fall back to header parsing
            log(f"[BSS] osz2-service unavailable, falling back to header parsing", Ansi.LYELLOW)
            existing_maps = await maps_repo.fetch_many(set_id=set_id)
            if not existing_maps:
                assigned_ids = await _bss_get_next_beatmap_ids(1)
                beatmap_id = assigned_ids[0]
                await _bss_create_entry_from_osz2(osz2_data, set_id, beatmap_id, player)
    else:
        log(f"[BSS] no files found in upload for set {set_id}", Ansi.LYELLOW)

    return Response(content="0")


@router.post("/web/osu-osz2-bmsubmit-post.php")
async def bmsubmitPost(
    player: Player = Depends(authenticate_player_session(Form, "u", "h")),
    set_id: int = Form(0, alias="b"),
    storyboard: int = Form(0, alias="storyboard"),
    notify: int = Form(0, alias="notify"),
    subject: str = Form("", alias="subject"),
    message: str = Form("", alias="message"),
    language_id: int = Form(1, alias="language_id"),
    genre_id: int = Form(1, alias="genre_id"),
) -> Response:
    """BSS Step 3: Finalize submission."""
    log(
        f"[BSS] post from {player}: set_id={set_id} subject='{subject[:50]}'",
        Ansi.LCYAN,
    )
    return Response(content="0")


@router.get("/web/osu-get-beatmap-topic.php")
async def getBeatmapTopic(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    set_id: int = Query(0, alias="s"),
) -> Response:
    """Get beatmap forum topic (stub — we don't have a forum)."""
    return Response(content="")


@router.post("/web/osu-screenshot.php")
async def osuScreenshot(
    player: Player = Depends(authenticate_player_session(Form, "u", "p")),
    endpoint_version: int = Form(..., alias="v"),
    screenshot_file: UploadFile = File(..., alias="ss"),
) -> Response:
    with memoryview(await screenshot_file.read()) as screenshot_view:
        # png sizes: 1080p: ~300-800kB | 4k: ~1-2mB
        if len(screenshot_view) > (4 * 1024 * 1024):
            return Response(
                content=b"Screenshot file too large.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if endpoint_version != 1:
            await app.state.services.log_strange_occurrence(
                f"Incorrect endpoint version (/web/osu-screenshot.php v{endpoint_version})",
            )

        if app.utils.has_jpeg_headers_and_trailers(screenshot_view):
            extension = "jpeg"
        elif app.utils.has_png_headers_and_trailers(screenshot_view):
            extension = "png"
        else:
            return Response(
                content=b"Invalid file type",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        while True:
            filename = f"{secrets.token_urlsafe(6)}.{extension}"
            ss_file = SCREENSHOTS_PATH / filename
            if not ss_file.exists():
                break

        with ss_file.open("wb") as f:
            f.write(screenshot_view)

    log(f"{player} uploaded {filename}.")
    return Response(filename.encode())


@router.get("/web/osu-getfriends.php")
async def osuGetFriends(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
) -> Response:
    return Response("\n".join(map(str, player.friends)).encode())


def bancho_to_osuapi_status(bancho_status: int) -> int:
    return {
        0: 0,
        2: 1,
        3: 2,
        4: 3,
        5: 4,
    }[bancho_status]

@router.post("/web/osu-getbeatmapinfo.php")
async def osuGetBeatmapInfo(
    request: Request,
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
) -> Response:
    body = await request.body()
    f = body.decode("utf-8", errors="replace")

    if not f.strip():
        try:
            form = await request.form()
            f_raw = form.get("f", "")
            if isinstance(f_raw, bytes):
                f_raw = f_raw.decode("utf-8", errors="replace")
            f = str(f_raw)
        except Exception:
            pass

    lines = [ln.strip() for ln in f.splitlines() if ln.strip()]
    log(f"{player} requested info for {len(lines)} maps.", Ansi.LCYAN)

    md5_re = re.compile(r"^[0-9a-f]{32}$", re.I)
    response_lines: list[str] = []

    for idx, entry in enumerate(lines):
        candidate_filename = None
        candidate_md5 = None

        if "|" in entry:
            parts = [p.strip() for p in entry.split("|") if p.strip()]
            for p in parts:
                if md5_re.match(p):
                    candidate_md5 = p.lower()
                    break
            for p in parts:
                if p.lower().endswith(".osu"):
                    candidate_filename = p
                    break
        else:
            if md5_re.match(entry):
                candidate_md5 = entry.lower()
            else:
                candidate_filename = entry

        beatmap = None
        if candidate_filename:
            beatmap = await maps_repo.fetch_one(filename=candidate_filename)
        if not beatmap and candidate_md5:
            beatmap = await maps_repo.fetch_one(md5=candidate_md5)

        if not beatmap:
            bmap_obj = None
            if candidate_md5:
                bmap_obj = await Beatmap.from_md5(candidate_md5)
            if not bmap_obj and candidate_filename:
                rec = await maps_repo.fetch_one(filename=candidate_filename)
                if rec:
                    beatmap = rec
            if bmap_obj and not beatmap:
                beatmap = {
                    "id": bmap_obj.id,
                    "set_id": bmap_obj.set_id,
                    "md5": bmap_obj.md5,
                    "status": int(bmap_obj.status),
                }
            if not beatmap:
                continue

        grades = ["N", "N", "N", "N"]
        for score in await scores_repo.fetch_many(
            map_md5=beatmap["md5"],
            user_id=player.id,
            mode=player.status.mode.as_vanilla,
            status=SubmissionStatus.BEST,
        ):
            grades[score["mode"]] = score["grade"]

        response_lines.append(
            "{i}|{id}|{set_id}|{md5}|{status}|{grades}".format(
                i=idx,
                id=beatmap["id"],
                set_id=beatmap["set_id"],
                md5=beatmap["md5"],
                status=bancho_to_osuapi_status(beatmap["status"]),
                grades="|".join(grades),
            ),
        )

    return Response("\n".join(response_lines).encode(), media_type="text/plain")



@router.get("/web/osu-getfavourites.php")
async def osuGetFavourites(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
) -> Response:
    favourites = await favourites_repo.fetch_all(userid=player.id)

    return Response(
        "\n".join([str(favourite["setid"]) for favourite in favourites]).encode(),
    )


@router.get("/web/osu-addfavourite.php")
async def osuAddFavourite(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    map_set_id: int = Query(..., alias="a"),
) -> Response:
    # check if they already have this favourited.
    if await favourites_repo.fetch_one(player.id, map_set_id):
        return Response(b"You've already favourited this beatmap!")

    # add favourite
    await favourites_repo.create(
        userid=player.id,
        setid=map_set_id,
    )

    return Response(b"Added favourite!")


@router.get("/web/lastfm.php")
async def lastFM(
    action: Literal["scrobble", "np"],
    beatmap_id_or_hidden_flag: str = Query(
        ...,
        description=(
            "This flag is normally a beatmap ID, but is also "
            "used as a hidden anticheat flag within osu!"
        ),
        alias="b",
    ),
    player: Player = Depends(authenticate_player_session(Query, "us", "ha")),
) -> Response:
    if beatmap_id_or_hidden_flag[0] != "a":
        # not anticheat related, tell the
        # client not to send any more for now.
        return Response(b"-3")

    flags = LastFMFlags(int(beatmap_id_or_hidden_flag[1:]))

    if flags & (LastFMFlags.HQ_ASSEMBLY | LastFMFlags.HQ_FILE):
        # Player is currently running hq!osu; could possibly
        # be a separate client, buuuut prooobably not lol.

        await player.restrict(
            admin=app.state.sessions.bot,
            reason=f"hq!osu running ({flags})",
        )

        # refresh their client state
        if player.is_online:
            player.logout()

        return Response(b"-3")

    if flags & LastFMFlags.REGISTRY_EDITS:
        # Player has registry edits left from
        # hq!osu's multiaccounting tool. This
        # does not necessarily mean they are
        # using it now, but they have in the past.

        if random.randrange(32) == 0:
            # Random chance (1/32) for a ban.
            await player.restrict(
                admin=app.state.sessions.bot,
                reason="hq!osu relife 1/32",
            )

            # refresh their client state
            if player.is_online:
                player.logout()

            return Response(b"-3")

        player.enqueue(
            app.packets.notification(
                "\n".join(
                    [
                        "Hey!",
                        "It appears you have hq!osu's multiaccounting tool (relife) enabled.",
                        "This tool leaves a change in your registry that the osu! client can detect.",
                        "Please re-install relife and disable the program to avoid any restrictions.",
                    ],
                ),
            ),
        )

        player.logout()

        return Response(b"-3")

    """ These checks only worked for ~5 hours from release. rumoi's quick!
    if flags & (
        LastFMFlags.SDL2_LIBRARY
        | LastFMFlags.OPENSSL_LIBRARY
        | LastFMFlags.AQN_MENU_SAMPLE
    ):
        # AQN has been detected in the client, either
        # through the 'libeay32.dll' library being found
        # onboard, or from the menu sound being played in
        # the AQN menu while being in an inappropriate menu
        # for the context of the sound effect.
        pass
    """

    return Response(b"")


DIRECT_SET_INFO_FMTSTR = (
    "{SetID}.osz|{Artist}|{Title}|{Creator}|"
    "{RankedStatus}|10.0|{LastUpdate}|{SetID}|"
    "0|{HasVideo}|0|0|0|{diffs}"  # 0s are threadid, has_story,
    # filesize, filesize_novid.
)

DIRECT_MAP_INFO_FMTSTR = (
    "[{DifficultyRating:.2f}⭐] {DiffName} "
    "{{cs: {CS} / od: {OD} / ar: {AR} / hp: {HP}}}@{Mode}"
)


@router.get("/web/osu-search.php")
async def osuSearchHandler(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    ranked_status: int = Query(..., alias="r", ge=0, le=8),
    query: str = Query(..., alias="q"),
    mode: int = Query(..., alias="m", ge=-1, le=3),  # -1 for all
    page_num: int = Query(..., alias="p"),
) -> Response:
    params: dict[str, Any] = {"amount": 100, "offset": page_num * 100}

    # eventually we could try supporting these,
    # but it mostly depends on the mirror.
    if query not in ("Newest", "Top+Rated", "Most+Played"):
        params["query"] = query

    if mode != -1:  # -1 for all
        params["mode"] = mode

    if ranked_status != 4:  # 4 for all
        # convert to osu!api status
        params["status"] = RankedStatus.from_osudirect(ranked_status).osu_api

    response = await app.state.services.http_client.get(
        app.settings.MIRROR_SEARCH_ENDPOINT,
        params=params,
    )
    if response.status_code != status.HTTP_200_OK:
        return Response(b"-1\nFailed to retrieve data from the beatmap mirror.")

    result = response.json()

    lresult = len(result)  # send over 100 if we receive
    # 100 matches, so the client
    # knows there are more to get
    ret = [f"{'101' if lresult == 100 else lresult}"]
    for bmapset in result:
        if bmapset["ChildrenBeatmaps"] is None:
            continue

        # some mirrors use a true/false instead of 0 or 1
        bmapset["HasVideo"] = int(bmapset["HasVideo"])

        diff_sorted_maps = sorted(
            bmapset["ChildrenBeatmaps"],
            key=lambda m: m["DifficultyRating"],
        )

        def handle_invalid_characters(s: str) -> str:
            # XXX: this is a bug that exists on official servers (lmao)
            # | is used to delimit the set data, so the difficulty name
            # cannot contain this or it will be ignored. we fix it here
            # by using a different character.
            return s.replace("|", "I")

        diffs_str = ",".join(
            [
                DIRECT_MAP_INFO_FMTSTR.format(
                    DifficultyRating=row["DifficultyRating"],
                    DiffName=handle_invalid_characters(row["DiffName"]),
                    CS=row["CS"],
                    OD=row["OD"],
                    AR=row["AR"],
                    HP=row["HP"],
                    Mode=row["Mode"],
                )
                for row in diff_sorted_maps
            ],
        )

        ret.append(
            DIRECT_SET_INFO_FMTSTR.format(
                Artist=handle_invalid_characters(bmapset["Artist"]),
                Title=handle_invalid_characters(bmapset["Title"]),
                Creator=bmapset["Creator"],
                RankedStatus=bmapset["RankedStatus"],
                LastUpdate=bmapset["LastUpdate"],
                SetID=bmapset["SetID"],
                HasVideo=bmapset["HasVideo"],
                diffs=diffs_str,
            ),
        )

    return Response("\n".join(ret).encode())


# TODO: video support (needs db change)
@router.get("/web/osu-search-set.php")
async def osuSearchSetHandler(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    map_set_id: int | None = Query(None, alias="s"),
    map_id: int | None = Query(None, alias="b"),
    checksum: str | None = Query(None, alias="c"),
) -> Response:
    # Since we only need set-specific data, we can basically
    # just do same query with either bid or bsid.

    v: int | str
    if map_set_id is not None:
        # this is just a normal request
        k, v = ("set_id", map_set_id)
    elif map_id is not None:
        k, v = ("id", map_id)
    elif checksum is not None:
        k, v = ("md5", checksum)
    else:
        return Response(b"")  # invalid args

    # Get all set data.
    bmapset = await app.state.services.database.fetch_one(
        "SELECT DISTINCT set_id, artist, "
        "title, status, creator, last_update "
        f"FROM maps WHERE {k} = :v",
        {"v": v},
    )
    if bmapset is None:
        # TODO: get from osu!
        return Response(b"")

    rating = 10.0  # TODO: real data

    return Response(
        (
            "{set_id}.osz|{artist}|{title}|{creator}|"
            "{status}|{rating:.1f}|{last_update}|{set_id}|"
            "0|0|0|0|0"
        )
        .format(**bmapset, rating=rating)
        .encode(),
    )
    # 0s are threadid, has_vid, has_story, filesize, filesize_novid


def chart_entry(name: str, before: float | None, after: float | None) -> str:
    return f"{name}Before:{before or ''}|{name}After:{after or ''}"


def format_achievement_string(file: str, name: str, description: str) -> str:
    return f"{file}+{name}+{description}"


# pp is stored as DECIMAL in the database on some deployments. That means
# reading it back yields decimal.Decimal values (from PyMySQL), which do not
# mix with float operations. Also, in rare cases pp calculation can produce
# absurd values (e.g. 1e23) which will overflow DECIMAL columns.
PP_DB_MAX = 9_999_999_999_999.0  # fits DECIMAL(16,3) (13 digits before decimal)

def pp_to_float(value: Any) -> float:
    """Convert a pp value from any backend type to a safe float."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0

    if not math.isfinite(f) or f < 0:
        return 0.0

    if f > PP_DB_MAX:
        log(f"Suspicious pp value {f:.3e}; dropping to 0.000 for safety.", Ansi.LYELLOW)
        return 0.0

    return f


def pp_to_db(value: Any) -> str:
    """Convert pp to a string safe for DECIMAL(16,3) inserts."""
    f = pp_to_float(value)
    return f"{f:.3f}"



def parse_form_data_score_params(
    score_data: FormData,
) -> tuple[bytes, StarletteUploadFile] | None:
    """Parse the score data, and replay file
    from the form data's 'score' parameters."""
    try:
        score_parts = score_data.getlist("score")
        assert len(score_parts) == 2, "Invalid score data"

        score_data_b64 = score_data.getlist("score")[0]
        assert isinstance(score_data_b64, str), "Invalid score data"
        replay_file = score_data.getlist("score")[1]
        assert isinstance(replay_file, StarletteUploadFile), "Invalid replay data"
    except AssertionError as exc:
        log(f"Failed to validate score multipart data: ({exc.args[0]})", Ansi.LRED)
        return None
    else:
        return (
            score_data_b64.encode(),
            replay_file,
        )

if(not app.settings.DISALLOW_OLD_CLIENTS):
    @router.post("/web/osu-submit-modular.php")
    async def osuSubmitModular(
        request: Request,
        score_time: int | None = Form(None, alias="st"),
        fail_time: int | None = Form(None, alias="ft"),
        exited_out: bool = Form(..., alias="x"),
        visual_settings_b64: bytes = Form(..., alias="fs"),
        storyboard_md5: str | None = Form(None, alias="sbk"),
        iv_b64: bytes = Form(..., alias="iv"),
        unique_ids: str = Form(..., alias="c1"),
        pw_md5: str = Form(..., alias="pass"),
        osu_version: str = Form(..., alias="osuver"),
        client_hash_b64: bytes = Form(..., alias="s"),
    ) -> Response:
        """Handle a score submission from an osu! client with an active session."""

        # NOTE: the bancho protocol uses the "score" parameter name for both
        # the base64'ed score data, and the replay file in the multipart
        # starlette/fastapi do not support this, so we've moved it out
        score_parameters = parse_form_data_score_params(await request.form())
        if score_parameters is None:
            return Response(b"")


        # extract the score data and replay file from the score data
        score_data_b64, replay_file = score_parameters

        # decrypt the score data (aes)
        score_data, client_hash_decoded = encryption.decrypt_score_aes_data(
            score_data_b64,
            client_hash_b64,
            iv_b64,
            osu_version,
        )

        raw_client_hash = _coerce_to_str(client_hash_decoded)
        norm_client_hash = _normalize_client_hash_for_compare(raw_client_hash)

        # fetch map & player

        bmap_md5 = score_data[0]
        bmap = await Beatmap.from_md5(bmap_md5)
        if not bmap:
            # Map does not exist, most likely unsubmitted.
            return Response(b"error: beatmap")

        # if the client has supporter, a space is appended
        # but usernames may also end with a space, which must be preserved
        username = score_data[1]
        if username[-1] == " ":
            username = username[:-1]

        player = await app.state.sessions.players.from_login(username, pw_md5)
        if not player:
            # Player is not online, return nothing so that their
            # client will retry submission when they log in.
            return Response(b"")

        # parse the score from the remaining data
        score = Score.from_submission(score_data[2:])

        # attach bmap & player
        score.bmap = bmap
        score.player = player

        ## perform checksum validation

        unique_id1, unique_id2 = unique_ids.split("|", maxsplit=1)
        unique_id1_md5 = hashlib.md5(unique_id1.encode()).hexdigest()
        unique_id2_md5 = hashlib.md5(unique_id2.encode()).hexdigest()

        try:
            assert player.client_details is not None

            if osu_version != f"{player.client_details.osu_version.date:%Y%m%d}":
                raise ValueError("osu! version mismatch")

            stored_client_hash = _coerce_to_str(player.client_details.client_hash)

            client_hash_for_compare = norm_client_hash if SANITIZE_CLIENT_HASH else raw_client_hash
            stored_hash_for_compare = (
                _normalize_client_hash_for_compare(stored_client_hash)
                if SANITIZE_CLIENT_HASH
                else stored_client_hash
            )

            if client_hash_for_compare != stored_hash_for_compare:
                raise ValueError("client hash mismatch")
            # assert unique ids (c1) are correct and match login params
            if unique_id1_md5 != player.client_details.uninstall_md5:
                raise ValueError(
                    f"unique_id1 mismatch ({unique_id1_md5} != {player.client_details.uninstall_md5})",
                )

            if unique_id2_md5 != player.client_details.disk_signature_md5:
                raise ValueError(
                    f"unique_id2 mismatch ({unique_id2_md5} != {player.client_details.disk_signature_md5})",
                )

            # assert online checksums match
            server_score_checksum = score.compute_online_checksum(
                osu_version=osu_version,
                osu_client_hash=raw_client_hash,
                storyboard_checksum=storyboard_md5 or "",
            )
            if score.client_checksum != server_score_checksum:
                raise ValueError(
                    f"online score checksum mismatch ({server_score_checksum} != {score.client_checksum})",
                )

        except (ValueError, AssertionError) as exc:
            # NOTE: this is undergoing a temporary trial period,
            # after which, it will be enabled & perform restrictions.
            if DIAG_SCORE_SUBMIT:
                await app.state.services.log_strange_occurrence(
                    {
                        "where": "osuSubmitModular",
                        "error": repr(exc),
                        "player": f"{player.name} ({player.id})",
                        "bmap_md5": bmap_md5,
                        "osu_version": osu_version,
                        "sanitize_client_hash": SANITIZE_CLIENT_HASH,
                        "client_hash_raw": _redact(raw_client_hash),
                        "client_hash_stored": _redact(getattr(player.client_details, "client_hash", "")),
                    },
                )
            else:
                log(f"[score-submit] validation failed (trial): {exc}", Ansi.LYELLOW)

            # await player.restrict(
            #     admin=app.state.sessions.bot,
            #     reason="mismatching hashes on score submission",
            # )

            # refresh their client state
            # if player.online:
            #     player.logout()

            # return b"error: ban"

        # we should update their activity no matter
        # what the result of the score submission is.
        score.player.update_latest_activity_soon()

        # make sure the player's client displays the correct mode's stats
        if score.mode != score.player.status.mode:
            score.player.status.mods = score.mods
            score.player.status.mode = score.mode

            if not score.player.restricted:
                app.state.sessions.players.enqueue(app.packets.user_stats(score.player))

        # hold a lock around (check if submitted, submission) to ensure no duplicates
        # are submitted to the database, and potentially award duplicate score/pp/etc.
        async with app.state.score_submission_locks[score.client_checksum]:
            # stop here if this is a duplicate score
            if await app.state.services.database.fetch_one(
                "SELECT 1 FROM scores WHERE online_checksum = :checksum",
                {"checksum": score.client_checksum},
            ):
                log(f"{score.player} submitted a duplicate score.", Ansi.LYELLOW)
                return Response(b"error: no")

            # all data read from submission.
            # now we can calculate things based on our data.
            score.acc = score.calculate_accuracy()

            osu_file_available = await ensure_osu_file_is_available(
                bmap.id,
                expected_md5=bmap.md5,
            )
            if osu_file_available:
                try:
                    score.pp, score.sr = score.calculate_performance(bmap.id)
                except Exception as e:
                    log(f"[BSS] PP calc failed for map {bmap.id}: {e}", Ansi.LYELLOW)
                    score.pp, score.sr = 0.0, 0.0

                if score.passed:
                    await score.calculate_status()

                    if score.bmap.status != RankedStatus.Pending:
                        score.rank = await score.calculate_placement()
                else:
                    score.status = SubmissionStatus.FAILED

            score.time_elapsed = score_time if score.passed else fail_time
        
            score_eligible = score.bmap.awards_ranked_pp and score.passed
            player_eligible = not score.player.priv & Privileges.WHITELISTED and not score.player.restricted
            if score_eligible and player_eligible and capData["enabled"]:
                log(f"caps: {capData['caps']}")
                caps = capData["caps"]

                # check if pp cap was exceeded
                if str(score.mode) in caps and score.pp >= caps[str(score.mode)]:
                    await score.player.restrict(
                        admin=app.state.sessions.bot,
                        reason=f"[{score.mode!r} autoban] liveplay requested",
                    )

                    # refresh their client state
                    if score.player.is_online:
                        score.player.logout()

            """ Score submission checks completed; submit the score. """

            if app.state.services.datadog:
                app.state.services.datadog.increment("bancho.submitted_scores")

            if app.metrics.enabled:
                app.metrics.increment("ex_submitted_scores")

            if score.status == SubmissionStatus.BEST:
                if app.state.services.datadog:
                    app.state.services.datadog.increment("bancho.submitted_scores_best")
                
                if app.metrics.enabled:
                    app.metrics.increment("ex_submitted_scores_best")

                if score.bmap.has_leaderboard:
                    if score.bmap.status == RankedStatus.Loved and score.mode in (
                        GameMode.VANILLA_OSU,
                        GameMode.VANILLA_TAIKO,
                        GameMode.VANILLA_CATCH,
                        GameMode.VANILLA_MANIA,
                    ):
                        performance = f"{score.score:,} score"
                    else:
                        performance = f"{score.pp:,.2f}pp"

                    score.player.enqueue(
                        app.packets.notification(
                            f"You achieved #{score.rank}! ({performance})",
                        ),
                    )

                    if score.rank == 1 and not score.player.restricted and score.status == 2 and _is_ranked_for_first_places(score.bmap.status):
                        announce_chan = app.state.sessions.channels.get_by_name("#announce")

                        ann = [
                            f"\x01ACTION achieved #1 on {score.bmap.embed}",
                            f"with {score.acc:.2f}% for {performance}.",
                        ]

                        if score.mods:
                            ann.insert(1, f"+{_fmt_mods_for_announce(score.mods)}")

                        scoring_metric = (
                            "pp"
                        )

                        # If there was previously a score on the map, add old #1.
                        prev_n1 = await app.state.services.database.fetch_one(
                            "SELECT u.id, name FROM users u "
                            "INNER JOIN scores s ON u.id = s.userid "
                            "WHERE s.map_md5 = :map_md5 AND s.mode = :mode "
                            "AND s.status = 2 AND u.priv & 1 "
                            f"ORDER BY s.{scoring_metric} DESC LIMIT 1",
                            {"map_md5": score.bmap.md5, "mode": score.mode},
                        )

                        if prev_n1:
                            if score.player.id != prev_n1["id"]:
                                ann.append(
                                    f"(Previous #1: [https://osu.{app.settings.DOMAIN}/u/"
                                    "{id} {name}])".format(
                                        id=prev_n1["id"],
                                        name=prev_n1["name"],
                                    ),
                                )

                        assert announce_chan is not None
                        announce_chan.send(" ".join(ann), sender=score.player, to_self=True)

                        if(app.settings.ENABLE_FIRST_PLACES_WEBHOOK):
                            embed = Embed(
                            title=f"#1 achieved by {score.player.name}",
                            description=f"{score.player.name} has achieved #1 on \nhttps://{app.settings.DOMAIN}/b/{score.bmap.id}",
                            color=0xFFD700,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            )

                            embed.add_field(name="Accuracy", value=f"{score.acc:.2f}%", inline=True)
                            embed.add_field(name="Performance", value=f"{performance}", inline=True)
                            embed.add_field(name="Combo", value=f"{score.max_combo}x", inline=True)
                            embed.add_field(name="Misses", value=f"{score.nmiss}", inline=True)

                            if score.mods:
                                embed.add_field(name="Mods", value=f"{score.mods!r}", inline=True)

                            if prev_n1:
                                if score.player.id != prev_n1["id"]:
                                    embed.add_field(
                                    name="Previous #1",
                                    value=f"[{prev_n1['name']}](https://{app.settings.DOMAIN}/u/{prev_n1['id']})",
                                    inline=False,
                                    )

                            embed.set_thumbnail(url=f"https://a.{app.settings.DOMAIN}/{score.player.id}")
                            webhook_url = app.settings.FIRST_PLACES_WEBHOOK
                            webhook = Webhook(url=webhook_url)
                            webhook.add_embed(embed)

                            asyncio.create_task(webhook.post())

                            if app.metrics.enabled:
                                app.metrics.increment("ex_first_place_webhook")

                # this score is our best score.
                # Lock + reset + INSERT atomowo w jednej transakcji.
                pp_db = pp_to_db(score.pp)
                async with app.state.services.database.transaction():
                    await app.state.services.database.fetch_one(
                        "SELECT id FROM users WHERE id = :user_id FOR UPDATE",
                        {"user_id": score.player.id},
                    )
                    await app.state.services.database.execute(
                        "UPDATE scores SET status = 1 "
                        "WHERE status IN (2, 3) AND map_md5 = :map_md5 "
                        "AND userid = :user_id AND mode = :mode",
                        {
                            "map_md5": score.bmap.md5,
                            "user_id": score.player.id,
                            "mode": score.mode,
                        },
                    )
                    if score.prev_best and score.prev_best.status == SubmissionStatus.SCORE_BEST:
                        await app.state.services.database.execute(
                            "UPDATE scores SET status = 3 WHERE id = :id",
                            {"id": score.prev_best.id},
                        )
                    score.id = await app.state.services.database.execute(
                        "INSERT INTO scores "
                        "VALUES (NULL, "
                        ":map_md5, :score, :pp, :acc, "
                        ":max_combo, :mods, :n300, :n100, "
                        ":n50, :nmiss, :ngeki, :nkatu, "
                        ":grade, :status, :mode, :play_time, "
                        ":time_elapsed, :client_flags, :user_id, :perfect, "
                        ":checksum)",
                        {
                            "map_md5": score.bmap.md5,
                            "score": score.score,
                            "pp": pp_db,
                            "acc": score.acc,
                            "max_combo": score.max_combo,
                            "mods": score.mods,
                            "n300": score.n300,
                            "n100": score.n100,
                            "n50": score.n50,
                            "nmiss": score.nmiss,
                            "ngeki": score.ngeki,
                            "nkatu": score.nkatu,
                            "grade": score.grade.name,
                            "status": score.status,
                            "mode": score.mode,
                            "play_time": score.server_time,
                            "time_elapsed": score.time_elapsed,
                            "client_flags": score.client_flags,
                            "user_id": score.player.id,
                            "perfect": score.perfect,
                            "checksum": score.client_checksum,
                        },
                    )

            if score.status == SubmissionStatus.SCORE_BEST:
                # Nowy score ma wyzszy score value ale nizsze PP.
                # Zdegraduj stary SCORE_BEST do SUBMITTED.
                await app.state.services.database.execute(
                    "UPDATE scores SET status = 1 "
                    "WHERE status = 3 AND map_md5 = :map_md5 "
                    "AND userid = :user_id AND mode = :mode",
                    {
                        "map_md5": score.bmap.md5,
                        "user_id": score.player.id,
                        "mode": score.mode,
                    },
                )

            if not score.id:
                pp_db = pp_to_db(score.pp)
                score.id = await app.state.services.database.execute(
                    "INSERT INTO scores "
                    "VALUES (NULL, "
                    ":map_md5, :score, :pp, :acc, "
                    ":max_combo, :mods, :n300, :n100, "
                    ":n50, :nmiss, :ngeki, :nkatu, "
                    ":grade, :status, :mode, :play_time, "
                    ":time_elapsed, :client_flags, :user_id, :perfect, "
                    ":checksum)",
                    {
                        "map_md5": score.bmap.md5,
                        "score": score.score,
                        "pp": pp_db,
                        "acc": score.acc,
                        "max_combo": score.max_combo,
                        "mods": score.mods,
                        "n300": score.n300,
                        "n100": score.n100,
                        "n50": score.n50,
                        "nmiss": score.nmiss,
                        "ngeki": score.ngeki,
                        "nkatu": score.nkatu,
                        "grade": score.grade.name,
                        "status": score.status,
                        "mode": score.mode,
                        "play_time": score.server_time,
                        "time_elapsed": score.time_elapsed,
                        "client_flags": score.client_flags,
                        "user_id": score.player.id,
                        "perfect": score.perfect,
                        "checksum": score.client_checksum,
                    },
                )

            await app.state.services.redis.publish("ex:submit", score.toJSON())
            

        if score.passed:
            replay_data = await replay_file.read()

            MIN_REPLAY_SIZE = 24

            if len(replay_data) >= MIN_REPLAY_SIZE:
                replay_disk_file = REPLAYS_PATH / f"{score.id}.osr"
                replay_disk_file.write_bytes(replay_data)
            else:
                log(f"{score.player} submitted a score without a replay!", Ansi.LRED)

                if not score.player.restricted:
                    await score.player.restrict(
                        admin=app.state.sessions.bot,
                        reason="submitted score with no replay",
                    )
                    if score.player.is_online:
                        score.player.logout()

        """ Update the user's & beatmap's stats """

        # get the current stats, and take a
        # shallow copy for the response charts.
        stats = score.player.stats[score.mode]
        prev_stats = copy.copy(stats)

        # stuff update for all submitted scores
        stats.playtime += score.time_elapsed // 1000
        stats.plays += 1
        stats.tscore += score.score
        stats.total_hits += score.n300 + score.n100 + score.n50

        if score.mode.as_vanilla in (1, 3):
            # taiko uses geki & katu for hitting big notes with 2 keys
            # mania uses geki & katu for rainbow 300 & 200
            stats.total_hits += score.ngeki + score.nkatu

        stats_updates: dict[str, Any] = {
            "plays": stats.plays,
            "playtime": stats.playtime,
            "tscore": stats.tscore,
            "total_hits": stats.total_hits,
        }

        if score.passed and score.bmap.has_leaderboard:
            # player passed & map is ranked, approved, or loved.

            if score.max_combo > stats.max_combo:
                stats.max_combo = score.max_combo
                stats_updates["max_combo"] = stats.max_combo

            if score.bmap.awards_ranked_pp and score.status == SubmissionStatus.BEST:
                # map is ranked or approved, and it's our (new)
                # best score on the map. update the player's
                # ranked score, grades, pp, acc and global rank.

                additional_rscore = score.score
                if score.prev_best:
                    # we previously had a score, so remove
                    # it's score from our ranked score.
                    additional_rscore -= score.prev_best.score

                    if score.grade != score.prev_best.grade:
                        if score.grade >= Grade.A:
                            stats.grades[score.grade] += 1
                            grade_col = format(score.grade, "stats_column")
                            stats_updates[grade_col] = stats.grades[score.grade]

                        if score.prev_best.grade >= Grade.A:
                            stats.grades[score.prev_best.grade] -= 1
                            grade_col = format(score.prev_best.grade, "stats_column")
                            stats_updates[grade_col] = stats.grades[score.prev_best.grade]
                else:
                    # this is our first submitted score on the map
                    if score.grade >= Grade.A:
                        stats.grades[score.grade] += 1
                        grade_col = format(score.grade, "stats_column")
                        stats_updates[grade_col] = stats.grades[score.grade]

                stats.rscore += additional_rscore
                stats_updates["rscore"] = stats.rscore

                # fetch scores sorted by pp for total acc/pp calc
                # NOTE: we select all plays (and not just top100)
                # because bonus pp counts the total amount of ranked
                # scores. I'm aware this scales horribly, and it'll
                # likely be split into two queries in the future.
                best_scores = await app.state.services.database.fetch_all(
                    "SELECT s.pp, s.acc FROM scores s "
                    "INNER JOIN maps m ON s.map_md5 = m.md5 "
                    "WHERE s.userid = :user_id AND s.mode = :mode "
                    "AND s.status = 2 AND m.status IN (2, 3) "  # ranked, approved
                    "ORDER BY s.pp DESC",
                    {"user_id": score.player.id, "mode": score.mode},
                )

                # calculate new total weighted accuracy
                weighted_acc = sum(float(row["acc"]) * 0.95**i for i, row in enumerate(best_scores))
                bonus_acc = 100.0 / (20 * (1 - 0.95 ** len(best_scores)))
                stats.acc = (weighted_acc * bonus_acc) / 100
                stats_updates["acc"] = stats.acc

                # calculate new total weighted pp
                weighted_pp = sum(pp_to_float(row["pp"]) * 0.95**i for i, row in enumerate(best_scores))
                bonus_pp = 416.6667 * (1 - 0.9994 ** len(best_scores))
                stats.pp = round(weighted_pp + bonus_pp)
                stats_updates["pp"] = stats.pp

                # update global & country ranking
                stats.rank = await score.player.update_rank(score.mode)

        await stats_repo.partial_update(
            score.player.id,
            score.mode.value,
            plays=stats_updates.get("plays", UNSET),
            playtime=stats_updates.get("playtime", UNSET),
            tscore=stats_updates.get("tscore", UNSET),
            total_hits=stats_updates.get("total_hits", UNSET),
            max_combo=stats_updates.get("max_combo", UNSET),
            xh_count=stats_updates.get("xh_count", UNSET),
            x_count=stats_updates.get("x_count", UNSET),
            sh_count=stats_updates.get("sh_count", UNSET),
            s_count=stats_updates.get("s_count", UNSET),
            a_count=stats_updates.get("a_count", UNSET),
            rscore=stats_updates.get("rscore", UNSET),
            acc=stats_updates.get("acc", UNSET),
            pp=stats_updates.get("pp", UNSET),
        )

        if not score.player.restricted:
            # enqueue new stats info to all other users
            app.state.sessions.players.enqueue(app.packets.user_stats(score.player))

            # update beatmap with new stats
            score.bmap.plays += 1
            if score.passed:
                score.bmap.passes += 1

            await app.state.services.database.execute(
                "UPDATE maps SET plays = :plays, passes = :passes WHERE md5 = :map_md5",
                {
                    "plays": score.bmap.plays,
                    "passes": score.bmap.passes,
                    "map_md5": score.bmap.md5,
                },
            )

        # update their recent score
        score.player.recent_scores[score.mode] = score

        """ score submission charts """

        # charts are only displayed for passes vanilla gamemodes.
        if not score.passed:  # TODO: check if this is correct
            response = b"error: no"
        else:
            # construct and send achievements & ranking charts to the client
            if score.bmap.awards_ranked_pp and not score.player.restricted:
                unlocked_achievements: list[Achievement] = []

                server_achievements = await achievements_usecases.fetch_many()
                player_achievements = await user_achievements_usecases.fetch_many(
                    user_id=score.player.id,
                )

                for server_achievement in server_achievements:
                    player_unlocked_achievement = any(
                        player_achievement
                        for player_achievement in player_achievements
                        if player_achievement["achid"] == server_achievement["id"]
                    )
                    if player_unlocked_achievement:
                        # player already has this achievement.
                        continue

                    achievement_condition = server_achievement["cond"]
                    if achievement_condition(score, score.mode.as_vanilla):
                        await user_achievements_usecases.create(
                            score.player.id,
                            server_achievement["id"],
                        )
                        unlocked_achievements.append(server_achievement)

                achievements_str = "/".join(
                    format_achievement_string(a["file"], a["name"], a["desc"])
                    for a in unlocked_achievements
                )
            else:
                achievements_str = ""

            # create score submission charts for osu! client to display

            if score.prev_best:
                beatmap_ranking_chart_entries = (
                        chart_entry("rank", score.prev_best.rank, score.rank),
                        chart_entry("rankedScore", score.prev_best.score, score.score),
                        chart_entry("totalScore", score.prev_best.score, score.score),
                        chart_entry("maxCombo", score.prev_best.max_combo, score.max_combo),
                        chart_entry(
                            "accuracy",
                            round(score.prev_best.acc, 2),
                            round(score.acc, 2),
                        ),
                        chart_entry("pp", score.prev_best.pp, score.pp),
                )
            else:
                # no previous best score
                beatmap_ranking_chart_entries = (
                    chart_entry("rank", None, score.rank),
                    chart_entry("rankedScore", None, score.score),
                    chart_entry("totalScore", None, score.score),
                    chart_entry("maxCombo", None, score.max_combo),
                    chart_entry("accuracy", None, round(score.acc, 2)),
                    chart_entry("pp", None, score.pp),
                )

            overall_ranking_chart_entries = (
                chart_entry("rank", prev_stats.rank, stats.rank),
                chart_entry("rankedScore", prev_stats.rscore, stats.rscore),
                chart_entry("totalScore", prev_stats.tscore, stats.tscore),
                chart_entry("maxCombo", prev_stats.max_combo, stats.max_combo),
                chart_entry("accuracy", round(prev_stats.acc, 2), round(stats.acc, 2)),
                chart_entry("pp", prev_stats.pp, stats.pp),
            )

            submission_charts = [
                # beatmap info chart
                f"beatmapId:{score.bmap.id}",
                f"beatmapSetId:{score.bmap.set_id}",
                f"beatmapPlaycount:{score.bmap.plays}",
                f"beatmapPasscount:{score.bmap.passes}",
                f"approvedDate:{score.bmap.last_update}",
                "\n",
                # beatmap ranking chart
                "chartId:beatmap",
                f"chartUrl:{score.bmap.set.url}",
                "chartName:Beatmap Ranking",
                *beatmap_ranking_chart_entries,
                f"onlineScoreId:{score.id}",
                "\n",
                # overall ranking chart
                "chartId:overall",
                f"chartUrl:https://{app.settings.DOMAIN}/u/{score.player.id}",
                "chartName:Overall Ranking",
                *overall_ranking_chart_entries,
                f"achievements-new:{achievements_str}",
            ]

            response = "|".join(submission_charts).encode()

        log(
            f"[{score.mode!r}] {score.player} submitted a score! "
            f"({score.status!r}, {score.pp:,.2f}pp / {stats.pp:,}pp)",
            Ansi.LGREEN,
        )

        return Response(response)


@router.post("/web/osu-submit-modular-selector.php")
async def osuSubmitModularSelector(
    request: Request,
    # TODO: should token be allowed
    # through but ac'd if not found?
    # TODO: validate token format
    # TODO: save token in the database
    token: str = Header(...),
    # TODO: do ft & st contain pauses?
    exited_out: bool = Form(..., alias="x"),
    fail_time: int = Form(..., alias="ft"),
    visual_settings_b64: bytes = Form(..., alias="fs"),
    updated_beatmap_hash: str = Form(..., alias="bmk"),
    storyboard_md5: str | None = Form(None, alias="sbk"),
    iv_b64: bytes = Form(..., alias="iv"),
    unique_ids: str = Form(..., alias="c1"),
    score_time: int = Form(..., alias="st"),
    pw_md5: str = Form(..., alias="pass"),
    osu_version: str = Form(..., alias="osuver"),
    client_hash_b64: bytes = Form(..., alias="s"),
    fl_cheat_screenshot: bytes | None = File(None, alias="i"),
) -> Response:
    """Handle a score submission from an osu! client with an active session."""

    if fl_cheat_screenshot:
        if DIAG_SCORE_SUBMIT:
            await app.state.services.log_strange_occurrence(
                {"where": "osuSubmitModularSelector", "event": "fl_cheat_screenshot_present"},
            )
        else:
            log("[score-submit] cheat screenshot provided (trial mode).", Ansi.LYELLOW)

    # NOTE: the bancho protocol uses the "score" parameter name for both
    # the base64'ed score data, and the replay file in the multipart
    # starlette/fastapi do not support this, so we've moved it out
    score_parameters = parse_form_data_score_params(await request.form())
    if score_parameters is None:
        return Response(b"")

    # extract the score data and replay file from the score data
    score_data_b64, replay_file = score_parameters

    # decrypt the score data (aes)
    score_data, client_hash_decoded = encryption.decrypt_score_aes_data(
        score_data_b64,
        client_hash_b64,
        iv_b64,
        osu_version,
    )

    raw_client_hash = _coerce_to_str(client_hash_decoded)
    norm_client_hash = _normalize_client_hash_for_compare(raw_client_hash)

    # fetch map & player

    bmap_md5 = score_data[0]
    bmap = await Beatmap.from_md5(bmap_md5)
    if not bmap:
        # Map does not exist, most likely unsubmitted.
        return Response(b"error: beatmap")

    # if the client has supporter, a space is appended
    # but usernames may also end with a space, which must be preserved
    username = score_data[1]
    if username[-1] == " ":
        username = username[:-1]

    player = await app.state.sessions.players.from_login(username, pw_md5)
    if not player:
        # Player is not online, return nothing so that their
        # client will retry submission when they log in.
        return Response(b"")

    # parse the score from the remaining data
    score = Score.from_submission(score_data[2:])

    # attach bmap & player
    score.bmap = bmap
    score.player = player

    ## perform checksum validation

    unique_id1, unique_id2 = unique_ids.split("|", maxsplit=1)
    unique_id1_md5 = hashlib.md5(unique_id1.encode()).hexdigest()
    unique_id2_md5 = hashlib.md5(unique_id2.encode()).hexdigest()

    try:
        assert player.client_details is not None

        if osu_version != f"{player.client_details.osu_version.date:%Y%m%d}":
            raise ValueError("osu! version mismatch")

        stored_client_hash = _coerce_to_str(player.client_details.client_hash)

        client_hash_for_compare = norm_client_hash if SANITIZE_CLIENT_HASH else raw_client_hash
        stored_hash_for_compare = (
            _normalize_client_hash_for_compare(stored_client_hash)
            if SANITIZE_CLIENT_HASH
            else stored_client_hash
        )

        if client_hash_for_compare != stored_hash_for_compare:
            raise ValueError("client hash mismatch")
        # assert unique ids (c1) are correct and match login params
        if unique_id1_md5 != player.client_details.uninstall_md5:
            raise ValueError(
                f"unique_id1 mismatch ({unique_id1_md5} != {player.client_details.uninstall_md5})",
            )

        if unique_id2_md5 != player.client_details.disk_signature_md5:
            raise ValueError(
                f"unique_id2 mismatch ({unique_id2_md5} != {player.client_details.disk_signature_md5})",
            )

        # assert online checksums match
        server_score_checksum = score.compute_online_checksum(
            osu_version=osu_version,
            osu_client_hash=raw_client_hash,
            storyboard_checksum=storyboard_md5 or "",
        )
        if score.client_checksum != server_score_checksum:
            raise ValueError(
                f"online score checksum mismatch ({server_score_checksum} != {score.client_checksum})",
            )

        # assert beatmap hashes match
        if bmap_md5 != updated_beatmap_hash:
            raise ValueError(
                f"beatmap hash mismatch ({bmap_md5} != {updated_beatmap_hash})",
            )

    except (ValueError, AssertionError) as exc:
        # NOTE: this is undergoing a temporary trial period,
        # after which, it will be enabled & perform restrictions.
        if DIAG_SCORE_SUBMIT:
            await app.state.services.log_strange_occurrence(
                {
                    "where": "osuSubmitModularSelector",
                    "error": repr(exc),
                    "player": f"{player.name} ({player.id})" if player else None,
                    "bmap_md5": bmap_md5,
                    "osu_version": osu_version,
                    "sanitize_client_hash": SANITIZE_CLIENT_HASH,
                    "client_hash_raw": _redact(raw_client_hash),
                    "client_hash_stored": _redact(getattr(getattr(player, "client_details", None), "client_hash", "")),
                },
            )
        else:
            log(f"[score-submit] validation failed (trial): {exc}", Ansi.LYELLOW)

        # await player.restrict(
        #     admin=app.state.sessions.bot,
        #     reason="mismatching hashes on score submission",
        # )

        # refresh their client state
        # if player.online:
        #     player.logout()

        # return b"error: ban"

    # we should update their activity no matter
    # what the result of the score submission is.
    score.player.update_latest_activity_soon()

    # make sure the player's client displays the correct mode's stats
    if score.mode != score.player.status.mode:
        score.player.status.mods = score.mods
        score.player.status.mode = score.mode

        if not score.player.restricted:
            app.state.sessions.players.enqueue(app.packets.user_stats(score.player))

    # hold a lock around (check if submitted, submission) to ensure no duplicates
    # are submitted to the database, and potentially award duplicate score/pp/etc.
    async with app.state.score_submission_locks[score.client_checksum]:
        # stop here if this is a duplicate score
        if await app.state.services.database.fetch_one(
            "SELECT 1 FROM scores WHERE online_checksum = :checksum",
            {"checksum": score.client_checksum},
        ):
            log(f"{score.player} submitted a duplicate score.", Ansi.LYELLOW)
            return Response(b"error: no")

        # all data read from submission.
        # now we can calculate things based on our data.
        score.acc = score.calculate_accuracy()

        osu_file_available = await ensure_osu_file_is_available(
            bmap.id,
            expected_md5=bmap.md5,
        )
        if osu_file_available:
            try:
                score.pp, score.sr = score.calculate_performance(bmap.id)
            except Exception as e:
                log(f"[BSS] PP calc failed for map {bmap.id}: {e}", Ansi.LYELLOW)
                score.pp, score.sr = 0.0, 0.0
            score.pp = pp_to_float(score.pp)  # clamp/normalize for DECIMAL and safety

            if score.passed:
                await score.calculate_status()

                if score.bmap.status != RankedStatus.Pending:
                    score.rank = await score.calculate_placement()
            else:
                score.status = SubmissionStatus.FAILED

        score.time_elapsed = score_time if score.passed else fail_time
        score_eligible = score.bmap.awards_ranked_pp and score.passed
        player_eligible = not score.player.priv & Privileges.WHITELISTED and not score.player.restricted
        if score_eligible and player_eligible and capData["enabled"]:
            log(f"caps: {capData['caps']}")
            caps = capData["caps"]

            # check if pp cap was exceeded
            if str(score.mode) in caps and score.pp >= caps[str(score.mode)]:
                await score.player.restrict(
                    admin=app.state.sessions.bot,
                    reason=f"[{score.mode!r} autoban] liveplay requested",
                )

                # refresh their client state
                if score.player.is_online:
                    score.player.logout()

        """ Score submission checks completed; submit the score. """

        if app.state.services.datadog:
            app.state.services.datadog.increment("bancho.submitted_scores")  # type: ignore[no-untyped-call]

        if app.metrics.enabled:
            app.metrics.increment("ex_submitted_scores")

        if score.status == SubmissionStatus.BEST:
            if app.state.services.datadog:
                app.state.services.datadog.increment("bancho.submitted_scores_best")  # type: ignore[no-untyped-call]

            if app.metrics.enabled:
                app.metrics.increment("ex_submitted_scores_best")
            if score.bmap.has_leaderboard:
                if score.bmap.status == RankedStatus.Loved and score.mode in (
                    GameMode.VANILLA_OSU,
                    GameMode.VANILLA_TAIKO,
                    GameMode.VANILLA_CATCH,
                    GameMode.VANILLA_MANIA,
                ):
                    performance = f"{score.score:,} score"
                else:
                    performance = f"{score.pp:,.2f}pp"

                score.player.enqueue(
                    app.packets.notification(
                        f"You achieved #{score.rank}! ({performance})",
                    ),
                )

                if score.rank == 1 and not score.player.restricted and score.status == 2 and _is_ranked_for_first_places(score.bmap.status):
                    announce_chan = app.state.sessions.channels.get_by_name("#announce")

                    ann = [
                        f"\x01ACTION achieved #1 on {score.bmap.embed}",
                        f"with {score.acc:.2f}% for {performance}.",
                    ]

                    if score.mods:
                        ann.insert(1, f"+{_fmt_mods_for_announce(score.mods)}")

                    scoring_metric = (
                        "pp" if score.mode >= GameMode.RELAX_OSU else "score"
                    )

                    # If there was previously a score on the map, add old #1.
                    prev_n1 = await app.state.services.database.fetch_one(
                        "SELECT u.id, name FROM users u "
                        "INNER JOIN scores s ON u.id = s.userid "
                        "WHERE s.map_md5 = :map_md5 AND s.mode = :mode "
                        "AND s.status = 2 AND u.priv & 1 "
                        f"ORDER BY s.{scoring_metric} DESC LIMIT 1",
                        {"map_md5": score.bmap.md5, "mode": score.mode},
                    )

                    if prev_n1:
                        if score.player.id != prev_n1["id"]:
                            ann.append(
                                f"(Previous #1: [https://osu.{app.settings.DOMAIN}/u/"
                                "{id} {name}])".format(
                                    id=prev_n1["id"],
                                    name=prev_n1["name"],
                                ),
                            )

                    assert announce_chan is not None
                    announce_chan.send(" ".join(ann), sender=score.player, to_self=True)

                    if(app.settings.ENABLE_FIRST_PLACES_WEBHOOK):
                        embed = Embed(
                        title=f"#1 achieved by {score.player.name}",
                        description=f"{score.player.name} has achieved #1 on \nhttps://{app.settings.DOMAIN}/b/{score.bmap.id}",
                        color=0xFFD700,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        )

                        embed.add_field(name="Accuracy", value=f"{score.acc:.2f}%", inline=True)
                        embed.add_field(name="Performance", value=f"{performance}", inline=True)
                        embed.add_field(name="Combo", value=f"{score.max_combo}x", inline=True)
                        embed.add_field(name="Misses", value=f"{score.nmiss}", inline=True)

                        if score.mods:
                            embed.add_field(name="Mods", value=f"{score.mods!r}", inline=True)

                        if prev_n1:
                            if score.player.id != prev_n1["id"]:
                                embed.add_field(
                                name="Previous #1",
                                value=f"[{prev_n1['name']}](https://{app.settings.DOMAIN}/u/{prev_n1['id']})",
                                inline=False,
                                )

                        embed.set_thumbnail(url=f"https://a.{app.settings.DOMAIN}/{score.player.id}")
                        webhook_url = app.settings.FIRST_PLACES_WEBHOOK
                        webhook = Webhook(url=webhook_url)
                        webhook.add_embed(embed)

                        asyncio.create_task(webhook.post())

                        if app.metrics.enabled:
                            app.metrics.increment("ex_first_place_webhook")


            # this score is our best score.
            # Lock + reset WSZYSTKICH BEST/SCORE_BEST + INSERT atomowo.
            pp_db = pp_to_db(score.pp)
            async with app.state.services.database.transaction():
                await app.state.services.database.fetch_one(
                    "SELECT id FROM users WHERE id = :user_id FOR UPDATE",
                    {"user_id": score.player.id},
                )
                await app.state.services.database.execute(
                    "UPDATE scores SET status = 1 "
                    "WHERE status IN (2, 3) AND map_md5 = :map_md5 "
                    "AND userid = :user_id AND mode = :mode",
                    {
                        "map_md5": score.bmap.md5,
                        "user_id": score.player.id,
                        "mode": score.mode,
                    },
                )
                if score.prev_best and score.prev_best.status == SubmissionStatus.SCORE_BEST:
                    await app.state.services.database.execute(
                        "UPDATE scores SET status = 3 WHERE id = :id",
                        {"id": score.prev_best.id},
                    )
                score.id = await app.state.services.database.execute(
                    "INSERT INTO scores "
                    "VALUES (NULL, "
                    ":map_md5, :score, :pp, :acc, "
                    ":max_combo, :mods, :n300, :n100, "
                    ":n50, :nmiss, :ngeki, :nkatu, "
                    ":grade, :status, :mode, :play_time, "
                    ":time_elapsed, :client_flags, :user_id, :perfect, "
                    ":checksum)",
                    {
                "map_md5": score.bmap.md5,
                "score": score.score,
                "pp": pp_db,
                "acc": score.acc,
                "max_combo": score.max_combo,
                "mods": score.mods,
                "n300": score.n300,
                "n100": score.n100,
                "n50": score.n50,
                "nmiss": score.nmiss,
                "ngeki": score.ngeki,
                "nkatu": score.nkatu,
                "grade": score.grade.name,
                "status": score.status,
                "mode": score.mode,
                "play_time": score.server_time,
                "time_elapsed": score.time_elapsed,
                "client_flags": score.client_flags,
                "user_id": score.player.id,
                "perfect": score.perfect,
                "checksum": score.client_checksum,
            },
        )

            if score.status == SubmissionStatus.SCORE_BEST:
                # Zdegraduj stary SCORE_BEST do SUBMITTED.
                await app.state.services.database.execute(
                    "UPDATE scores SET status = 1 "
                    "WHERE status = 3 AND map_md5 = :map_md5 "
                    "AND userid = :user_id AND mode = :mode",
                    {
                        "map_md5": score.bmap.md5,
                        "user_id": score.player.id,
                        "mode": score.mode,
                    },
                )

        if not score.id:
            pp_db = pp_to_db(score.pp)
            score.id = await app.state.services.database.execute(
                "INSERT INTO scores "
                "VALUES (NULL, "
                ":map_md5, :score, :pp, :acc, "
                ":max_combo, :mods, :n300, :n100, "
                ":n50, :nmiss, :ngeki, :nkatu, "
                ":grade, :status, :mode, :play_time, "
                ":time_elapsed, :client_flags, :user_id, :perfect, "
                ":checksum)",
                {
                    "map_md5": score.bmap.md5,
                    "score": score.score,
                    "pp": pp_db,
                    "acc": score.acc,
                    "max_combo": score.max_combo,
                    "mods": score.mods,
                    "n300": score.n300,
                    "n100": score.n100,
                    "n50": score.n50,
                    "nmiss": score.nmiss,
                    "ngeki": score.ngeki,
                    "nkatu": score.nkatu,
                    "grade": score.grade.name,
                    "status": score.status,
                    "mode": score.mode,
                    "play_time": score.server_time,
                    "time_elapsed": score.time_elapsed,
                    "client_flags": score.client_flags,
                    "user_id": score.player.id,
                    "perfect": score.perfect,
                    "checksum": score.client_checksum,
                },
            )

        await app.state.services.redis.publish("ex:submit", score.toJSON())
        
    if score.passed:
        replay_data = await replay_file.read()

        MIN_REPLAY_SIZE = 24

        if len(replay_data) >= MIN_REPLAY_SIZE:
            replay_disk_file = REPLAYS_PATH / f"{score.id}.osr"
            replay_disk_file.write_bytes(replay_data)
        else:
            log(f"{score.player} submitted a score without a replay!", Ansi.LRED)

            if not score.player.restricted:
                await score.player.restrict(
                    admin=app.state.sessions.bot,
                    reason="submitted score with no replay",
                )
                if score.player.is_online:
                    score.player.logout()

    """ Update the user's & beatmap's stats """

    # get the current stats, and take a
    # shallow copy for the response charts.
    stats = score.player.stats[score.mode]
    prev_stats = copy.copy(stats)

    # stuff update for all submitted scores
    stats.playtime += score.time_elapsed // 1000
    stats.plays += 1
    stats.tscore += score.score
    stats.total_hits += score.n300 + score.n100 + score.n50

    if score.mode.as_vanilla in (1, 3):
        # taiko uses geki & katu for hitting big notes with 2 keys
        # mania uses geki & katu for rainbow 300 & 200
        stats.total_hits += score.ngeki + score.nkatu

    stats_updates: dict[str, Any] = {
        "plays": stats.plays,
        "playtime": stats.playtime,
        "tscore": stats.tscore,
        "total_hits": stats.total_hits,
    }

    if score.passed and score.bmap.has_leaderboard:
        # player passed & map is ranked, approved, or loved.

        if score.max_combo > stats.max_combo:
            stats.max_combo = score.max_combo
            stats_updates["max_combo"] = stats.max_combo

        if score.bmap.awards_ranked_pp and score.status == SubmissionStatus.BEST:
            # map is ranked or approved, and it's our (new)
            # best score on the map. update the player's
            # ranked score, grades, pp, acc and global rank.

            additional_rscore = score.score
            if score.prev_best:
                # we previously had a score, so remove
                # it's score from our ranked score.
                additional_rscore -= score.prev_best.score

                if score.grade != score.prev_best.grade:
                    if score.grade >= Grade.A:
                        stats.grades[score.grade] += 1
                        grade_col = format(score.grade, "stats_column")
                        stats_updates[grade_col] = stats.grades[score.grade]

                    if score.prev_best.grade >= Grade.A:
                        stats.grades[score.prev_best.grade] -= 1
                        grade_col = format(score.prev_best.grade, "stats_column")
                        stats_updates[grade_col] = stats.grades[score.prev_best.grade]
            else:
                # this is our first submitted score on the map
                if score.grade >= Grade.A:
                    stats.grades[score.grade] += 1
                    grade_col = format(score.grade, "stats_column")
                    stats_updates[grade_col] = stats.grades[score.grade]

            stats.rscore += additional_rscore
            stats_updates["rscore"] = stats.rscore

            # fetch scores sorted by pp for total acc/pp calc
            # NOTE: we select all plays (and not just top100)
            # because bonus pp counts the total amount of ranked
            # scores. I'm aware this scales horribly, and it'll
            # likely be split into two queries in the future.
            best_scores = await app.state.services.database.fetch_all(
                "SELECT s.pp, s.acc FROM scores s "
                "INNER JOIN maps m ON s.map_md5 = m.md5 "
                "WHERE s.userid = :user_id AND s.mode = :mode "
                "AND s.status = 2 AND m.status IN (2, 3) "  # ranked, approved
                "ORDER BY s.pp DESC",
                {"user_id": score.player.id, "mode": score.mode},
            )

            # calculate new total weighted accuracy
            weighted_acc = sum(float(row["acc"]) * 0.95**i for i, row in enumerate(best_scores))
            bonus_acc = 100.0 / (20 * (1 - 0.95 ** len(best_scores)))
            stats.acc = (weighted_acc * bonus_acc) / 100
            stats_updates["acc"] = stats.acc

            # calculate new total weighted pp
            weighted_pp = sum(pp_to_float(row["pp"]) * 0.95**i for i, row in enumerate(best_scores))
            bonus_pp = 416.6667 * (1 - 0.9994 ** len(best_scores))
            stats.pp = round(weighted_pp + bonus_pp)
            stats_updates["pp"] = stats.pp

            # update global & country ranking
            stats.rank = await score.player.update_rank(score.mode)

    await stats_repo.partial_update(
        score.player.id,
        score.mode.value,
        plays=stats_updates.get("plays", UNSET),
        playtime=stats_updates.get("playtime", UNSET),
        tscore=stats_updates.get("tscore", UNSET),
        total_hits=stats_updates.get("total_hits", UNSET),
        max_combo=stats_updates.get("max_combo", UNSET),
        xh_count=stats_updates.get("xh_count", UNSET),
        x_count=stats_updates.get("x_count", UNSET),
        sh_count=stats_updates.get("sh_count", UNSET),
        s_count=stats_updates.get("s_count", UNSET),
        a_count=stats_updates.get("a_count", UNSET),
        rscore=stats_updates.get("rscore", UNSET),
        acc=stats_updates.get("acc", UNSET),
        pp=stats_updates.get("pp", UNSET),
    )

    if not score.player.restricted:
        # enqueue new stats info to all other users
        app.state.sessions.players.enqueue(app.packets.user_stats(score.player))

        # update beatmap with new stats
        score.bmap.plays += 1
        if score.passed:
            score.bmap.passes += 1

        await app.state.services.database.execute(
            "UPDATE maps SET plays = :plays, passes = :passes WHERE md5 = :map_md5",
            {
                "plays": score.bmap.plays,
                "passes": score.bmap.passes,
                "map_md5": score.bmap.md5,
            },
        )

    # update their recent score
    score.player.recent_scores[score.mode] = score

    """ score submission charts """

    # charts are only displayed for passes vanilla gamemodes.
    if not score.passed:  # TODO: check if this is correct
        response = b"error: no"
    else:
        # construct and send achievements & ranking charts to the client
        if score.bmap.awards_ranked_pp and not score.player.restricted:
            unlocked_achievements: list[Achievement] = []

            locked_achievements = await achievements_usecases.fetch_user_locked(user_id=score.player.id)

            for server_achievement in locked_achievements:
                achievement_condition = server_achievement["cond"]
                if achievement_condition(score, score.mode.as_vanilla):
                    await user_achievements_usecases.create(
                        score.player.id,
                        server_achievement["id"],
                    )
                    unlocked_achievements.append(server_achievement)

            achievements_str = "/".join(
                format_achievement_string(a["file"], a["name"], a["desc"])
                for a in unlocked_achievements
            )
        else:
            achievements_str = ""

        # create score submission charts for osu! client to display

        if score.prev_best:
            beatmap_ranking_chart_entries = (
                chart_entry("rank", score.prev_best.rank, score.rank),
                chart_entry("rankedScore", score.prev_best.score, score.score),
                chart_entry("totalScore", score.prev_best.score, score.score),
                chart_entry("maxCombo", score.prev_best.max_combo, score.max_combo),
                chart_entry(
                    "accuracy",
                    round(score.prev_best.acc, 2),
                    round(score.acc, 2),
                ),
                chart_entry("pp", score.prev_best.pp, score.pp),
            )
        else:
            # no previous best score
            beatmap_ranking_chart_entries = (
                chart_entry("rank", None, score.rank),
                chart_entry("rankedScore", None, score.score),
                chart_entry("totalScore", None, score.score),
                chart_entry("maxCombo", None, score.max_combo),
                chart_entry("accuracy", None, round(score.acc, 2)),
                chart_entry("pp", None, score.pp),
            )

        overall_ranking_chart_entries = (
            chart_entry("rank", prev_stats.rank, stats.rank),
            chart_entry("rankedScore", prev_stats.rscore, stats.rscore),
            chart_entry("totalScore", prev_stats.tscore, stats.tscore),
            chart_entry("maxCombo", prev_stats.max_combo, stats.max_combo),
            chart_entry("accuracy", round(prev_stats.acc, 2), round(stats.acc, 2)),
            chart_entry("pp", prev_stats.pp, stats.pp),
        )

        submission_charts = [
            # beatmap info chart
            f"beatmapId:{score.bmap.id}",
            f"beatmapSetId:{score.bmap.set_id}",
            f"beatmapPlaycount:{score.bmap.plays}",
            f"beatmapPasscount:{score.bmap.passes}",
            f"approvedDate:{score.bmap.last_update}",
            "\n",
            # beatmap ranking chart
            "chartId:beatmap",
            f"chartUrl:{score.bmap.set.url}",
            "chartName:Beatmap Ranking",
            *beatmap_ranking_chart_entries,
            f"onlineScoreId:{score.id}",
            "\n",
            # overall ranking chart
            "chartId:overall",
            f"chartUrl:https://{app.settings.DOMAIN}/u/{score.player.id}",
            "chartName:Overall Ranking",
            *overall_ranking_chart_entries,
            f"achievements-new:{achievements_str}",
        ]

        response = "|".join(submission_charts).encode()

    log(
        f"[{score.mode!r}] {score.player} submitted a score! "
        f"({score.status!r}, {score.pp:,.2f}pp / {stats.pp:,}pp)",
        Ansi.LGREEN,
    )

    return Response(response)


@router.get("/web/osu-getreplay.php")
async def getReplay(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    mode: int = Query(..., alias="m", ge=0, le=3),
    score_id: int = Query(..., alias="c", min=0, max=9_223_372_036_854_775_807),
) -> Response:
    score = await Score.from_sql(score_id)
    if not score:
        return Response(b"", status_code=404)

    file = REPLAYS_PATH / f"{score_id}.osr"
    if not file.exists():
        return Response(b"", status_code=404)

    # increment replay views for this score
    if score.player is not None and player.id != score.player.id:
        app.state.loop.create_task(score.increment_replay_views())  # type: ignore[unused-awaitable]

    return FileResponse(file)


@router.get("/web/osu-rate.php")
async def osuRate(
    player: Player = Depends(
        authenticate_player_session(Query, "u", "p", err=b"auth fail"),
    ),
    map_md5: str = Query(..., alias="c", min_length=32, max_length=32),
    rating: int | None = Query(None, alias="v", ge=1, le=10),
) -> Response:
    if rating is None:
        # check if we have the map in our cache;
        # if not, the map probably doesn't exist.
        if map_md5 not in app.state.cache.beatmap:
            return Response(b"no exist")

        cached = app.state.cache.beatmap[map_md5]

        # only allow rating on maps with a leaderboard.
        if cached.status < RankedStatus.Ranked:
            return Response(b"not ranked")

        # osu! client is checking whether we can rate the map or not.
        # the client hasn't rated the map, so simply
        # tell them that they can submit a rating.
        if not await ratings_repo.fetch_one(map_md5=map_md5, userid=player.id):
            return Response(b"ok")
    else:
        # the client is submitting a rating for the map.
        await ratings_repo.create(userid=player.id, map_md5=map_md5, rating=rating)

    # send back the average rating
    avg = await ratings_repo.get_map_rating(map_md5=map_md5)
    return Response(f"alreadyvoted\n{avg}".encode())


@unique
@pymysql_encode(escape_enum)
class LeaderboardType(IntEnum):
    Local = 0
    Top = 1
    Mods = 2
    Friends = 3
    Country = 4


async def get_leaderboard_scores(
    leaderboard_type: LeaderboardType | int,
    map_md5: str,
    mode: int,
    mods: Mods,
    player: Player,
    scoring_metric: Literal["pp", "score"],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    query = [
        f"SELECT s.id, s.{scoring_metric} AS _score, "
        "s.max_combo, s.n50, s.n100, s.n300, "
        "s.nmiss, s.nkatu, s.ngeki, s.perfect, s.mods, "
        "UNIX_TIMESTAMP(s.play_time) time, u.id userid, "
        "COALESCE(CONCAT('[', c.tag, '] ', u.name), u.name) AS name "
        "FROM scores s "
        "INNER JOIN users u ON u.id = s.userid "
        "LEFT JOIN clans c ON c.id = u.clan_id "
        "WHERE s.map_md5 = :map_md5 AND s.status IN (2, 3) "  # 2: best pp, 3: best score
"AND NOT (s.status = 2 AND EXISTS (SELECT 1 FROM scores s2 WHERE s2.userid = s.userid AND s2.map_md5 = s.map_md5 AND s2.mode = s.mode AND s2.status = 3)) "
        "AND (u.priv & 1 OR u.id = :user_id) AND mode = :mode",
    ]

    params: dict[str, Any] = {
        "map_md5": map_md5,
        "user_id": player.id,
        "mode": mode,
    }

    if leaderboard_type == LeaderboardType.Mods:
        query.append("AND s.mods = :mods")
        params["mods"] = mods
    elif leaderboard_type == LeaderboardType.Friends:
        query.append("AND s.userid IN :friends")
        params["friends"] = player.friends | {player.id}
    elif leaderboard_type == LeaderboardType.Country:
        query.append("AND u.country = :country")
        params["country"] = player.geoloc["country"]["acronym"]

    # TODO: customizability of the number of scores
    query.append("ORDER BY _score DESC LIMIT 50")

    score_rows = await app.state.services.database.fetch_all(
        " ".join(query),
        params,
    )

    if score_rows:  # None or []
        # fetch player's personal best score
        personal_best_score_row = await app.state.services.database.fetch_one(
            f"SELECT id, {scoring_metric} AS _score, "
            "max_combo, n50, n100, n300, "
            "nmiss, nkatu, ngeki, perfect, mods, "
            "UNIX_TIMESTAMP(play_time) time "
            "FROM scores "
            "WHERE map_md5 = :map_md5 AND mode = :mode "
            "AND userid = :user_id AND status IN (2, 3) "
            "ORDER BY _score DESC LIMIT 1",
            {"map_md5": map_md5, "mode": mode, "user_id": player.id},
        )

        if personal_best_score_row is not None:
            # calculate the rank of the score.
            p_best_rank = 1 + await app.state.services.database.fetch_val(
                "SELECT COUNT(*) FROM scores s "
                "INNER JOIN users u ON u.id = s.userid "
                "WHERE s.map_md5 = :map_md5 AND s.mode = :mode "
                "AND s.status = 2 AND u.priv & 1 "
                f"AND s.{scoring_metric} > :score",
                {
                    "map_md5": map_md5,
                    "mode": mode,
                    "score": personal_best_score_row["_score"],
                },
                column=0,  # COUNT(*)
            )

            # attach rank to personal best row
            personal_best_score_row["rank"] = p_best_rank
    else:
        score_rows = []
        personal_best_score_row = None

    return score_rows, personal_best_score_row


SCORE_LISTING_FMTSTR = (
    "{id}|{name}|{score}|{max_combo}|"
    "{n50}|{n100}|{n300}|{nmiss}|{nkatu}|{ngeki}|"
    "{perfect}|{mods}|{userid}|{rank}|{time}|{has_replay}"
)


@router.get("/web/osu-osz2-getscores.php")
async def getScores(
    player: Player = Depends(authenticate_player_session(Query, "us", "ha")),
    requesting_from_editor_song_select: bool = Query(..., alias="s"),
    leaderboard_version: int = Query(..., alias="vv"),
    leaderboard_type: int = Query(..., alias="v", ge=0, le=4),
    map_md5: str = Query(..., alias="c", min_length=32, max_length=32),
    map_filename: str = Query(..., alias="f"),
    mode_arg: int = Query(..., alias="m", ge=0, le=3),
    map_set_id: int = Query(..., alias="i", ge=-1, le=2_147_483_647),
    mods_arg: int = Query(..., alias="mods", ge=0, le=2_147_483_647),
    map_package_hash: str = Query(..., alias="h"),  # TODO: further validation
    aqn_files_found: bool = Query(..., alias="a"),
) -> Response:
    if aqn_files_found:
        stacktrace = app.utils.get_appropriate_stacktrace()
        await app.state.services.log_strange_occurrence(stacktrace)
    # check if this md5 has already been  cached as
    # unsubmitted/needs update to reduce osu!api spam
    if map_md5 in app.state.cache.needs_update:
        return Response(b"1|false")
    if map_md5 in app.state.cache.unsubmitted:
        # DEBUG: log when BSS maps hit unsubmitted cache
        if map_set_id >= BSS_ID_OFFSET:
            log(f"[BSS-DEBUG] md5={map_md5} set={map_set_id} hit unsubmitted cache!", Ansi.LRED)
        return Response(b"-1|false")
    # Nt: unsubmitted cache check removed — let from_md5 re-check
    # every time so newly imported maps get picked up properly

    # DEBUG: log BSS map lookups
    if map_set_id >= BSS_ID_OFFSET:
        log(f"[BSS-DEBUG] getscores lookup: md5={map_md5} set={map_set_id} file={map_filename}", Ansi.LCYAN)

    if mods_arg & Mods.RELAX:
        if mode_arg == 3:  # rx!mania doesn't exist
            mods_arg &= ~Mods.RELAX
        else:
            mode_arg += 4
    elif mods_arg & Mods.AUTOPILOT:
        if mode_arg in (1, 2, 3):  # ap!catch, taiko and mania don't exist
            mods_arg &= ~Mods.AUTOPILOT
        else:
            mode_arg += 8

    mods = Mods(mods_arg)
    mode = GameMode(mode_arg)

    # attempt to update their stats if their
    # gm/gm-affecting-mods change at all.
    if mode != player.status.mode:
        player.status.mods = mods
        player.status.mode = mode

        if not player.restricted:
            app.state.sessions.players.enqueue(app.packets.user_stats(player))

    scoring_metric: Literal["pp", "score"] = (
        "pp" if mode >= GameMode.RELAX_OSU else "score"
    )

    bmap = await Beatmap.from_md5(map_md5, set_id=map_set_id)
    has_set_id = map_set_id > 0

    # BSS maps: client updates BeatmapID/SetID in the .osu file after
    # submission, which changes the md5. If lookup by md5 fails but
    # we have a BSS set_id, look up by set_id and fix the md5.
    if not bmap and map_set_id >= BSS_ID_OFFSET:
        bss_maps = await maps_repo.fetch_many(set_id=map_set_id)
        if bss_maps:
            already_exists = next((m for m in bss_maps if m["md5"] == map_md5), None)
            if already_exists:
                bmap = await Beatmap.from_md5(map_md5, set_id=map_set_id)
            else:
                target = bss_maps[0]
                log(
                    f"[BSS] md5 mismatch fix: {target['md5']} -> {map_md5} "
                    f"for map {target['id']}",
                    Ansi.LCYAN,
                )
                await maps_repo.partial_update(id=target["id"], md5=map_md5)

                # Clear old md5 from unsubmitted cache if present
                if target["md5"] in app.state.cache.unsubmitted:
                    app.state.cache.unsubmitted.discard(target["md5"])

                # Evict the old beatmapset from cache so it reloads
                if map_set_id in app.state.cache.beatmapset:
                    del app.state.cache.beatmapset[map_set_id]

            # Retry lookup with the now-corrected md5
            bmap = await Beatmap.from_md5(map_md5, set_id=map_set_id)

    if not bmap:
        # DEBUG: log BSS map lookup failures
        if map_set_id >= BSS_ID_OFFSET:
            db_rec = await maps_repo.fetch_one(id=map_set_id)
            log(
                f"[BSS-DEBUG] from_md5 returned None! "
                f"client_md5={map_md5} set={map_set_id} "
                f"db_md5={db_rec['md5'] if db_rec else 'NOT IN DB'}",
                Ansi.LRED,
            )
        # map not found, figure out whether it needs an
        # update or isn't submitted using its filename.

        if has_set_id and map_set_id not in app.state.cache.beatmapset:
            # set not cached, it doesn't exist
            log(f"[BSS-DEBUG] adding to unsubmitted (set not cached): md5={map_md5} set={map_set_id}", Ansi.LYELLOW)
            app.state.cache.unsubmitted.add(map_md5)
            return Response(b"-1|false")

        map_filename = unquote_plus(map_filename)  # TODO: is unquote needed?

        map_exists = False
        if has_set_id:
            # we can look it up in the specific set from cache
            for bmap in app.state.cache.beatmapset[map_set_id].maps:
                if map_filename == bmap.filename:
                    map_exists = True
                    break
            else:
                map_exists = False
        else:
            # we can't find it on the osu!api by md5,
            # and we don't have the set id, so we must
            # look it up in sql from the filename.
            map_exists = (
                await maps_repo.fetch_one(
                    filename=map_filename,
                )
                is not None
            )

        if map_exists:
            # map can be updated.
            app.state.cache.needs_update.add(map_md5)
            return Response(b"1|false")
        else:
            # map is unsubmitted.
            # add this map to the unsubmitted cache, so
            # that we don't have to make this request again.
            log(f"[BSS-DEBUG] adding to unsubmitted (not found): md5={map_md5} set={map_set_id} file={map_filename}", Ansi.LYELLOW)
            app.state.cache.unsubmitted.add(map_md5)
            return Response(b"-1|false")

    # we've found a beatmap for the request.

    if app.state.services.datadog:
        app.state.services.datadog.increment("bancho.leaderboards_served")  # type: ignore[no-untyped-call]

    if app.metrics.enabled:
        app.metrics.increment("ex_leaderboards_served")

    if bmap.status < RankedStatus.Ranked:
        # only show leaderboards for ranked,
        # approved, qualified, or loved maps.
        return Response(f"{int(bmap.status)}|false".encode())

    # fetch scores & personal best
    # TODO: create a leaderboard cache
    if not requesting_from_editor_song_select:
        score_rows, personal_best_score_row = await get_leaderboard_scores(
            leaderboard_type,
            bmap.md5,
            mode,
            mods,
            player,
            scoring_metric,
        )
    else:
        score_rows = []
        personal_best_score_row = None

    # fetch beatmap rating
    map_avg_rating = await ratings_repo.get_map_rating(map_md5=map_md5)

    ## construct response for osu! client

    response_lines: list[str] = [
        # NOTE: fa stands for featured artist (for the ones that may not know)
        # {ranked_status}|{serv_has_osz2}|{bid}|{bsid}|{len(scores)}|{fa_track_id}|{fa_license_text}
        f"{int(bmap.status)}|false|{bmap.id}|{bmap.set_id}|{len(score_rows)}|0|",
        # {offset}\n{beatmap_name}\n{rating}
        # TODO: server side beatmap offsets
        f"0\n{bmap.full_name}\n{map_avg_rating}",
    ]

    if not score_rows:
        response_lines.extend(("", ""))  # no scores, no personal best
        return Response("\n".join(response_lines).encode())

    if personal_best_score_row is not None:
        user_clan = (
            await clans_repo.fetch_one(id=player.clan_id)
            if player.clan_id is not None
            else None
        )
        display_name = (
            f"[{user_clan['tag']}] {player.name}"
            if user_clan is not None
            else player.name
        )
        response_lines.append(
            SCORE_LISTING_FMTSTR.format(
                **personal_best_score_row,
                name=display_name,
                userid=player.id,
                score=int(round(personal_best_score_row["_score"])),
                has_replay="1",
            ),
        )
    else:
        response_lines.append("")

    response_lines.extend(
        [
            SCORE_LISTING_FMTSTR.format(
                **s,
                score=int(round(s["_score"])),
                has_replay="1",
                rank=idx + 1,
            )
            for idx, s in enumerate(score_rows)
        ],
    )

    return Response("\n".join(response_lines).encode())


@router.post("/web/osu-comment.php")
async def osuComment(
    player: Player = Depends(authenticate_player_session(Form, "u", "p")),
    map_id: int = Form(..., alias="b"),
    map_set_id: int = Form(..., alias="s"),
    score_id: int = Form(..., alias="r", ge=0, le=9_223_372_036_854_775_807),
    mode_vn: int = Form(..., alias="m", ge=0, le=3),
    action: Literal["get", "post"] = Form(..., alias="a"),
    # only sent for post
    target: Literal["song", "map", "replay"] | None = Form(None),
    colour: str | None = Form(None, alias="f", min_length=6, max_length=6),
    start_time: int | None = Form(None, alias="starttime"),
    comment: str | None = Form(None, min_length=1, max_length=80),
) -> Response:
    if action == "get":
        # client is requesting all comments
        comments = await comments_repo.fetch_all_relevant_to_replay(
            score_id=score_id,
            map_set_id=map_set_id,
            map_id=map_id,
        )

        ret: list[str] = []

        for cmt in comments:
            # note: this implementation does not support
            #       "player" or "creator" comment colours
            if cmt["priv"] & Privileges.NOMINATOR:
                fmt = "bat"
            elif cmt["priv"] & Privileges.DONATOR:
                fmt = "supporter"
            else:
                fmt = ""

            if cmt["colour"]:
                fmt += f'|{cmt["colour"]}'

            ret.append(
                "{time}\t{target_type}\t{fmt}\t{comment}".format(fmt=fmt, **cmt),
            )

        player.update_latest_activity_soon()
        return Response("\n".join(ret).encode())

    elif action == "post":
        # client is submitting a new comment

        # validate all required params are provided
        assert target is not None
        assert start_time is not None
        assert comment is not None

        # get the corresponding id from the request
        if target == "song":
            target_id = map_set_id
        elif target == "map":
            target_id = map_id
        else:  # target == "replay"
            target_id = score_id

        if colour and not player.priv & Privileges.DONATOR:
            # only supporters can use colours.
            colour = None

            log(
                f"User {player} attempted to use a coloured comment without "
                "supporter status. Submitting comment without a colour.",
            )

        # insert into sql
        await comments_repo.create(
            target_id=target_id,
            target_type=comments_repo.TargetType(target),
            userid=player.id,
            time=start_time,
            comment=comment,
            colour=colour,
        )

        player.update_latest_activity_soon()

    return Response(b"")  # empty resp is fine


@router.get("/web/osu-markasread.php")
async def osuMarkAsRead(
    player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    channel: str = Query(..., min_length=0, max_length=32),
) -> Response:
    target_name = unquote(channel)  # TODO: unquote needed?
    if not target_name:
        log(
            f"User {player} attempted to mark a channel as read without a target.",
            Ansi.LYELLOW,
        )
        return Response(b"")  # no channel specified

    target = await app.state.sessions.players.from_cache_or_sql(name=target_name)
    if target:
        # mark any unread mail from this user as read.
        await mail_repo.mark_conversation_as_read(
            to_id=player.id,
            from_id=target.id,
        )

    return Response(b"")


@router.get("/web/osu-getseasonal.php")
async def osuSeasonal() -> Response:
    return ORJSONResponse(app.settings.SEASONAL_BGS)


@router.get("/web/bancho_connect.php")
async def banchoConnect(
    # NOTE: this is disabled as this endpoint can be called
    #       before a player has been granted a session
    # player: Player = Depends(authenticate_player_session(Query, "u", "h")),
    osu_ver: str = Query(..., alias="v"),
    active_endpoint: str | None = Query(None, alias="fail"),
    net_framework_vers: str | None = Query(None, alias="fx"),  # delimited by |
    client_hash: str | None = Query(None, alias="ch"),
    retrying: bool | None = Query(None, alias="retry"),  # '0' or '1'
) -> Response:
    return Response(b"")


@router.get("/web/check-updates.php")
async def checkUpdates(
    request: Request,
    action: Literal["check", "path", "error"],
    stream: Literal["cuttingedge", "stable40", "beta40", "stable"],
) -> Response:
    return Response(b"")

# BANCHO.PY - REDIRECT PPY BUTTON TO SERVER PAGE
@router.get("/")
async def osuRoot() -> Response:
    return RedirectResponse(
        url=f"https://{app.settings.DOMAIN}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )

# BANCHO.PY - REDIRECTION FOR BEATMAPS, TOPICS, AVATARS FOR SHIINA
if app.settings.REDIRECT_OSU_URLS:

    async def osu_redirect_beatmaps(file_path: str) -> Response:
        return RedirectResponse(
            url=f"https://{app.settings.DOMAIN}/b/{file_path}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )

    async def osu_redirect_beatmapsets(file_path: str) -> Response:
        return RedirectResponse(
            url=f"https://{app.settings.DOMAIN}/beatmapset/{file_path}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )

    async def osu_redirect_topic(file_path: str, file_path_two: str) -> Response:
        if file_path_two == "":
            return RedirectResponse(
                url=f"https://{app.settings.DOMAIN}/beatmapset/{file_path}",
                status_code=status.HTTP_301_MOVED_PERMANENTLY,
            )
        return RedirectResponse(
            url=f"https://{app.settings.DOMAIN}/b/{file_path_two}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )

    async def profile_redirect(file_path: str) -> Response:
        return RedirectResponse(
            url=f"https://{app.settings.DOMAIN}/u/{file_path}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )

    async def avatar_edit_redirect() -> Response:
        return RedirectResponse(
            url=f"https://{app.settings.DOMAIN}/settings",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )

    # Register more specific routes first
    router.get("/beatmapsets/{file_path:path}/discussion/{file_path_two:path}")(osu_redirect_topic)
    router.get("/beatmaps/{file_path:path}")(osu_redirect_beatmaps)
    router.get("/beatmapsets/{file_path:path}")(osu_redirect_beatmapsets)
    router.get("/community/forums/topics/{file_path:path}")(osu_redirect_beatmaps)
    router.get("/users/{file_path:path}")(profile_redirect)
    router.get("/home/account/edit")(avatar_edit_redirect)

""" Misc handlers """


@router.get("/ss/{screenshot_id}.{extension}")
async def get_screenshot(
    screenshot_id: str = Path(..., pattern=r"[a-zA-Z0-9-_]{8}"),
    extension: Literal["jpg", "jpeg", "png"] = Path(...),
) -> Response:
    """Serve a screenshot from the server, by filename."""
    screenshot_path = SCREENSHOTS_PATH / f"{screenshot_id}.{extension}"

    if not screenshot_path.exists():
        return ORJSONResponse(
            content={"status": "Screenshot not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    if extension in ("jpg", "jpeg"):
        media_type = "image/jpeg"
    elif extension == "png":
        media_type = "image/png"
    else:
        media_type = None

    return FileResponse(
        path=screenshot_path,
        media_type=media_type,
    )


@router.get("/d/{map_set_id}")
async def get_osz(
    map_set_id: str = Path(...),
) -> Response:
    """Handle a map download request (osu.ppy.sh/d/*)."""
    no_video = map_set_id[-1] == "n"
    if no_video:
        map_set_id = map_set_id[:-1]

    try:
        set_id_int = int(map_set_id)
    except ValueError:
        set_id_int = 0

    # For BSS maps, build and serve .osz from local files
    if set_id_int >= BSS_ID_OFFSET:
        import zipfile
        import io

        maps = await maps_repo.fetch_many(set_id=set_id_int)
        if not maps:
            return Response(status_code=status.HTTP_404_NOT_FOUND)

        buf = io.BytesIO()
        has_files = False
        first_map = maps[0]
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for m in maps:
                osu_path = BEATMAPS_PATH / f"{m['id']}.osu"
                if osu_path.exists():
                    zf.writestr(m["filename"], osu_path.read_bytes())
                    has_files = True

            # Include audio/bg from submissions dir if available
            sub_dir = SUBMISSIONS_PATH / str(set_id_int)
            if sub_dir.exists():
                for f in sub_dir.iterdir():
                    if f.is_file() and "." in f.name and not f.name.endswith((".osz2", ".osz", ".osu")):
                        zf.writestr(f.name, f.read_bytes())

        if not has_files:
            return Response(status_code=status.HTTP_404_NOT_FOUND)

        osz_filename = (
            f"{first_map['artist']} - {first_map['title']} "
            f"({first_map['creator']}).osz"
        )

        return Response(
            content=buf.getvalue(),
            media_type="application/x-osu-beatmap-archive",
            headers={
                "Content-Disposition": f'attachment; filename="{osz_filename}"',
            },
        )

    query_str = f"{map_set_id}?n={int(not no_video)}"

    return RedirectResponse(
        url=f"{app.settings.MIRROR_DOWNLOAD_ENDPOINT}/{query_str}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )


@router.get("/api/v1/bss/bg/{set_id}")
async def bss_background(set_id: int = Path(...)) -> Response:
    """Serve background image for BSS maps."""
    if set_id < BSS_ID_OFFSET:
        return RedirectResponse(
            url=f"https://assets.ppy.sh/beatmaps/{set_id}/covers/cover.jpg",
        )
    sub_dir = SUBMISSIONS_PATH / str(set_id)
    if sub_dir.exists():
        for f in sub_dir.iterdir():
            if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                media = "image/jpeg" if f.suffix.lower() in (".jpg", ".jpeg") else "image/png"
                return Response(content=f.read_bytes(), media_type=media)
    return Response(status_code=404)

@router.get("/web/maps/{map_filename}")
async def get_updated_beatmap(
    request: Request,
    map_filename: str,
    host: str = Header(...),
) -> Response:
    """Send the latest .osu file the server has for a given map."""
    if host == "osu.ppy.sh":
        return Response("bancho.py only supports the -devserver connection method")

    # Check if we have this .osu file locally (BSS maps)
    # Try to find by filename in DB
    rec = await maps_repo.fetch_one(filename=map_filename)
    if rec is not None:
        osu_path = BEATMAPS_PATH / f"{rec['id']}.osu"
        if osu_path.exists():
            return Response(
                content=osu_path.read_bytes(),
                media_type="application/octet-stream",
            )

    return RedirectResponse(
        url=f"https://osu.ppy.sh{request['raw_path'].decode()}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )


@router.get("/p/doyoureallywanttoaskpeppy")
async def peppyDMHandler() -> Response:
    return Response(
        content=(
            b"This user's ID is usually peppy's (when on bancho), "
            b"and is blocked from being messaged by the osu! client."
        ),
    )


""" ingame registration """

INGAME_REGISTRATION_DISALLOWED_ERROR = {
    "form_error": {
        "user": {
            "password": [
                "In-game registration is disabled. Please register on the website.",
            ],
        },
    },
}


@router.post("/users")
async def register_account(
    request: Request,
    username: str = Form(..., alias="user[username]"),
    email: str = Form(..., alias="user[user_email]"),
    pw_plaintext: str = Form(..., alias="user[password]"),
    check: int = Form(...),
    # XXX: require/validate these headers; they are used later
    # on in the registration process for resolving geolocation
    forwarded_ip: str = Header(..., alias="X-Forwarded-For"),
    real_ip: str = Header(..., alias="X-Real-IP"),
) -> Response:
    if not all((username, email, pw_plaintext)):
        return Response(
            content=b"Missing required params",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Disable in-game registration if enabled
    if app.settings.DISALLOW_INGAME_REGISTRATION:
        return ORJSONResponse(
            content=INGAME_REGISTRATION_DISALLOWED_ERROR,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # ensure all args passed
    # are safe for registration.
    errors: Mapping[str, list[str]] = defaultdict(list)

    # Usernames must:
    # - be within 2-15 characters in length
    # - not contain both ' ' and '_', one is fine
    # - not be in the config's `disallowed_names` list
    # - not already be taken by another player
    if not regexes.USERNAME.match(username):
        errors["username"].append("Must be 2-15 characters in length.")

    if "_" in username and " " in username:
        errors["username"].append('May contain "_" and " ", but not both.')

    if username in app.settings.DISALLOWED_NAMES:
        errors["username"].append("Disallowed username; pick another.")

    if "username" not in errors:
        if await users_repo.fetch_one(name=username):
            errors["username"].append("Username already taken by another player.")

    # Emails must:
    # - match the regex `^[^@\s]{1,200}@[^@\s\.]{1,30}\.[^@\.\s]{1,24}$`
    # - not already be taken by another player
    if not regexes.EMAIL.match(email):
        errors["user_email"].append("Invalid email syntax.")
    else:
        if await users_repo.fetch_one(email=email):
            errors["user_email"].append("Email already taken by another player.")

    # Passwords must:
    # - be within 8-32 characters in length
    # - have more than 3 unique characters
    # - not be in the config's `disallowed_passwords` list
    if not 8 <= len(pw_plaintext) <= 32:
        errors["password"].append("Must be 8-32 characters in length.")

    if len(set(pw_plaintext)) <= 3:
        errors["password"].append("Must have more than 3 unique characters.")

    if pw_plaintext.lower() in app.settings.DISALLOWED_PASSWORDS:
        errors["password"].append("That password was deemed too simple.")

    if errors:
        # we have errors to send back, send them back delimited by newlines.
        errors = {k: ["\n".join(v)] for k, v in errors.items()}
        errors_full = {"form_error": {"user": errors}}
        return ORJSONResponse(
            content=errors_full,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if check == 0:
        # the client isn't just checking values,
        # they want to register the account now.
        # make the md5 & bcrypt the md5 for sql.
        pw_md5 = hashlib.md5(pw_plaintext.encode()).hexdigest().encode()
        pw_bcrypt = bcrypt.hashpw(pw_md5, bcrypt.gensalt())
        _ck = hashlib.sha256(pw_bcrypt + b":" + pw_md5).digest()
        app.state.cache.bcrypt[_ck] = True  # cache result for login

        ip = app.state.services.ip_resolver.get_ip(request.headers)

        geoloc = await app.state.services.fetch_geoloc(ip, request.headers)
        country = geoloc["country"]["acronym"] if geoloc is not None else "XX"

        async with app.state.services.database.transaction():
            # add to `users` table.
            player = await users_repo.create(
                name=username,
                email=email,
                pw_bcrypt=pw_bcrypt,
                country=country,
            )

            # add to `stats` table.
            await stats_repo.create_all_modes(player_id=player["id"])

        if app.state.services.datadog:
            app.state.services.datadog.increment("bancho.registrations")  # type: ignore[no-untyped-call]

        if(app.metrics.enabled):
            app.metrics.increment("ex_registrations")

        log(f"<{username} ({player['id']})> has registered!", Ansi.LGREEN)

    return Response(content=b"ok")  # success


@router.post("/difficulty-rating")
async def difficultyRatingHandler(request: Request) -> Response:
    return RedirectResponse(
        url=f"https://osu.ppy.sh{request['path']}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )
