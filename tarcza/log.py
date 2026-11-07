from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from .restore import Restorer

RED = discord.Colour.from_rgb(237, 66, 69)
GREEN = discord.Colour.from_rgb(87, 242, 135)
ORANGE = discord.Colour.from_rgb(250, 166, 26)
BLUE = discord.Colour.from_rgb(88, 101, 242)


async def send_log(bot: "Bot", guild: discord.Guild, embed: discord.Embed, *, dm_owner: bool = False) -> None:
    cfg = bot.config.get(guild.id)
    channel = guild.get_channel(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None
    if isinstance(channel, discord.abc.Messageable):
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
    if dm_owner and guild.owner is not None:
        try:
            await guild.owner.send(content=f"Wiadomość z serwera **{discord.utils.escape_markdown(guild.name)}**:", embed=embed)
        except discord.HTTPException:
            pass


def report_embed(title: str, restorer: "Restorer", *, colour: discord.Colour = GREEN) -> discord.Embed:
    text = "\n".join(restorer.done) or "Nie było nic do odtworzenia ani cofnięcia."
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    embed = discord.Embed(title=title, description=text, colour=colour)
    if restorer.failed:
        failed = "\n".join(f"• {f}" for f in restorer.failed)
        if len(failed) > 1000:
            failed = failed[:1000] + "\n…"
        embed.add_field(name=f"⚠️ Uwagi ({len(restorer.failed)})", value=failed, inline=False)
    return embed
