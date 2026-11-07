from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Hashable, Sequence

import discord
from discord.ext import commands

from .log import ORANGE, RED, report_embed, send_log
from .restore import Restorer, undo_entries

A = discord.AuditLogAction
log = logging.getLogger("tarcza.antinuke")

DANGEROUS = discord.Permissions(
    administrator=True,
    ban_members=True,
    kick_members=True,
    manage_guild=True,
    manage_roles=True,
    manage_channels=True,
    manage_webhooks=True,
)

PERM_NAMES = {
    "administrator": "Administrator",
    "ban_members": "banowanie",
    "kick_members": "wyrzucanie",
    "manage_guild": "zarządzanie serwerem",
    "manage_roles": "zarządzanie rolami",
    "manage_channels": "zarządzanie kanałami",
    "manage_webhooks": "zarządzanie webhookami",
}

COUNTED = {
    A.channel_delete: "channel_delete",
    A.channel_create: "channel_create",
    A.role_delete: "role_delete",
    A.role_create: "role_create",
    A.ban: "ban",
    A.kick: "kick",
    A.webhook_create: "webhook_create",
    A.bot_add: "bot_add",
    A.member_role_update: "member_role_update",
    A.member_update: "member_update",
}

LABELS = {
    "channel_delete": "usuwanie kanałów",
    "channel_create": "tworzenie kanałów",
    "role_delete": "usuwanie ról",
    "role_create": "tworzenie ról",
    "ban": "banowanie",
    "kick": "wyrzucanie",
    "webhook_create": "tworzenie webhooków",
    "bot_add": "dodawanie botów",
    "dangerous_grant": "nadawanie niebezpiecznych uprawnień",
    "member_role_update": "masowe zmiany ról członków",
    "member_update": "masowe zmiany nicków / wyciszenia",
}

HISTORY_SECONDS = 60 * 60
QUARANTINE_SECONDS = 15 * 60


def perm_names(value: int) -> str:
    names = [PERM_NAMES.get(name, name) for name, on in discord.Permissions(value) if on]
    return ", ".join(names) or "—"


class RateTracker:
    def __init__(self) -> None:
        self._hits: dict[Hashable, deque[float]] = defaultdict(deque)

    def hit(self, key: Hashable, now: float, limit: int, window: float) -> bool:
        dq = self._hits[key]
        dq.append(now)
        while dq and dq[0] <= now - window:
            dq.popleft()
        return len(dq) >= limit


