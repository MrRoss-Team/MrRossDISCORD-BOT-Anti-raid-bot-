from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import discord

from .state import serialize_guild

KEEP = 48
_NAME_RE = re.compile(r"^[0-9_\-]+$")


def _digest(data: dict) -> str:
    body = {k: v for k, v in data.items() if k != "taken_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._last_digest: dict[int, str] = {}
        self._cache: dict[tuple[int, str], dict] = {}

    def _dir(self, guild_id: int) -> Path:
        return self.root / str(guild_id) / "snapshots"

    def list(self, guild_id: int) -> list[Path]:
        folder = self._dir(guild_id)
        if not folder.exists():
            return []
        return sorted(folder.glob("*.json"), reverse=True)

    def load(self, guild_id: int, name: str) -> Optional[dict]:
        if not _NAME_RE.match(name):
            return None
        key = (guild_id, name)
        if key not in self._cache:
            path = self._dir(guild_id) / f"{name}.json"
            try:
                self._cache[key] = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                return None
        return self._cache[key]

    def save(self, guild: discord.Guild, *, force: bool = False) -> Optional[Path]:
        data = serialize_guild(guild)
        digest = _digest(data)
        if guild.id not in self._last_digest:
            files = self.list(guild.id)
            latest = self.load(guild.id, files[0].stem) if files else None
            if latest:
                self._last_digest[guild.id] = _digest(latest)
        if not force and self._last_digest.get(guild.id) == digest:
            return None

        folder = self._dir(guild.id)
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = folder / f"{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        self._last_digest[guild.id] = digest
        self._cache[(guild.id, name)] = data

        for old in self.list(guild.id)[KEEP:]:
            old.unlink(missing_ok=True)
            self._cache.pop((guild.id, old.stem), None)
        return path

    def _recent(self, guild_id: int, how_many: int = 10):
        for path in self.list(guild_id)[:how_many]:
            data = self.load(guild_id, path.stem)
            if data:
                yield data

    def find_channel(self, guild_id: int, channel_id: int) -> Optional[tuple[dict, list[int]]]:
        for snap in self._recent(guild_id):
            for ch in snap["channels"]:
                if ch["id"] == channel_id:
                    children = [c["id"] for c in snap["channels"] if c.get("parent_id") == channel_id]
                    return ch, children
        return None

    def find_role(self, guild_id: int, role_id: int) -> Optional[tuple[dict, dict[int, dict]]]:
        for snap in self._recent(guild_id):
            for role in snap["roles"]:
                if role["id"] == role_id:
                    return role, role_overwrites(snap, role_id)
        return None


def role_overwrites(snap: dict[str, Any], role_id: int) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for ch in snap["channels"]:
        for ow in ch.get("overwrites", []):
            if ow["type"] == "role" and ow["id"] == role_id:
                result[ch["id"]] = {"allow": ow["allow"], "deny": ow["deny"]}
    return result
