from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from tarcza.antinuke import AntiNuke
from tarcza.antiraid import AntiRaid
from tarcza.commands import Commands
from tarcza.config import ConfigStore
from tarcza.levels import Levels, LevelStore
from tarcza.messages import MessageCache
from tarcza.snapshots import SnapshotStore
from tarcza.state import StateTracker

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
log = logging.getLogger("tarcza")


class TarczaBot(commands.Bot):
    def __init__(self, *, read_messages: bool, sync_guild: int | None = None, data_dir: Path = DATA) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.moderation = True
        intents.guild_messages = True
        intents.message_content = read_messages
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.data_dir = data_dir
        self.config = ConfigStore(data_dir)
        self.snapshots = SnapshotStore(data_dir)
        self.sync_guild = sync_guild

    async def load_cogs(self) -> None:
        for cog in (StateTracker, MessageCache, AntiNuke, AntiRaid, Commands):
            await self.add_cog(cog(self))
        await self.add_cog(Levels(self, LevelStore(self.data_dir / "levels.db")))

    async def setup_hook(self) -> None:
        await self.load_cogs()
        self.tree.on_error = self.on_app_command_error
        if self.sync_guild:
            guild = discord.Object(self.sync_guild)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Zarejestrowano %d komend.", len(synced))
        self.snapshot_loop.start()

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Zalogowano jako %s – pilnuję %d serwerów.", self.user, len(self.guilds))
        for guild in self.guilds:
            me = guild.me
            if not me.guild_permissions.administrator:
                log.warning("[%s] Bot nie ma uprawnienia Administrator – ochrona będzie niepełna.", guild.name)
            elif me.top_role != max(guild.roles):
                log.warning("[%s] Rola bota nie jest na samej górze – nie ukarze osób z wyższą rolą.", guild.name)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.snapshots.save(guild, force=True)

    @tasks.loop(minutes=30)
    async def snapshot_loop(self) -> None:
        for guild in self.guilds:
            try:
                path = self.snapshots.save(guild)
                if path:
                    log.info("[%s] Zapisano kopię %s", guild.name, path.stem)
            except Exception:
                log.exception("[%s] Błąd zapisu kopii", guild.name)

    @snapshot_loop.before_loop
    async def _before_snapshots(self) -> None:
        await self.wait_until_ready()

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.error("Błąd komendy %s", interaction.command and interaction.command.qualified_name, exc_info=error)
        text = f"❌ Coś poszło nie tak: `{getattr(error, 'original', error)}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "wklej_tutaj_token":
        print("Brak tokenu. Skopiuj plik .env.example jako .env i wklej token bota w DISCORD_TOKEN=")
        sys.exit(1)
    read_messages = os.getenv("ODTWARZAJ_WIADOMOSCI", "1").strip() == "1"
    sync_guild = os.getenv("SERWER_ID", "").strip()

    discord.utils.setup_logging(level=logging.INFO)
    bot = TarczaBot(read_messages=read_messages, sync_guild=int(sync_guild) if sync_guild.isdigit() else None)
    try:
        bot.run(token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        needed = "SERVER MEMBERS INTENT" + (" oraz MESSAGE CONTENT INTENT" if read_messages else "")
        print(
            f"\nDiscord odrzucił połączenie: włącz {needed} na stronie "
            "https://discord.com/developers/applications → twoja aplikacja → Bot → Privileged Gateway Intents."
        )
        sys.exit(1)
    except discord.LoginFailure:
        print("\nNieprawidłowy token. Wygeneruj nowy w Developer Portal → Bot → Reset Token.")
        sys.exit(1)


if __name__ == "__main__":
    main()
