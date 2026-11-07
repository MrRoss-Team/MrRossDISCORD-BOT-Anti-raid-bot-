from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import sqlite3
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .cards import GREEN, render_card

log = logging.getLogger("tarcza.levels")

try:
    from zoneinfo import ZoneInfo

    TZ: Optional[dt.tzinfo] = ZoneInfo("Europe/Warsaw")
except Exception:
    TZ = None

_COUNTED_TYPES = (discord.MessageType.default, discord.MessageType.reply)


def now_text() -> str:
    now = dt.datetime.now(TZ) if TZ else dt.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M")


def add_xp(level: int, xp: int, gained: int, per_level: int) -> tuple[int, int, int]:
    per_level = max(1, per_level)
    xp += gained
    ups = 0
    while xp >= per_level:
        xp -= per_level
        level += 1
        ups += 1
    return level, xp, ups


class LevelStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS levels (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                level    INTEGER NOT NULL DEFAULT 0,
                xp       INTEGER NOT NULL DEFAULT 0,
                total_xp INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        self.db.commit()

    def get(self, guild_id: int, user_id: int) -> tuple[int, int, int]:
        row = self.db.execute(
            "SELECT level, xp, total_xp FROM levels WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return (row[0], row[1], row[2]) if row else (0, 0, 0)

    def set(self, guild_id: int, user_id: int, level: int, xp: int, total_xp: int) -> None:
        self.db.execute(
            """INSERT INTO levels (guild_id, user_id, level, xp, total_xp) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE
               SET level = excluded.level, xp = excluded.xp, total_xp = excluded.total_xp""",
            (guild_id, user_id, level, xp, total_xp),
        )
        self.db.commit()

    def top(self, guild_id: int, limit: int = 10) -> list[tuple[int, int, int]]:
        rows = self.db.execute(
            "SELECT user_id, level, xp FROM levels WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def rank(self, guild_id: int, user_id: int) -> int:
        level, xp, _ = self.get(guild_id, user_id)
        (better,) = self.db.execute(
            "SELECT COUNT(*) FROM levels WHERE guild_id = ? AND (level > ? OR (level = ? AND xp > ?))",
            (guild_id, level, level, xp),
        ).fetchone()
        return better + 1

    def close(self) -> None:
        self.db.close()


class Levels(commands.Cog):
    poziomy = app_commands.Group(
        name="poziomy",
        description="Ustawienia systemu poziomów",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot, store: LevelStore) -> None:
        self.bot = bot
        self.store = store
        self.cooldowns: dict[tuple[int, int], float] = {}
        self._icons: dict[int, tuple[str, Optional[bytes]]] = {}

    def cfg(self, guild_id: int) -> dict[str, Any]:
        return self.bot.config.get(guild_id)["levels"]

    async def cog_unload(self) -> None:
        self.store.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        if not isinstance(message.author, discord.Member) or message.type not in _COUNTED_TYPES:
            return
        cfg = self.cfg(message.guild.id)
        if not cfg["enabled"]:
            return

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        last = self.cooldowns.get(key)
        if last is not None and now - last < cfg["cooldown"]:
            return
        self.cooldowns[key] = now

        low, high = sorted((int(cfg["xp_min"]), int(cfg["xp_max"])))
        gained = random.randint(low, high)
        if gained <= 0:
            return
        level, xp, total = self.store.get(*key)
        new_level, new_xp, ups = add_xp(level, xp, gained, int(cfg["xp_per_level"]))
        self.store.set(*key, new_level, new_xp, total + gained)
        if ups:
            await self.announce(message.author, message.channel, new_level, new_xp)

    async def announce(self, member: discord.Member, fallback: Any, level: int, xp: int) -> None:
        cfg = self.cfg(member.guild.id)
        channel = member.guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
        channel = channel or fallback
        if not isinstance(channel, discord.abc.Messageable) or not hasattr(channel, "permissions_for"):
            return
        perms = channel.permissions_for(member.guild.me)
        if not (perms.send_messages and perms.attach_files):
            return
        try:
            png = await self.levelup_card(member, level, xp)
            ping = bool(cfg["ping"])
            await channel.send(
                content=member.mention if ping else None,
                file=discord.File(BytesIO(png), filename="awans.png"),
                allowed_mentions=discord.AllowedMentions(users=[member]) if ping else discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Nie udało się wysłać karty awansu dla %s", member)

    async def _avatar(self, member: discord.Member) -> Optional[bytes]:
        try:
            return await member.display_avatar.replace(size=128, format="png").read()
        except (discord.HTTPException, ValueError):
            return None

    async def _guild_icon(self, guild: discord.Guild) -> Optional[bytes]:
        if guild.icon is None:
            return None
        cached = self._icons.get(guild.id)
        if cached and cached[0] == guild.icon.key:
            return cached[1]
        try:
            data: Optional[bytes] = await guild.icon.replace(size=64, format="png").read()
        except (discord.HTTPException, ValueError):
            data = None
        self._icons[guild.id] = (guild.icon.key, data)
        return data

    async def _card(self, member: discord.Member, title: str, fields: list[tuple[str, str]]) -> bytes:
        cfg = self.cfg(member.guild.id)
        footer = " • ".join(p for p in (str(cfg.get("footer") or "").strip(), now_text()) if p)
        avatar, icon = await asyncio.gather(self._avatar(member), self._guild_icon(member.guild))
        return await asyncio.to_thread(
            render_card, title, fields, avatar=avatar, footer=footer, footer_icon=icon, accent=GREEN
        )

    async def levelup_card(self, member: discord.Member, level: int, xp: int) -> bytes:
        per = int(self.cfg(member.guild.id)["xp_per_level"])
        return await self._card(
            member,
            "Awans poziomu",
            [("Użytkownik", member.name), ("Nowy poziom", str(level)), ("Exp", f"{xp} / {per}")],
        )

    @app_commands.command(name="poziom", description="Pokazuje poziom i exp – twój albo innej osoby")
    @app_commands.guild_only()
    @app_commands.describe(uzytkownik="Czyj poziom pokazać (domyślnie twój)")
    async def poziom(self, interaction: discord.Interaction, uzytkownik: Optional[discord.Member] = None) -> None:
        member = uzytkownik or interaction.user
        assert isinstance(member, discord.Member) and interaction.guild is not None
        if member.bot:
            await interaction.response.send_message("Boty nie zbierają expa 🤖", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        level, xp, _ = self.store.get(interaction.guild.id, member.id)
        rank = self.store.rank(interaction.guild.id, member.id)
        per = int(self.cfg(interaction.guild.id)["xp_per_level"])
        png = await self._card(
            member,
            f"Miejsce #{rank} w rankingu",
            [("Użytkownik", member.name), ("Poziom", str(level)), ("Exp", f"{xp} / {per}")],
        )
        await interaction.followup.send(file=discord.File(BytesIO(png), filename="poziom.png"))

    @app_commands.command(name="ranking", description="Top 10 osób z najwyższym poziomem na serwerze")
    @app_commands.guild_only()
    async def ranking(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        per = int(self.cfg(interaction.guild.id)["xp_per_level"])
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [
            f"{medals.get(i, f'**{i}.**')} <@{uid}> – poziom **{level}** ({xp} / {per} exp)"
            for i, (uid, level, xp) in enumerate(self.store.top(interaction.guild.id, 10), start=1)
        ]
        embed = discord.Embed(
            title="🏆 Ranking poziomów",
            description="\n".join(lines) or "Nikt jeszcze nie zdobył expa – piszcie! ✍️",
            colour=discord.Colour.from_rgb(*GREEN),
        )
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _manager_only(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message("⛔ Potrzebujesz uprawnienia „Zarządzanie serwerem”.", ephemeral=True)
        return False

    @poziomy.command(name="kanal", description="Gdzie wysyłać karty awansu (puste = tam, gdzie ktoś napisał)")
    @app_commands.describe(kanal="Kanał na karty awansu; zostaw puste, żeby wysyłać na kanale z wiadomością")
    async def kanal(self, interaction: discord.Interaction, kanal: Optional[discord.TextChannel] = None) -> None:
        if not await self._manager_only(interaction):
            return
        assert interaction.guild is not None
        self.cfg(interaction.guild.id)["channel_id"] = kanal.id if kanal else None
        self.bot.config.save(interaction.guild.id)
        where = kanal.mention if kanal else "kanał, na którym ktoś awansował"
        await interaction.response.send_message(f"✅ Karty awansu będą trafiać na: {where}.", ephemeral=True)

    @poziomy.command(name="ustaw", description="Ile expa za wiadomość, ile na poziom, przerwa, stopka karty")
    @app_commands.describe(
        wlaczone="Czy system poziomów działa",
        exp_na_poziom="Ile expa potrzeba na każdy poziom (na wzorze: 250)",
        exp_min="Najmniej expa za jedną wiadomość",
        exp_max="Najwięcej expa za jedną wiadomość",
        przerwa="Co ile sekund wiadomość może dać exp (ochrona przed spamem)",
        stopka="Tekst w stopce karty, np. „Statystyki”",
        oznaczaj="Czy oznaczać osobę (@) przy karcie awansu",
    )
    async def ustaw(
        self,
        interaction: discord.Interaction,
        wlaczone: Optional[bool] = None,
        exp_na_poziom: Optional[app_commands.Range[int, 10, 1_000_000]] = None,
        exp_min: Optional[app_commands.Range[int, 0, 10_000]] = None,
        exp_max: Optional[app_commands.Range[int, 0, 10_000]] = None,
        przerwa: Optional[app_commands.Range[int, 0, 3600]] = None,
        stopka: Optional[app_commands.Range[str, 0, 60]] = None,
        oznaczaj: Optional[bool] = None,
    ) -> None:
        if not await self._manager_only(interaction):
            return
        assert interaction.guild is not None
        cfg = self.cfg(interaction.guild.id)
        for key, value in (
            ("enabled", wlaczone),
            ("xp_per_level", exp_na_poziom),
            ("xp_min", exp_min),
            ("xp_max", exp_max),
            ("cooldown", przerwa),
            ("footer", stopka),
            ("ping", oznaczaj),
        ):
            if value is not None:
                cfg[key] = value
        if cfg["xp_min"] > cfg["xp_max"]:
            cfg["xp_min"], cfg["xp_max"] = cfg["xp_max"], cfg["xp_min"]
        self.bot.config.save(interaction.guild.id)
        await interaction.response.send_message(
            f"{'✅' if cfg['enabled'] else '❌'} Poziomy {'włączone' if cfg['enabled'] else 'wyłączone'}\n"
            f"• **{cfg['xp_min']}–{cfg['xp_max']}** expa za wiadomość, najwyżej raz na **{cfg['cooldown']} s**\n"
            f"• **{cfg['xp_per_level']}** expa na poziom\n"
            f"• stopka karty: „{cfg['footer']}”, oznaczanie: {'tak' if cfg['ping'] else 'nie'}",
            ephemeral=True,
        )

    @poziomy.command(name="nadaj", description="Ustawia komuś poziom i exp (np. przy przenoszeniu z innego bota)")
    @app_commands.describe(uzytkownik="Komu", poziom="Poziom do ustawienia", exp="Exp w obecnym poziomie")
    async def nadaj(
        self,
        interaction: discord.Interaction,
        uzytkownik: discord.Member,
        poziom: app_commands.Range[int, 0, 100_000],
        exp: app_commands.Range[int, 0, 1_000_000] = 0,
    ) -> None:
        if not await self._manager_only(interaction):
            return
        assert interaction.guild is not None
        per = int(self.cfg(interaction.guild.id)["xp_per_level"])
        level, xp, _ = add_xp(poziom, 0, exp, per)
        self.store.set(interaction.guild.id, uzytkownik.id, level, xp, level * per + xp)
        await interaction.response.send_message(
            f"✅ {uzytkownik.mention}: poziom **{level}**, exp **{xp} / {per}**.", ephemeral=True
        )

    @poziomy.command(name="test", description="Pokazuje podgląd karty awansu (tylko tobie)")
    async def test(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        assert isinstance(member, discord.Member) and interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        level, xp, _ = self.store.get(interaction.guild.id, member.id)
        png = await self.levelup_card(member, max(level, 1), xp)
        await interaction.followup.send(
            "Tak będzie wyglądać karta awansu:", file=discord.File(BytesIO(png), filename="awans.png"), ephemeral=True
        )
