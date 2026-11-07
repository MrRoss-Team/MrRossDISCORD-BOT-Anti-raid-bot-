from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from .restore import Restorer

log = logging.getLogger("tarcza.messages")

PER_CHANNEL = 50
DEAD_TTL = 60 * 60
_CACHEABLE = (discord.TextChannel, discord.VoiceChannel)
_KEEP_TYPES = (discord.MessageType.default, discord.MessageType.reply)


def safe_webhook_name(name: str) -> str:
    name = re.sub(r"(?i)(d)(iscord)|(c)(lyde)", lambda m: "​".join(g for g in m.groups() if g), name)
    name = name.strip()[:80]
    return name or "Użytkownik"


def pack(message: discord.Message) -> dict:
    return {
        "id": message.id,
        "author": message.author.display_name,
        "avatar": message.author.display_avatar.url,
        "content": message.content,
        "files": [a.filename for a in message.attachments],
        "embeds": len(message.embeds),
        "ts": int(message.created_at.timestamp()),
    }


class MessageCache(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.channels: dict[int, deque[dict]] = {}
        self.dead: dict[int, tuple[float, list[dict]]] = {}
        self._backfill_started = False

    def enabled(self, guild: Optional[discord.Guild]) -> bool:
        if guild is None or not self.bot.intents.message_content:
            return False
        return bool(self.bot.config.get(guild.id)["restore_messages"])

    def stats(self, guild: discord.Guild) -> tuple[int, int]:
        ids = {c.id for c in guild.channels}
        counts = [len(dq) for cid, dq in self.channels.items() if cid in ids]
        return sum(counts), len([c for c in counts if c])

    def _queue(self, channel_id: int) -> deque[dict]:
        dq = self.channels.get(channel_id)
        if dq is None:
            dq = self.channels[channel_id] = deque(maxlen=PER_CHANNEL)
        return dq

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not isinstance(message.channel, _CACHEABLE) or message.type not in _KEEP_TYPES:
            return
        if not self.enabled(message.guild):
            return
        self._queue(message.channel.id).append(pack(message))

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        dq = self.channels.get(payload.channel_id)
        if not dq or "content" not in payload.data:
            return
        for item in dq:
            if item["id"] == payload.message_id:
                item["content"] = payload.data["content"]
                break

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        dq = self.channels.get(payload.channel_id)
        if dq:
            self.channels[payload.channel_id] = deque((m for m in dq if m["id"] != payload.message_id), maxlen=PER_CHANNEL)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        dq = self.channels.get(payload.channel_id)
        if dq:
            ids = payload.message_ids
            self.channels[payload.channel_id] = deque((m for m in dq if m["id"] not in ids), maxlen=PER_CHANNEL)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        messages = self.channels.pop(channel.id, None)
        if messages:
            self.dead[channel.id] = (time.time(), list(messages))
        limit = time.time() - DEAD_TTL
        self.dead = {k: v for k, v in self.dead.items() if v[0] > limit}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._backfill_started:
            self._backfill_started = True
            asyncio.create_task(self._backfill())

    async def _backfill(self) -> None:
        for guild in list(self.bot.guilds):
            if not self.enabled(guild):
                continue
            for channel in list(guild.text_channels) + list(guild.voice_channels):
                perms = channel.permissions_for(guild.me)
                if not (perms.read_messages and perms.read_message_history) or self.channels.get(channel.id):
                    continue
                try:
                    batch = [pack(m) async for m in channel.history(limit=PER_CHANNEL) if m.type in _KEEP_TYPES]
                except discord.HTTPException:
                    continue
                dq = self._queue(channel.id)
                existing = {m["id"] for m in dq}
                merged = sorted([m for m in batch if m["id"] not in existing] + list(dq), key=lambda m: m["id"])
                self.channels[channel.id] = deque(merged, maxlen=PER_CHANNEL)
                await asyncio.sleep(1.0)
        log.info("Wczytano ostatnie wiadomości z kanałów.")

    async def replay(self, channel: discord.abc.GuildChannel, messages: list[dict]) -> int:
        if not messages or not hasattr(channel, "create_webhook"):
            return 0
        try:
            webhook = await channel.create_webhook(name="Tarcza – archiwum", reason="Odtwarzanie wiadomości")
        except discord.HTTPException:
            return 0
        sent = 0
        try:
            await webhook.send(
                f"♻️ **Ten kanał został usunięty i odtworzony przez Tarczę.** "
                f"Poniżej {len(messages)} ostatnich wiadomości sprzed usunięcia.",
                username="Tarcza",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            for m in messages:
                parts = []
                if m["content"]:
                    parts.append(m["content"])
                for name in m["files"]:
                    parts.append(f"📎 *{discord.utils.escape_markdown(name)}* (załącznik przepadł razem z kanałem)")
                if not parts and m["embeds"]:
                    parts.append("*[wiadomość z osadzeniem]*")
                if not parts:
                    continue
                stamp = f"\n-# 🕓 <t:{m['ts']}:f>"
                text = "\n".join(parts)[: 2000 - len(stamp)] + stamp
                await webhook.send(
                    text,
                    username=safe_webhook_name(m["author"]),
                    avatar_url=m["avatar"],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent += 1
        except discord.HTTPException as exc:
            log.warning("Odtwarzanie wiadomości przerwane: %s", exc)
        finally:
            try:
                await webhook.delete(reason="Koniec odtwarzania wiadomości")
            except discord.HTTPException:
                pass
        return sent

    async def replay_into(self, restorer: "Restorer") -> None:
        if not self.enabled(restorer.guild):
            return
        for old_id, channel in restorer.restored_text:
            dead = self.dead.pop(old_id, None)
            if not dead:
                continue
            sent = await self.replay(channel, dead[1])
            if sent:
                restorer.done.append(f"💬 odtworzono {sent} wiadomości na {channel.mention}")
