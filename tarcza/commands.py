from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any, Optional

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

from .antinuke import LABELS
from .log import BLUE, ORANGE, report_embed, send_log
from .restore import restore_from_snapshot, snapshot_missing

ADMIN = discord.Permissions(administrator=True)

ACTION_PL = {
    "channel_delete": "usunięte kanały",
    "channel_create": "utworzone kanały",
    "channel_update": "zmiany kanałów",
    "role_delete": "usunięte role",
    "role_create": "utworzone role",
    "role_update": "zmiany ról",
    "ban": "bany",
    "kick": "wyrzucenia (nie do cofnięcia)",
    "member_role_update": "zmiany ról członków",
    "member_update": "zmiany nicków / wyciszenia",
    "webhook_create": "utworzone webhooki",
    "overwrite_create": "zmiany uprawnień na kanałach",
    "overwrite_update": "zmiany uprawnień na kanałach",
    "overwrite_delete": "zmiany uprawnień na kanałach",
    "guild_update": "zmiany ustawień serwera",
    "bot_add": "dodane boty",
    "emoji_delete": "usunięte emoji (nie do cofnięcia)",
}

OPTIONS = {
    "guard_permissions": "straż uprawnień (tylko zaufani nadają groźne uprawnienia)",
    "block_bots": "blokowanie botów dodanych przez niezaufanych",
    "restore_messages": "odtwarzanie ostatnich wiadomości w odbudowanych kanałach",
    "raid": "ochrona przed rajdami",
}


def mark(value: bool) -> str:
    return "✅" if value else "❌"


async def snapshot_names(interaction: discord.Interaction, current: str) -> list[Choice[str]]:
    if interaction.guild_id is None:
        return []
    names = [p.stem for p in interaction.client.snapshots.list(interaction.guild_id)]
    return [Choice(name=n, value=n) for n in names if current in n][:25]


