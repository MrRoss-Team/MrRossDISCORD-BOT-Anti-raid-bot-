from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import discord

DEFAULTS: dict[str, Any] = {
    "log_channel_id": None,
    "trusted": [],
    "punishment": "strip",
    "thresholds": {
        "channel_delete": [3, 15],
        "channel_create": [6, 15],
        "role_delete": [3, 15],
        "role_create": [6, 15],
        "ban": [3, 15],
        "kick": [4, 15],
        "webhook_create": [3, 30],
        "bot_add": [2, 120],
        "dangerous_grant": [2, 60],
        "member_role_update": [8, 20],
        "member_update": [8, 20],
    },
    "rollback_minutes": 10,
    "guard_permissions": True,
    "block_bots": True,
    "restore_messages": True,
    "raid": {
        "enabled": True,
        "joins": 8,
        "seconds": 20,
        "min_account_age_days": 7,
        "cluster_size": 4,
        "action": "kick",
        "quiet_minutes": 5,
    },
    "levels": {
        "enabled": True,
        "channel_id": None,
        "xp_per_level": 250,
        "xp_min": 10,
        "xp_max": 20,
        "cooldown": 60,
        "footer": "Statystyki",
        "ping": False,
    },
    "raid_restore": None,
    "id_map": {"channels": {}, "roles": {}},
}


def _merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


class ConfigStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[int, dict[str, Any]] = {}

    def _path(self, guild_id: int) -> Path:
        return self.root / str(guild_id) / "config.json"

    def get(self, guild_id: int) -> dict[str, Any]:
        if guild_id not in self._cache:
            data = copy.deepcopy(DEFAULTS)
            path = self._path(guild_id)
            if path.exists():
                try:
                    _merge(data, json.loads(path.read_text("utf-8")))
                except (OSError, ValueError):
                    pass
            self._cache[guild_id] = data
        return self._cache[guild_id]

    def save(self, guild_id: int) -> None:
        path = self._path(guild_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.get(guild_id), indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(path)

    def is_trusted(self, guild: discord.Guild, user_id: int) -> bool:
        return user_id == guild.owner_id or user_id in self.get(guild.id)["trusted"]

    def id_maps(self, guild_id: int) -> tuple[dict[int, int], dict[int, int]]:
        raw = self.get(guild_id)["id_map"]
        channels = {int(k): int(v) for k, v in raw.get("channels", {}).items()}
        roles = {int(k): int(v) for k, v in raw.get("roles", {}).items()}
        return channels, roles

    def remember_ids(self, guild_id: int, channels: dict[int, int], roles: dict[int, int]) -> None:
        if not channels and not roles:
            return
        raw = self.get(guild_id)["id_map"]
        raw.setdefault("channels", {}).update({str(k): v for k, v in channels.items()})
        raw.setdefault("roles", {}).update({str(k): v for k, v in roles.items()})
        cfg = self.get(guild_id)
        log_id = cfg.get("log_channel_id")
        if log_id in channels:
            cfg["log_channel_id"] = channels[log_id]
        levels_id = cfg["levels"].get("channel_id")
        if levels_id in channels:
            cfg["levels"]["channel_id"] = channels[levels_id]
        self.save(guild_id)