class AntiNuke(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.rates = RateTracker()
        self.history: dict[int, dict[int, deque[discord.AuditLogEntry]]] = defaultdict(lambda: defaultdict(deque))
        self.processed: dict[int, float] = {}
        self.quarantine: dict[tuple[int, int], float] = {}
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: set[tuple[int, int]] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def config(self) -> Any:
        return self.bot.config

    def is_quarantined(self, guild_id: int, user_id: int) -> bool:
        until = self.quarantine.get((guild_id, user_id))
        return bool(until and until > time.time())

    def _remember(self, entry: discord.AuditLogEntry) -> None:
        assert entry.user_id is not None
        dq = self.history[entry.guild.id][entry.user_id]
        dq.append(entry)
        limit = time.time() - HISTORY_SECONDS
        while dq and dq[0].created_at.timestamp() < limit:
            dq.popleft()
        if len(self.processed) > 5000:
            self.processed = {k: v for k, v in self.processed.items() if v > limit}

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        guild = entry.guild
        uid = entry.user_id
        if uid is None or self.bot.user is None or uid == self.bot.user.id:
            return
        if self.config.is_trusted(guild, uid):
            return
        self._remember(entry)

        if self.is_quarantined(guild.id, uid):
            self.schedule_rollback(guild, uid)
            return

        cfg = self.config.get(guild.id)
        kinds = []
        if entry.action in COUNTED:
            kinds.append(COUNTED[entry.action])
        if await self.guard(entry, cfg):
            kinds.append("dangerous_grant")

        now = time.time()
        for kind in kinds:
            limit, window = cfg["thresholds"].get(kind, [10**6, 1])
            if self.rates.hit((guild.id, uid, kind), now, int(limit), float(window)):
                await self.incident(guild, uid, f"{LABELS[kind]}: {limit}× w {window} s")
                return

    async def guard(self, entry: discord.AuditLogEntry, cfg: dict) -> bool:
        guild = entry.guild
        me = guild.me
        uid = entry.user_id
        target_id = getattr(entry.target, "id", None)

        if entry.action is A.bot_add:
            member = guild.get_member(target_id) if target_id else None
            if cfg["block_bots"] and member and member.bot and member.top_role < me.top_role:
                try:
                    await member.kick(reason="Tarcza: boty mogą dodawać tylko zaufane osoby")
                except discord.HTTPException:
                    return False
                await send_log(
                    self.bot,
                    guild,
                    discord.Embed(
                        title="🤖 Zablokowano dodanie bota",
                        colour=ORANGE,
                        description=(
                            f"<@{uid}> dodał bota **{discord.utils.escape_markdown(member.name)}** – wyrzuciłem go.\n"
                            "Boty mogą dodawać tylko właściciel i osoby z `/tarcza zaufani`."
                        ),
                    ),
                )
            return False

        if not cfg["guard_permissions"]:
            return False

        if entry.action in (A.role_update, A.role_create):
            after = getattr(entry.after, "permissions", None)
            if after is None:
                return False
            before = getattr(entry.before, "permissions", None) or discord.Permissions.none()
            gained = after.value & ~before.value & DANGEROUS.value
            if not gained:
                return False
            role = guild.get_role(target_id) if target_id else None
            if role and role < me.top_role:
                try:
                    await role.edit(
                        permissions=discord.Permissions(role.permissions.value & ~gained),
                        reason="Tarcza: niebezpieczne uprawnienia mogą nadawać tylko zaufani",
                    )
                    await send_log(
                        self.bot,
                        guild,
                        discord.Embed(
                            title="🛑 Zablokowano nadanie uprawnień",
                            colour=ORANGE,
                            description=(
                                f"<@{uid}> dał roli {role.mention} uprawnienia: **{perm_names(gained)}** – cofnąłem to.\n"
                                "Takie uprawnienia mogą nadawać tylko właściciel i osoby z `/tarcza zaufani`."
                            ),
                        ),
                    )
                except discord.HTTPException:
                    pass
            return True

        if entry.action is A.member_role_update:
            added = [guild.get_role(r.id) for r in getattr(entry.after, "roles", []) or []]
            bad = [r for r in added if r and not r.managed and r.permissions.value & DANGEROUS.value and r < me.top_role]
            if not bad:
                return False
            member = guild.get_member(target_id) if target_id else None
            if member:
                try:
                    await member.remove_roles(*bad, reason="Tarcza: niebezpieczne role mogą nadawać tylko zaufani")
                    await send_log(
                        self.bot,
                        guild,
                        discord.Embed(
                            title="🛑 Zablokowano nadanie roli",
                            colour=ORANGE,
                            description=(
                                f"<@{uid}> dał {member.mention} rolę "
                                + ", ".join(r.mention for r in bad)
                                + " z groźnymi uprawnieniami – zabrałem ją.\n"
                                "Takie role mogą nadawać tylko właściciel i osoby z `/tarcza zaufani`."
                            ),
                        ),
                    )
                except discord.HTTPException:
                    pass
            return True

        return False

    async def incident(self, guild: discord.Guild, uid: int, reason: str) -> None:
        if self.is_quarantined(guild.id, uid):
            return
        self.quarantine[(guild.id, uid)] = time.time() + QUARANTINE_SECONDS
        log.warning("Atak na %s (%s) przez %s: %s", guild.name, guild.id, uid, reason)

        result = await self.punish(guild, uid, reason)
        minutes = self.config.get(guild.id)["rollback_minutes"]
        embed = discord.Embed(
            title="🚨 Wykryto atak na serwer",
            colour=RED,
            description=(
                f"**Sprawca:** <@{uid}> (`{uid}`)\n"
                f"**Co robił:** {reason}\n"
                f"**Reakcja:** {result}\n\n"
                f"Cofam wszystko, co ta osoba zrobiła w ostatnich {minutes} min…"
            ),
        )
        await send_log(self.bot, guild, embed, dm_owner=True)
        await asyncio.sleep(2.0)
        await self.rollback(guild, uid)

    async def punish(self, guild: discord.Guild, uid: int, reason: str) -> str:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                return "sprawcy nie ma już na serwerze"
        if member.id == guild.owner_id:
            return "to właściciel serwera – nie mogę go ukarać, cofam tylko zmiany"
        me = guild.me
        if member.top_role >= me.top_role:
            return (
                f"⚠️ **nie mogę go ukarać** – jego rola {member.top_role.mention} jest wyżej niż rola bota. "
                "Przesuń rolę bota na samą górę listy ról! Mimo to cofam jego zmiany na bieżąco."
            )
        mode = "ban" if member.bot else self.config.get(guild.id)["punishment"]
        audit_reason = f"Tarcza: {reason}"[:500]
        try:
            if mode == "ban":
                await guild.ban(member, reason=audit_reason, delete_message_seconds=0)
                return "zbanowany" + (" (to był bot)" if member.bot else "")
            if mode == "kick":
                await member.kick(reason=audit_reason)
                return "wyrzucony z serwera"
            keep = [r for r in member.roles if r.managed and not r.is_default()]
            await member.edit(roles=keep, reason=audit_reason)
            return "zabrałem mu wszystkie role"
        except discord.HTTPException as exc:
            return f"⚠️ kara się nie udała: {getattr(exc, 'text', None) or exc}"

    def schedule_rollback(self, guild: discord.Guild, uid: int) -> None:
        key = (guild.id, uid)
        if key in self._pending:
            return
        self._pending.add(key)

        async def runner() -> None:
            try:
                await asyncio.sleep(2.0)
            finally:
                self._pending.discard(key)
            await self.rollback(guild, uid)

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def rollback(self, guild: discord.Guild, uid: int) -> None:
        cfg = self.config.get(guild.id)
        since = time.time() - cfg["rollback_minutes"] * 60
        async with self.locks[guild.id]:
            entries = [
                e
                for e in self.history[guild.id].get(uid, ())
                if e.created_at.timestamp() >= since and e.id not in self.processed
            ]
            if not entries:
                return
            now = time.time()
            for e in entries:
                self.processed[e.id] = now
            restorer = await undo_entries(self.bot, guild, entries, reason=f"Tarcza: cofanie skutków ataku ({uid})")
        embed = report_embed("♻️ Raport z cofania zmian", restorer)
        embed.insert_field_at(0, name="Sprawca", value=f"<@{uid}> (`{uid}`)", inline=False)
        await send_log(self.bot, guild, embed, dm_owner=True)

    async def manual_undo(self, guild: discord.Guild, entries: Sequence[discord.AuditLogEntry], reason: str) -> Restorer:
        async with self.locks[guild.id]:
            fresh = [e for e in entries if e.id not in self.processed]
            now = time.time()
            for e in fresh:
                self.processed[e.id] = now
            return await undo_entries(self.bot, guild, fresh, reason=reason)
