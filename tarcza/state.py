from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

import discord
from discord.ext import commands

GRAVEYARD_TTL = 60 * 60


def overwrite_target_type(target: Any) -> str:
    if isinstance(target, discord.Role):
        return "role"
    if isinstance(target, (discord.Member, discord.User)):
        return "member"
    return "role" if getattr(target, "type", None) is discord.Role else "member"


def serialize_overwrites(overwrites: Any) -> list[dict]:
    items: Iterable = overwrites.items() if isinstance(overwrites, dict) else overwrites
    out = []
    for target, ow in items:
        allow, deny = ow.pair()
        out.append({"id": target.id, "type": overwrite_target_type(target), "allow": allow.value, "deny": deny.value})
    return out


def serialize_channel(ch: discord.abc.GuildChannel) -> dict:
    data: dict[str, Any] = {
        "id": ch.id,
        "name": ch.name,
        "type": ch.type.name,
        "position": ch.position,
        "parent_id": ch.category_id,
        "overwrites": serialize_overwrites(ch.overwrites),
    }
    if isinstance(ch, discord.TextChannel):
        data.update(topic=ch.topic, nsfw=ch.nsfw, slowmode=ch.slowmode_delay)
    elif isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        data.update(bitrate=ch.bitrate, user_limit=ch.user_limit, rtc_region=ch.rtc_region, nsfw=ch.nsfw)
    elif isinstance(ch, discord.ForumChannel):
        data.update(
            topic=ch.topic,
            nsfw=ch.nsfw,
            slowmode=ch.slowmode_delay,
            tags=[
                {
                    "name": t.name,
                    "moderated": t.moderated,
                    "emoji": str(t.emoji) if t.emoji and t.emoji.is_unicode_emoji() else None,
                }
                for t in ch.available_tags
            ],
        )
    return data


def serialize_role(role: discord.Role) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "permissions": role.permissions.value,
        "color": role.colour.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "position": role.position,
        "managed": role.managed,
        "default": role.is_default(),
    }


def serialize_guild(guild: discord.Guild) -> dict:
    roles = []
    for role in guild.roles:
        data = serialize_role(role)
        data["members"] = [] if role.is_default() else [m.id for m in role.members]
        roles.append(data)
    return {
        "guild_id": guild.id,
        "guild_name": guild.name,
        "taken_at": time.time(),
        "roles": roles,
        "channels": [serialize_channel(c) for c in guild.channels],
    }


@dataclass
class DeadChannel:
    data: dict
    children: list[int]
    deleted_at: float


@dataclass
class DeadRole:
    data: dict
    members: list[int]
    overwrites: dict[int, dict]
    deleted_at: float


@dataclass
class GuildState:
    channels: dict[int, dict] = field(default_factory=dict)
    roles: dict[int, dict] = field(default_factory=dict)
    role_members: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))
    dead_channels: dict[int, DeadChannel] = field(default_factory=dict)
    dead_roles: dict[int, DeadRole] = field(default_factory=dict)
    orphans: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))

    def refresh(self, guild: discord.Guild) -> None:
        self.channels = {c.id: serialize_channel(c) for c in guild.channels}
        self.roles = {r.id: serialize_role(r) for r in guild.roles}
        self.role_members = defaultdict(set)
        for member in guild.members:
            for role in member.roles:
                if not role.is_default():
                    self.role_members[role.id].add(member.id)

    def prune(self) -> None:
        limit = time.time() - GRAVEYARD_TTL
        self.dead_channels = {k: v for k, v in self.dead_channels.items() if v.deleted_at > limit}
        self.dead_roles = {k: v for k, v in self.dead_roles.items() if v.deleted_at > limit}


class StateTracker(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.guilds: dict[int, GuildState] = {}

    def get(self, guild: discord.Guild) -> GuildState:
        state = self.guilds.get(guild.id)
        if state is None:
            state = self.guilds[guild.id] = GuildState()
            state.refresh(guild)
        return state

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            self.get(guild).refresh(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.get(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.guilds.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        self.get(channel.guild).channels[channel.id] = serialize_channel(channel)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        state = self.get(after.guild)
        state.channels[after.id] = serialize_channel(after)
        if before.category_id and after.category_id is None:
            state.orphans[before.category_id].add(after.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        state = self.get(channel.guild)
        data = state.channels.pop(channel.id, None) or serialize_channel(channel)
        children: list[int] = []
        if isinstance(channel, discord.CategoryChannel):
            children = [cid for cid, c in state.channels.items() if c.get("parent_id") == channel.id]
        state.dead_channels[channel.id] = DeadChannel(data, children, time.time())
        state.prune()

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        self.get(role.guild).roles[role.id] = serialize_role(role)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        self.get(after.guild).roles[after.id] = serialize_role(after)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        state = self.get(role.guild)
        data = state.roles.pop(role.id, None) or serialize_role(role)
        members = sorted(state.role_members.pop(role.id, set()))
        overwrites: dict[int, dict] = {}
        for cid, ch in state.channels.items():
            for ow in ch.get("overwrites", []):
                if ow["type"] == "role" and ow["id"] == role.id:
                    overwrites[cid] = {"allow": ow["allow"], "deny": ow["deny"]}
        state.dead_roles[role.id] = DeadRole(data, members, overwrites, time.time())
        state.prune()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        state = self.get(member.guild)
        for role in member.roles:
            if not role.is_default():
                state.role_members[role.id].add(member.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return
        state = self.get(after.guild)
        old = {r.id for r in before.roles}
        new = {r.id for r in after.roles}
        for rid in new - old:
            state.role_members[rid].add(after.id)
        for rid in old - new:
            state.role_members[rid].discard(after.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        state = self.get(member.guild)
        for role in member.roles:
            state.role_members.get(role.id, set()).discard(member.id)
