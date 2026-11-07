from __future__ import annotations

import datetime as dt
import logging
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

import discord
from discord.ext import commands, tasks

from .log import GREEN, ORANGE, send_log

log = logging.getLogger("tarcza.antiraid")

RECENT_SECONDS = 15 * 60
CREATED_GAP = 30 * 60


@dataclass(frozen=True)
class Joiner:
    id: int
    name: str
    created: float
    joined: float
    default_avatar: bool


def skeleton(name: str) -> str:
    text = unicodedata.normalize("NFKD", name.lower())
    return "".join(ch for ch in text if ch.isalpha() and ch.isascii())


def names_similar(a: str, b: str) -> bool:
    sa, sb = skeleton(a), skeleton(b)
    if len(sa) >= 3 and sa == sb:
        return True
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.8


def find_clusters(joiners: Iterable[Joiner], *, min_size: int, created_gap: float = CREATED_GAP) -> list[list[Joiner]]:
    items = sorted(joiners, key=lambda j: j.created)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(items):
        for j in range(i + 1, len(items)):
            b = items[j]
            gap = b.created - a.created
            if gap > created_gap:
                break
            if names_similar(a.name, b.name) or (a.default_avatar and b.default_avatar and gap <= created_gap / 3):
                parent[find(i)] = find(j)

    groups: dict[int, list[Joiner]] = defaultdict(list)
    for i, item in enumerate(items):
        groups[find(i)].append(item)
    return [g for g in groups.values() if len(g) >= min_size]