class Confirm(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=90)
        self.author_id = author_id
        self.value: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("To nie twoje potwierdzenie.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Tak, wykonaj", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.value = True
        await interaction.response.edit_message(content="⏳ Pracuję… to może potrwać kilka minut.", view=None)
        self.stop()

    @discord.ui.button(label="Anuluj", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.value = False
        await interaction.response.edit_message(content="Anulowano.", embed=None, view=None)
        self.stop()


class Commands(commands.Cog):
    tarcza = app_commands.Group(
        name="tarcza", description="Ustawienia ochrony serwera", guild_only=True, default_permissions=ADMIN
    )
    kopia = app_commands.Group(
        name="kopia", description="Kopie zapasowe struktury serwera", guild_only=True, default_permissions=ADMIN
    )
    rajd = app_commands.Group(
        name="rajd", description="Tryb ochrony przed rajdem", guild_only=True, default_permissions=ADMIN
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def config(self) -> Any:
        return self.bot.config

    async def _owner_only(self, interaction: discord.Interaction) -> bool:
        assert interaction.guild is not None
        if interaction.user.id == interaction.guild.owner_id:
            return True
        await interaction.response.send_message(
            "⛔ To może zmienić tylko właściciel serwera – żeby przejęte konto admina nie mogło wyłączyć ochrony.",
            ephemeral=True,
        )
        return False

    async def _trusted_only(self, interaction: discord.Interaction) -> bool:
        assert interaction.guild is not None
        if self.config.is_trusted(interaction.guild, interaction.user.id):
            return True
        await interaction.response.send_message(
            "⛔ Tylko właściciel serwera i osoby z listy `/tarcza zaufani`.", ephemeral=True
        )
        return False

    @tarcza.command(name="status", description="Sprawdza konfigurację bota i pokazuje ustawienia ochrony")
    async def status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        me = guild.me
        cfg = self.config.get(guild.id)
        perms = me.guild_permissions
        above = [r for r in reversed(guild.roles) if r > me.top_role]

        setup = [
            f"{mark(perms.administrator)} uprawnienie Administrator",
            f"{mark(not above)} rola bota na samej górze listy ról"
            + (f" – wyżej są: {', '.join(r.mention for r in above[:5])}" if above else ""),
            f"{mark(perms.view_audit_log)} dostęp do dziennika zdarzeń",
            f"{mark(self.bot.intents.members)} Server Members Intent",
            f"{mark(self.bot.intents.message_content)} Message Content Intent (do odtwarzania wiadomości)",
        ]

        raid_cog = self.bot.get_cog("AntiRaid")
        raid_active = bool(raid_cog and raid_cog.is_active(guild.id))
        log_channel = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        punishment = {"strip": "zabranie wszystkich ról", "kick": "wyrzucenie", "ban": "ban"}[cfg["punishment"]]
        raid_action = {"kick": "wyrzucenie", "ban": "ban", "timeout": "wyciszenie na dobę"}[cfg["raid"]["action"]]
        settings = [
            f"Kanał logów: {log_channel.mention if log_channel else '**brak** – ustaw `/tarcza logi`'}",
            f"Kara za atak: **{punishment}** (boty zawsze dostają bana)",
            f"Cofanie zmian z ostatnich **{cfg['rollback_minutes']} min**",
            f"{mark(cfg['guard_permissions'])} {OPTIONS['guard_permissions']}",
            f"{mark(cfg['block_bots'])} {OPTIONS['block_bots']}",
            f"{mark(cfg['restore_messages'] and self.bot.intents.message_content)} {OPTIONS['restore_messages']}",
            f"{mark(cfg['raid']['enabled'])} {OPTIONS['raid']}: {cfg['raid']['joins']} wejść / {cfg['raid']['seconds']} s, "
            f"klony od {cfg['raid']['cluster_size']} kont, kara: {raid_action}"
            + (" – **RAJD TRWA**" if raid_active else ""),
        ]
        thresholds = [f"{LABELS[k]}: **{v[0]}×** w {v[1]} s" for k, v in cfg["thresholds"].items() if k in LABELS]
        trusted = [f"<@{guild.owner_id}> (właściciel)"] + [f"<@{uid}>" for uid in cfg["trusted"]]

        snaps = self.bot.snapshots.list(guild.id)
        cache = self.bot.get_cog("MessageCache")
        cached, channels = cache.stats(guild) if cache else (0, 0)

        embed = discord.Embed(title="🛡️ Tarcza – status", colour=ORANGE if (above or not perms.administrator) else BLUE)
        embed.add_field(name="Konfiguracja", value="\n".join(setup), inline=False)
        embed.add_field(name="Ustawienia", value="\n".join(settings), inline=False)
        embed.add_field(name="Progi wykrywania ataku (jedna osoba)", value="\n".join(thresholds), inline=False)
        embed.add_field(name="Zaufani", value=", ".join(trusted)[:1000], inline=False)
        embed.add_field(
            name="Pamięć",
            value=(
                f"Kopie zapasowe: **{len(snaps)}**" + (f" (ostatnia: `{snaps[0].stem}`)" if snaps else "")
                + f"\nWiadomości w pamięci: **{cached}** z {channels} kanałów"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tarcza.command(name="logi", description="Ustawia kanał, na który bot wysyła raporty")
    @app_commands.describe(kanal="Kanał na raporty (najlepiej widoczny tylko dla administracji)")
    async def logi(self, interaction: discord.Interaction, kanal: discord.TextChannel) -> None:
        if not await self._trusted_only(interaction):
            return
        assert interaction.guild is not None
        self.config.get(interaction.guild.id)["log_channel_id"] = kanal.id
        self.config.save(interaction.guild.id)
        await interaction.response.send_message(f"✅ Raporty będą trafiać na {kanal.mention}.", ephemeral=True)

    @tarcza.command(name="zaufani", description="Osoby, których Tarcza nie blokuje (zmienia tylko właściciel)")
    @app_commands.describe(akcja="Co zrobić", uzytkownik="Kogo dodać lub usunąć")
    @app_commands.choices(
        akcja=[Choice(name="pokaż listę", value="list"), Choice(name="dodaj", value="add"), Choice(name="usuń", value="remove")]
    )
    async def zaufani(
        self, interaction: discord.Interaction, akcja: Choice[str], uzytkownik: Optional[discord.User] = None
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        cfg = self.config.get(guild.id)
        if akcja.value == "list":
            people = [f"<@{guild.owner_id}> (właściciel)"] + [f"<@{uid}>" for uid in cfg["trusted"]]
            await interaction.response.send_message("**Zaufani:** " + ", ".join(people), ephemeral=True)
            return
        if not await self._owner_only(interaction):
            return
        if uzytkownik is None:
            await interaction.response.send_message("Wskaż użytkownika.", ephemeral=True)
            return
        if akcja.value == "add":
            if uzytkownik.id not in cfg["trusted"]:
                cfg["trusted"].append(uzytkownik.id)
            text = f"✅ {uzytkownik.mention} jest teraz zaufany – Tarcza nie będzie go blokować."
        else:
            if uzytkownik.id in cfg["trusted"]:
                cfg["trusted"].remove(uzytkownik.id)
            text = f"✅ {uzytkownik.mention} nie jest już zaufany."
        self.config.save(guild.id)
        await interaction.response.send_message(text, ephemeral=True)

    @tarcza.command(name="kara", description="Co zrobić z osobą, która atakuje serwer (zmienia tylko właściciel)")
    @app_commands.choices(
        tryb=[
            Choice(name="zabierz wszystkie role (bezpieczne przy fałszywym alarmie)", value="strip"),
            Choice(name="wyrzuć z serwera", value="kick"),
            Choice(name="zbanuj", value="ban"),
        ]
    )
    async def kara(self, interaction: discord.Interaction, tryb: Choice[str]) -> None:
        if not await self._owner_only(interaction):
            return
        assert interaction.guild is not None
        self.config.get(interaction.guild.id)["punishment"] = tryb.value
        self.config.save(interaction.guild.id)
        await interaction.response.send_message(f"✅ Kara za atak: **{tryb.name}**.", ephemeral=True)

    @tarcza.command(name="prog", description="Zmienia próg wykrywania ataku (zmienia tylko właściciel)")
    @app_commands.describe(akcja="Rodzaj akcji", limit="Ile takich akcji…", sekundy="…w ilu sekundach uznać za atak")
    @app_commands.choices(akcja=[Choice(name=label, value=key) for key, label in LABELS.items()])
    async def prog(
        self,
        interaction: discord.Interaction,
        akcja: Choice[str],
        limit: app_commands.Range[int, 1, 100],
        sekundy: app_commands.Range[int, 1, 3600],
    ) -> None:
        if not await self._owner_only(interaction):
            return
        assert interaction.guild is not None
        self.config.get(interaction.guild.id)["thresholds"][akcja.value] = [limit, sekundy]
        self.config.save(interaction.guild.id)
        await interaction.response.send_message(
            f"✅ {akcja.name}: atak = **{limit}×** w **{sekundy} s** przez jedną osobę.", ephemeral=True
        )

    @tarcza.command(name="opcja", description="Włącza lub wyłącza funkcję ochrony (zmienia tylko właściciel)")
    @app_commands.choices(nazwa=[Choice(name=label, value=key) for key, label in OPTIONS.items()])
    async def opcja(self, interaction: discord.Interaction, nazwa: Choice[str], wlaczona: bool) -> None:
        if not await self._owner_only(interaction):
            return
        assert interaction.guild is not None
        cfg = self.config.get(interaction.guild.id)
        if nazwa.value == "raid":
            cfg["raid"]["enabled"] = wlaczona
        else:
            cfg[nazwa.value] = wlaczona
        self.config.save(interaction.guild.id)
        extra = ""
        if nazwa.value == "restore_messages" and wlaczona and not self.bot.intents.message_content:
            extra = "\n⚠️ Bot działa bez Message Content Intent – włącz go w Developer Portal i ustaw ODTWARZAJ_WIADOMOSCI=1."
        await interaction.response.send_message(
            f"{mark(wlaczona)} {nazwa.name}: **{'włączone' if wlaczona else 'wyłączone'}**{extra}", ephemeral=True
        )

    @kopia.command(name="zrob", description="Robi teraz kopię ról, kanałów i uprawnień")
    async def kopia_zrob(self, interaction: discord.Interaction) -> None:
        if not await self._trusted_only(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        path = self.bot.snapshots.save(guild, force=True)
        await interaction.response.send_message(
            f"✅ Zapisano kopię `{path.stem}`: {len(guild.roles) - 1} ról, {len(guild.channels)} kanałów.\n"
            "Bot robi też kopie sam co 30 minut (gdy coś się zmieniło).",
            ephemeral=True,
        )

    @kopia.command(name="lista", description="Pokazuje zapisane kopie")
    async def kopia_lista(self, interaction: discord.Interaction) -> None:
        if not await self._trusted_only(interaction):
            return
        assert interaction.guild is not None
        store = self.bot.snapshots
        lines = []
        for path in store.list(interaction.guild.id)[:15]:
            snap = store.load(interaction.guild.id, path.stem)
            if snap:
                lines.append(
                    f"`{path.stem}` – <t:{int(snap['taken_at'])}:R> – {len(snap['roles']) - 1} ról, {len(snap['channels'])} kanałów"
                )
        await interaction.response.send_message("\n".join(lines) or "Brak kopii. Zrób pierwszą: `/kopia zrob`.", ephemeral=True)

    @kopia.command(name="porownaj", description="Pokazuje, czego brakuje na serwerze w porównaniu z kopią")
    @app_commands.autocomplete(nazwa=snapshot_names)
    async def kopia_porownaj(self, interaction: discord.Interaction, nazwa: str) -> None:
        if not await self._trusted_only(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        snap = self.bot.snapshots.load(guild.id, nazwa)
        if snap is None:
            await interaction.response.send_message("Nie ma takiej kopii.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self._missing_embed(guild, snap, nazwa), ephemeral=True)

    def _missing_embed(self, guild: discord.Guild, snap: dict, name: str) -> discord.Embed:
        roles, channels = snapshot_missing(self.bot, guild, snap)
        embed = discord.Embed(title=f"Porównanie z kopią {name}", colour=BLUE)
        embed.add_field(
            name=f"Brakujące role ({len(roles)})",
            value=(", ".join(discord.utils.escape_markdown(r["name"]) for r in roles) or "—")[:1000],
            inline=False,
        )
        embed.add_field(
            name=f"Brakujące kanały ({len(channels)})",
            value=(", ".join("#" + discord.utils.escape_markdown(c["name"]) for c in channels) or "—")[:1000],
            inline=False,
        )
        return embed

    @kopia.command(name="przywroc", description="Odtwarza brakujące role i kanały z kopii (tylko właściciel)")
    @app_commands.autocomplete(nazwa=snapshot_names)
    async def kopia_przywroc(self, interaction: discord.Interaction, nazwa: str) -> None:
        if not await self._owner_only(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        snap = self.bot.snapshots.load(guild.id, nazwa)
        if snap is None:
            await interaction.response.send_message("Nie ma takiej kopii.", ephemeral=True)
            return
        roles, channels = snapshot_missing(self.bot, guild, snap)
        if not roles and not channels:
            await interaction.response.send_message("✅ Niczego nie brakuje – serwer zgadza się z tą kopią.", ephemeral=True)
            return
        view = Confirm(interaction.user.id)
        await interaction.response.send_message(
            content="Odtworzyć to, czego brakuje? Istniejące kanały i role nie zostaną ruszone.",
            embed=self._missing_embed(guild, snap, nazwa),
            view=view,
            ephemeral=True,
        )
        if await view.wait() or not view.value:
            return
        restorer = await restore_from_snapshot(self.bot, guild, snap, reason=f"Tarcza: przywracanie kopii {nazwa}")
        embed = report_embed(f"♻️ Przywrócono kopię {nazwa}", restorer)
        await send_log(self.bot, guild, embed)
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

    @rajd.command(name="start", description="Ręcznie włącza tryb anty-rajd (wstrzymuje zaproszenia)")
    async def rajd_start(self, interaction: discord.Interaction) -> None:
        if not await self._trusted_only(interaction):
            return
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        await self.bot.get_cog("AntiRaid").start_raid(
            interaction.guild, "włączony ręcznie", manual_by=interaction.user.id
        )
        await interaction.followup.send("🛡️ Tryb anty-rajd włączony.", ephemeral=True)

    @rajd.command(name="koniec", description="Wyłącza tryb anty-rajd i przywraca zaproszenia")
    async def rajd_koniec(self, interaction: discord.Interaction) -> None:
        if not await self._trusted_only(interaction):
            return
        assert interaction.guild is not None
        cog = self.bot.get_cog("AntiRaid")
        if not cog.is_active(interaction.guild.id):
            await interaction.response.send_message("Tryb anty-rajd nie jest włączony.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await cog.end_raid(interaction.guild, manual_by=interaction.user.id)
        await interaction.followup.send("✅ Tryb anty-rajd wyłączony.", ephemeral=True)

    @rajd.command(name="ustaw", description="Ustawienia wykrywania rajdów (tylko właściciel)")
    @app_commands.describe(
        wejscia="Ile wejść…",
        sekundy="…w ilu sekundach to rajd",
        wiek_konta_dni="Konta młodsze niż tyle dni są usuwane w trakcie rajdu",
        klony="Od ilu podobnych kont z jednej partii uznać je za rajd",
        akcja="Co robić z kontami z rajdu",
    )
    @app_commands.choices(
        akcja=[
            Choice(name="wyrzuć", value="kick"),
            Choice(name="zbanuj", value="ban"),
            Choice(name="wycisz na dobę", value="timeout"),
        ]
    )
    async def rajd_ustaw(
        self,
        interaction: discord.Interaction,
        wejscia: Optional[app_commands.Range[int, 2, 200]] = None,
        sekundy: Optional[app_commands.Range[int, 5, 600]] = None,
        wiek_konta_dni: Optional[app_commands.Range[int, 0, 365]] = None,
        klony: Optional[app_commands.Range[int, 2, 50]] = None,
        akcja: Optional[Choice[str]] = None,
    ) -> None:
        if not await self._owner_only(interaction):
            return
        assert interaction.guild is not None
        raid = self.config.get(interaction.guild.id)["raid"]
        for key, value in (
            ("joins", wejscia),
            ("seconds", sekundy),
            ("min_account_age_days", wiek_konta_dni),
            ("cluster_size", klony),
        ):
            if value is not None:
                raid[key] = value
        if akcja is not None:
            raid["action"] = akcja.value
        self.config.save(interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Rajd = **{raid['joins']}** wejść w **{raid['seconds']} s** albo **{raid['cluster_size']}** kont-klonów. "
            f"W trakcie rajdu konta młodsze niż **{raid['min_account_age_days']} dni**: `{raid['action']}`.",
            ephemeral=True,
        )

    @app_commands.command(name="cofnij", description="Cofa wszystko, co dana osoba zrobiła na serwerze w ostatnich minutach")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(uzytkownik="Czyje zmiany cofnąć", minuty="Z ilu ostatnich minut (max 1440 = doba)")
    async def cofnij(
        self,
        interaction: discord.Interaction,
        uzytkownik: discord.User,
        minuty: app_commands.Range[int, 1, 1440] = 30,
    ) -> None:
        if not await self._trusted_only(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        if self.bot.user and uzytkownik.id == self.bot.user.id:
            await interaction.response.send_message("Nie cofam własnych zmian Tarczy.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        since = discord.utils.utcnow() - dt.timedelta(minutes=minuty)
        try:
            entries = [
                e async for e in guild.audit_logs(limit=1000, user=uzytkownik, after=since, oldest_first=True)
            ]
        except discord.Forbidden:
            await interaction.followup.send("⛔ Bot nie ma dostępu do dziennika zdarzeń.", ephemeral=True)
            return
        counts = Counter(ACTION_PL.get(e.action.name, "inne (pominięte)") for e in entries)
        if not entries:
            await interaction.followup.send(
                f"{uzytkownik.mention} nic nie zmieniał w ostatnich {minuty} min.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"Cofnąć zmiany {uzytkownik} z ostatnich {minuty} min?",
            colour=ORANGE,
            description="\n".join(f"• {name}: **{n}**" for name, n in counts.most_common()),
        )
        view = Confirm(interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        if await view.wait() or not view.value:
            return
        cog = self.bot.get_cog("AntiNuke")
        restorer = await cog.manual_undo(
            guild, entries, reason=f"Tarcza: /cofnij {uzytkownik} ({minuty} min), zlecił {interaction.user}"
        )
        report = report_embed(f"♻️ Cofnięto zmiany {uzytkownik}", restorer)
        await send_log(self.bot, guild, report)
        try:
            await interaction.followup.send(embed=report, ephemeral=True)
        except discord.HTTPException:
            pass
