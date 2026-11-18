"""Atomic edits of the routing tables in the user's existing TOML profile."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import math
import os
from pathlib import Path
import stat
import tempfile
from uuid import uuid4

import tomlkit

from .config import AIRPLAY_MODES, Config, parse_config

_REVISION_MARKER = b"# OpenSurround web revision "


class RevisionConflict(Exception):
    pass


class NoPreviousRevision(Exception):
    pass


@dataclass(frozen=True)
class Revision:
    id: str
    config: Config
    can_restore: bool


def _digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def _replace(path: Path, contents: bytes, mode: int = 0o600) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".opensurround-")
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), mode)
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class RouteStore:
    def __init__(self, path: str | Path, channels: int):
        self.path = Path(path).resolve()
        self.channels = channels
        self.previous = self.path.with_name(self.path.name + ".previous")
        self.pending = self.path.with_name(self.path.name + ".pending")
        self.lock = self.path.with_name(self.path.name + ".lock")

    def _sync_directory(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self):
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.path.read_bytes()
            if self.pending.exists():
                # The staged backup belongs to the new revision only if the
                # atomic replacement of the profile actually happened.
                target = self.pending.read_bytes().partition(b"\n")[0]
                if target == _digest(current).encode("ascii"):
                    os.replace(self.pending, self.previous)
                else:
                    self.pending.unlink()
                self._sync_directory()
            yield current

    def _snapshot(self, raw: bytes) -> Revision:
        return Revision(_digest(raw), parse_config(raw.decode("utf-8"), allow_empty=True),
                        self.previous.is_file())

    def read(self) -> Revision:
        with self._locked() as current:
            return self._snapshot(current)

    def save(self, expected: str, rows: list) -> Revision:
        with self._locked() as current:
            snapshot = self._snapshot(current)
            if expected != snapshot.id:
                raise RevisionConflict
            doc = tomlkit.parse(current.decode("utf-8"))
            if "endpoints" not in doc:
                doc["endpoints"] = tomlkit.table()
            tables = doc["endpoints"]
            original = {ep.name: ep for ep in snapshot.config.endpoints}
            keys = set()
            devices = set()
            if not isinstance(rows, list):
                raise ValueError("Az útvonalak listája hibás.")
            for row in rows:
                key, fields = self._validate_row(row, original)
                if key in keys or (fields["client_id"] and fields["client_id"] in devices):
                    raise ValueError("Egy eszközhöz csak egy útvonal adható meg.")
                keys.add(key)
                if fields["client_id"]:
                    devices.add(fields["client_id"])
                if key not in tables:
                    tables[key] = tomlkit.inline_table() if isinstance(tables, tomlkit.items.InlineTable) else tomlkit.table()
                table = tables[key]
                for field, value in fields.items():
                    if field in ("match_name", "client_id") and not value:
                        table.pop(field, None)
                    elif field == "airplay_mode" and fields["transport"] != "airplay":
                        table.pop(field, None)
                    elif field == "transport" and field not in table and value == "sendspin":
                        continue
                    elif field in table and table[field] == value:
                        continue
                    elif field == "label" and field not in table and value == key:
                        continue
                    elif field in ("gain_db", "delay_ms", "muted") and field not in table and not value:
                        continue
                    else:
                        table[field] = value
            for key in original.keys() - keys:
                del tables[key]
            return self._write(current, tomlkit.dumps(doc).encode("utf-8"))

    def restore(self, expected: str) -> Revision:
        with self._locked() as current:
            if expected != _digest(current):
                raise RevisionConflict
            if not self.previous.is_file():
                raise NoPreviousRevision
            previous = self.previous.read_bytes().partition(b"\n")[2]
            old = tomlkit.parse(previous.decode("utf-8"))
            doc = tomlkit.parse(current.decode("utf-8"))
            if "endpoints" in old:
                doc["endpoints"] = old["endpoints"]
            else:
                doc.pop("endpoints", None)
            return self._write(current, tomlkit.dumps(doc).encode("utf-8"))

    def _write(self, current: bytes, updated: bytes) -> Revision:
        if updated == current:
            return self._snapshot(current)
        # Revisions stay distinct even when a restore returns to older values.
        if updated.startswith(_REVISION_MARKER):
            updated = updated.partition(b"\n")[2]
        updated = _REVISION_MARKER + uuid4().hex.encode("ascii") + b"\n" + updated
        snapshot = self._snapshot(updated)
        for ep in snapshot.config.endpoints:
            if any(ch < 0 or ch >= self.channels for ch in ep.source_channels):
                raise ValueError("Az útvonal a bemeneten nem elérhető csatornát használ.")

        mode = stat.S_IMODE(self.path.stat().st_mode)
        _replace(self.pending, snapshot.id.encode("ascii") + b"\n" + current)
        self._sync_directory()
        _replace(self.path, updated, mode)
        self._sync_directory()
        os.replace(self.pending, self.previous)
        self._sync_directory()
        return Revision(snapshot.id, snapshot.config, True)

    def _validate_row(self, row: dict, original: dict) -> tuple[str, dict]:
        if not isinstance(row, dict):
            raise ValueError("Az útvonal adatai hibásak.")
        key = row.get("key")
        if key is None:
            key = uuid4().hex
        elif not isinstance(key, str) or key not in original:
            raise ValueError("Ismeretlen útvonal.")
        label = row.get("label")
        match_name = row.get("match_name", "")
        client_id = row.get("client_id", "")
        old = original.get(key)
        transport = row.get("transport", old.transport if old else "sendspin")
        mode = row.get("airplay_mode", old.airplay_mode if old else "auto")
        if transport not in ("sendspin", "airplay") or mode not in AIRPLAY_MODES:
            raise ValueError("Érvénytelen kimenet vagy AirPlay-mód.")
        if (not isinstance(label, str) or not label.strip() or
                not isinstance(match_name, str) or not isinstance(client_id, str) or
                not (client_id.strip() or (transport == "sendspin" and match_name.strip()))):
            raise ValueError("Adj meg nevet és válassz egy eszközt.")
        sources = row.get("source_channels")
        if (not isinstance(sources, list) or len(sources) not in (1, 2) or
                any(type(ch) is not int or not 0 <= ch < self.channels for ch in sources)):
            raise ValueError("Válassz egy vagy két elérhető bemeneti csatornát.")
        gain = row.get("gain_db")
        delay = row.get("delay_ms")
        muted = row.get("muted")
        if (type(gain) not in (int, float) or
                not (-120 <= gain <= 120 or old and gain == old.gain_db and math.isfinite(gain)) or
                type(delay) not in (int, float) or
                not (0 <= delay <= 10_000 or old and delay == old.delay_ms and math.isfinite(delay)) or
                type(muted) is not bool):
            raise ValueError("Az erősítés −120 és +120 dB, a késleltetés 0 és 10 000 ms között lehet.")
        return key, dict(label=label, match_name=match_name if transport == "sendspin" else "",
                         client_id=client_id, transport=transport, airplay_mode=mode,
                         source_channels=sources, gain_db=gain, delay_ms=delay, muted=muted)