class AntiRaid(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.recent: dict[int, deque[Joiner]] = defaultdict(deque)
        self.active: dict[int, float] = {}
        self.handled: dict[int, set[int]] = defaultdict(set)
        self.started: dict[int, float] = {}

    @property
    def config(self) -> Any:
        return self.bot.config

    async def cog_load(self) -> None:
        self.watch.start()

    async def cog_unload(self) -> None:
        self.watch.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            if self.config.get(guild.id).get("raid_restore") and guild.id not in self.active:
                self.active[guild.id] = time.time()
                self.started[guild.id] = time.time()

    def is_active(self, guild_id: int) -> bool:
        return guild_id in self.active

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        cfg = self.config.get(guild.id)["raid"]
        if not cfg["enabled"]:
            return

        now = time.time()
        joiner = Joiner(member.id, member.name, member.created_at.timestamp(), now, member.avatar is None)
        dq = self.recent[guild.id]
        dq.append(joiner)
        while dq and dq[0].joined < now - RECENT_SECONDS:
            dq.popleft()

        young = (now - joiner.created) < cfg["min_account_age_days"] * 86400
        clusters = find_clusters(dq, min_size=int(cfg["cluster_size"]))
        clustered = {j.id for group in clusters for j in group}

        if self.is_active(guild.id):
            if young or member.id in clustered:
                self.active[guild.id] = now
            await self._act_on(guild, [j for j in dq if j.id in clustered], "konto z grupy klonów")
            if young:
                await self._act_on(guild, [joiner], "nowe konto w trakcie rajdu")
            return

        window = [j for j in dq if j.joined >= now - cfg["seconds"]]
        if len(window) >= cfg["joins"]:
            await self.start_raid(guild, f"{len(window)} wejść w {cfg['seconds']} s")
            limit = now - cfg["min_account_age_days"] * 86400
            await self._act_on(guild, [j for j in window if j.created > limit], "nowe konto w trakcie rajdu")
            await self._act_on(guild, [j for j in dq if j.id in clustered], "konto z grupy klonów")
        elif clusters:
            biggest = max(clusters, key=len)
            await self.start_raid(
                guild,
                f"{len(biggest)} podobnych kont założonych w odstępie kilkunastu minut "
                f"(np. {', '.join(discord.utils.escape_markdown(j.name) for j in biggest[:3])})",
            )
            await self._act_on(guild, [j for j in dq if j.id in clustered], "konto z grupy klonów")

    async def _act_on(self, guild: discord.Guild, joiners: Iterable[Joiner], why: str) -> None:
        cfg = self.config.get(guild.id)["raid"]
        me = guild.me
        for j in joiners:
            if j.id in self.handled[guild.id] or self.config.is_trusted(guild, j.id):
                continue
            member = guild.get_member(j.id)
            if member is None or member.bot or member.top_role >= me.top_role:
                continue
            self.handled[guild.id].add(j.id)
            reason = f"Tarcza (rajd): {why}"
            try:
                if cfg["action"] == "ban":
                    await guild.ban(member, reason=reason, delete_message_seconds=3600)
                elif cfg["action"] == "timeout":
                    await member.timeout(dt.timedelta(days=1), reason=reason)
                else:
                    await member.kick(reason=reason)
            except discord.HTTPException as exc:
                log.warning("Nie udało się ukarać %s: %s", member, exc)

    async def start_raid(self, guild: discord.Guild, reason: str, *, manual_by: Optional[int] = None) -> None:
        if self.is_active(guild.id):
            return
        self.active[guild.id] = time.time()
        self.started[guild.id] = time.time()
        cfg = self.config.get(guild.id)
        notes = []
        if guild.me.guild_permissions.manage_guild:
            restore = {
                "invites_disabled": guild.invites_paused(),
                "verification_level": guild.verification_level.value,
            }
            kwargs: dict[str, Any] = {"invites_disabled": True}
            if guild.verification_level < discord.VerificationLevel.high:
                kwargs["verification_level"] = discord.VerificationLevel.high
            try:
                await guild.edit(reason=f"Tarcza: tryb anty-rajd – {reason}"[:500], **kwargs)
                notes.append("⏸️ zaproszenia wstrzymane")
                if "verification_level" in kwargs:
                    notes.append("🔒 poziom weryfikacji: wysoki")
                cfg["raid_restore"] = restore
                self.config.save(guild.id)
            except discord.HTTPException as exc:
                notes.append(f"⚠️ nie udało się wstrzymać zaproszeń: {getattr(exc, 'text', None) or exc}")
        action = {"kick": "wyrzucam", "ban": "banuję", "timeout": "wyciszam na dobę"}[cfg["raid"]["action"]]
        who = f"\n**Włączył:** <@{manual_by}>" if manual_by else ""
        embed = discord.Embed(
            title="🛡️ Tryb anty-rajd WŁĄCZONY",
            colour=ORANGE,
            description=(
                f"**Powód:** {reason}{who}\n"
                + ("\n".join(notes) + "\n" if notes else "")
                + f"\nNowe konta (młodsze niż {cfg['raid']['min_account_age_days']} dni) i konta-klony {action}.\n"
                f"Tryb wyłączy się sam po {cfg['raid']['quiet_minutes']} min spokoju albo przez `/rajd koniec`."
            ),
        )
        await send_log(self.bot, guild, embed, dm_owner=True)

    async def end_raid(self, guild: discord.Guild, *, manual_by: Optional[int] = None) -> None:
        if self.active.pop(guild.id, None) is None:
            return
        cfg = self.config.get(guild.id)
        restore = cfg.get("raid_restore")
        notes = []
        if restore:
            try:
                await guild.edit(
                    invites_disabled=bool(restore["invites_disabled"]),
                    verification_level=discord.VerificationLevel(restore["verification_level"]),
                    reason="Tarcza: koniec trybu anty-rajd",
                )
                notes.append("▶️ zaproszenia i weryfikacja wróciły do poprzednich ustawień")
            except discord.HTTPException as exc:
                notes.append(f"⚠️ nie udało się przywrócić ustawień: {getattr(exc, 'text', None) or exc}")
            cfg["raid_restore"] = None
            self.config.save(guild.id)
        caught = len(self.handled.pop(guild.id, set()))
        started = self.started.pop(guild.id, time.time())
        who = f" (wyłączył <@{manual_by}>)" if manual_by else ""
        embed = discord.Embed(
            title="✅ Tryb anty-rajd wyłączony" + who,
            colour=GREEN,
            description=(
                f"Trwał {max(1, round((time.time() - started) / 60))} min. Usunięto podejrzanych kont: **{caught}**.\n"
                + "\n".join(notes)
            ),
        )
        await send_log(self.bot, guild, embed)

    @tasks.loop(seconds=30)
    async def watch(self) -> None:
        now = time.time()
        for guild_id, last in list(self.active.items()):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self.active.pop(guild_id, None)
                continue
            quiet = self.config.get(guild_id)["raid"]["quiet_minutes"] * 60
            if now - last >= quiet:
                await self.end_raid(guild)

    @watch.before_loop
    async def _before_watch(self) -> None:
        await self.bot.wait_until_ready()
