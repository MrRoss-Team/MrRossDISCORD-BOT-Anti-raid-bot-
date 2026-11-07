from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Iterable, Optional, Sequence

import discord
from discord.utils import escape_markdown

from .state import overwrite_target_type, serialize_overwrites

if TYPE_CHECKING:
    from discord.ext.commands import Bot

A = discord.AuditLogAction


def resolve(mapping: dict[int, int], old_id: int) -> int:
    seen: set[int] = set()
    current = old_id
    while current in mapping and current not in seen:
        seen.add(current)
        current = mapping[current]
    return current


def _err(exc: Exception) -> str:
    return getattr(exc, "text", None) or str(exc)


def _md(text: str) -> str:
    return escape_markdown(text or "")


class Restorer:
    def __init__(
        self,
        guild: discord.Guild,
        reason: str,
        *,
        channel_map: Optional[dict[int, int]] = None,
        role_map: Optional[dict[int, int]] = None,
    ) -> None:
        self.guild = guild
        self.reason = reason[:500]
        self.channel_map = dict(channel_map or {})
        self.role_map = dict(role_map or {})
        self.new_channels: dict[int, int] = {}
        self.new_roles: dict[int, int] = {}
        self.restored_text: list[tuple[int, discord.abc.GuildChannel]] = []
        self.done: list[str] = []
        self.failed: list[str] = []

    def role(self, old_id: int) -> Optional[discord.Role]:
        return self.guild.get_role(resolve(self.role_map, old_id))

    def channel(self, old_id: int) -> Optional[discord.abc.GuildChannel]:
        return self.guild.get_channel(resolve(self.channel_map, old_id))

    def fail(self, what: str, exc: Exception) -> None:
        self.failed.append(f"{what}: {_err(exc)}")

    def overwrites(self, items: Iterable[dict]) -> dict[Any, discord.PermissionOverwrite]:
        result: dict[Any, discord.PermissionOverwrite] = {}
        for ow in items:
            target = self.role(ow["id"]) if ow["type"] == "role" else self.guild.get_member(ow["id"])
            if target is None:
                continue
            result[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"])
            )
        return result

    async def create_role(self, data: dict) -> Optional[discord.Role]:
        if data.get("managed") or data.get("default") or self.role(data["id"]) is not None:
            return None
        try:
            role = await self.guild.create_role(
                name=data["name"],
                permissions=discord.Permissions(data["permissions"]),
                colour=discord.Colour(data.get("color", 0)),
                hoist=bool(data.get("hoist")),
                mentionable=bool(data.get("mentionable")),
                reason=self.reason,
            )
        except discord.HTTPException as exc:
            self.fail(f"rola {data['name']}", exc)
            return None
        self.role_map[data["id"]] = role.id
        self.new_roles[data["id"]] = role.id
        self.done.append(f"🎭 odtworzono rolę **{_md(data['name'])}**")
        return role

    async def position_roles(self, pairs: Sequence[tuple[discord.Role, int]]) -> None:
        if not pairs:
            return
        top = self.guild.me.top_role.position
        positions = {role: max(1, min(int(pos), top - 1)) for role, pos in sorted(pairs, key=lambda p: p[1])}
        try:
            await self.guild.edit_role_positions(positions=positions, reason=self.reason)
        except discord.HTTPException as exc:
            self.fail("kolejność ról", exc)

    async def restore_role_overwrites(self, role: discord.Role, overwrites: dict[int, dict]) -> None:
        count = 0
        for cid, ow in overwrites.items():
            channel = self.channel(int(cid))
            if channel is None:
                continue
            overwrite = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"])
            )
            try:
                await channel.set_permissions(role, overwrite=overwrite, reason=self.reason)
                count += 1
            except discord.HTTPException as exc:
                self.fail(f"uprawnienia {role.name} na #{channel.name}", exc)
        if count:
            self.done.append(f"🔐 przywrócono uprawnienia roli **{_md(role.name)}** na {count} kanałach")

    async def add_members(self, role: discord.Role, member_ids: Iterable[int]) -> None:
        added = 0
        for mid in member_ids:
            member = self.guild.get_member(int(mid))
            if member is None or role in member.roles:
                continue
            try:
                await member.add_roles(role, reason=self.reason)
                added += 1
            except discord.HTTPException:
                pass
        if added:
            self.done.append(f"👥 oddano rolę **{_md(role.name)}** {added} osobom")

    async def create_channel(self, data: dict) -> Optional[discord.abc.GuildChannel]:
        if self.channel(data["id"]) is not None:
            return None
        g = self.guild
        kind = data.get("type", "text")
        parent = self.channel(data["parent_id"]) if data.get("parent_id") else None
        if not isinstance(parent, discord.CategoryChannel):
            parent = None

        base: dict[str, Any] = {
            "name": data["name"],
            "overwrites": self.overwrites(data.get("overwrites", [])),
            "reason": self.reason,
            "position": int(data.get("position", 0)),
        }
        text_opts: dict[str, Any] = {"nsfw": bool(data.get("nsfw")), "slowmode_delay": int(data.get("slowmode") or 0)}
        if data.get("topic"):
            text_opts["topic"] = data["topic"]

        try:
            if kind == "category":
                channel = await g.create_category(**base)
            elif kind in ("text", "news"):
                try:
                    channel = await g.create_text_channel(category=parent, news=kind == "news", **base, **text_opts)
                except discord.HTTPException:
                    if kind != "news":
                        raise
                    channel = await g.create_text_channel(category=parent, **base, **text_opts)
            elif kind in ("voice", "stage_voice"):
                voice_opts = {
                    "bitrate": min(int(data.get("bitrate") or 64000), int(g.bitrate_limit)),
                    "user_limit": int(data.get("user_limit") or 0),
                    "rtc_region": data.get("rtc_region"),
                }
                if kind == "voice":
                    channel = await g.create_voice_channel(category=parent, **base, **voice_opts)
                else:
                    channel = await g.create_stage_channel(category=parent, **base, **voice_opts)
            elif kind in ("forum", "media"):
                tags = [
                    discord.ForumTag(name=t["name"], emoji=t.get("emoji"), moderated=bool(t.get("moderated")))
                    for t in data.get("tags", [])
                ]
                channel = await g.create_forum(
                    category=parent, media=kind == "media", available_tags=tags, **base, **text_opts
                )
            else:
                self.failed.append(f"kanał {data['name']}: nieobsługiwany typ {kind}")
                return None
        except discord.HTTPException as exc:
            self.fail(f"kanał {data['name']}", exc)
            return None

        self.channel_map[data["id"]] = channel.id
        self.new_channels[data["id"]] = channel.id
        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            self.restored_text.append((data["id"], channel))
        icon = "📁" if kind == "category" else "#️⃣"
        self.done.append(f"{icon} odtworzono kanał **{_md(data['name'])}**")
        return channel

    async def restore_channels(self, items: Sequence[tuple[dict, list[int]]]) -> None:
        categories = [x for x in items if x[0].get("type") == "category"]
        others = [x for x in items if x[0].get("type") != "category"]
        for data, _ in sorted(categories, key=lambda x: x[0].get("position", 0)):
            await self.create_channel(data)
        for data, _ in sorted(others, key=lambda x: x[0].get("position", 0)):
            await self.create_channel(data)

        for data, children in categories:
            category = self.channel(data["id"])
            if not isinstance(category, discord.CategoryChannel):
                continue
            moved = 0
            for cid in dict.fromkeys(children):
                ch = self.channel(cid)
                if ch is None or isinstance(ch, discord.CategoryChannel) or ch.category_id is not None:
                    continue
                try:
                    await ch.edit(category=category, sync_permissions=False, reason=self.reason)
                    moved += 1
                except discord.HTTPException as exc:
                    self.fail(f"przeniesienie #{ch.name}", exc)
            if moved:
                self.done.append(f"↩️ {moved} kanałów wróciło do kategorii **{_md(category.name)}**")


def role_from_audit(entry: discord.AuditLogEntry) -> dict:
    b = entry.before
    return {
        "id": entry.target.id if entry.target else 0,
        "name": getattr(b, "name", None) or "odtworzona-rola",
        "permissions": getattr(b, "permissions", discord.Permissions.none()).value,
        "color": getattr(b, "colour", discord.Colour.default()).value,
        "hoist": bool(getattr(b, "hoist", False)),
        "mentionable": bool(getattr(b, "mentionable", False)),
        "position": 1,
        "managed": False,
        "default": False,
        "members": [],
    }


def channel_from_audit(entry: discord.AuditLogEntry) -> dict:
    b = entry.before
    kind = getattr(b, "type", discord.ChannelType.text)
    return {
        "id": entry.target.id if entry.target else 0,
        "name": getattr(b, "name", None) or "odtworzony-kanal",
        "type": kind.name if isinstance(kind, discord.ChannelType) else "text",
        "position": getattr(b, "position", 0) or 0,
        "parent_id": None,
        "overwrites": serialize_overwrites(getattr(b, "overwrites", [])),
        "topic": getattr(b, "topic", None),
        "nsfw": bool(getattr(b, "nsfw", False)),
        "slowmode": getattr(b, "slowmode_delay", 0) or 0,
        "bitrate": getattr(b, "bitrate", 64000) or 64000,
        "user_limit": getattr(b, "user_limit", 0) or 0,
    }


def _target_id(entry: discord.AuditLogEntry) -> Optional[int]:
    return getattr(entry.target, "id", None)


async def undo_entries(bot: "Bot", guild: discord.Guild, entries: Sequence[discord.AuditLogEntry], *, reason: str) -> Restorer:
    state = bot.get_cog("StateTracker").get(guild)
    snapshots = bot.snapshots
    config = bot.config
    channel_map, role_map = config.id_maps(guild.id)
    r = Restorer(guild, reason, channel_map=channel_map, role_map=role_map)
    me = guild.me

    by: dict[discord.AuditLogAction, list[discord.AuditLogEntry]] = defaultdict(list)
    for entry in sorted(entries, key=lambda e: e.id):
        by[entry.action].append(entry)

    created_channels = {_target_id(e) for e in by[A.channel_create]} - {None}
    created_roles = {_target_id(e) for e in by[A.role_create]} - {None}

    restored_roles: list[tuple[discord.Role, dict, list[int], dict[int, dict]]] = []
    for e in by[A.role_delete]:
        rid = _target_id(e)
        if rid is None or rid in created_roles or r.role(rid) is not None:
            continue
        dead = state.dead_roles.get(rid)
        if dead:
            data, members, ows = dead.data, dead.members, dead.overwrites
        else:
            found = snapshots.find_role(guild.id, rid)
            if found:
                data, ows = found
                members = data.get("members", [])
            else:
                data, members, ows = role_from_audit(e), [], {}
        role = await r.create_role(data)
        if role:
            state.dead_roles.pop(rid, None)
            restored_roles.append((role, data, members, ows))
    await r.position_roles([(role, data.get("position", 1)) for role, data, _, _ in restored_roles])

    to_create: list[tuple[dict, list[int]]] = []
    for e in by[A.channel_delete]:
        cid = _target_id(e)
        if cid is None or cid in created_channels or r.channel(cid) is not None:
            continue
        dead = state.dead_channels.get(cid)
        if dead:
            data, children = dead.data, list(dead.children)
        else:
            found = snapshots.find_channel(guild.id, cid)
            data, children = found if found else (channel_from_audit(e), [])
        children = children + list(state.orphans.get(cid, ()))
        to_create.append((data, children))
    await r.restore_channels(to_create)
    for data, _ in to_create:
        if data["id"] in r.new_channels:
            state.dead_channels.pop(data["id"], None)
            state.orphans.pop(data["id"], None)

    for role, _data, members, ows in restored_roles:
        await r.restore_role_overwrites(role, ows)
        await r.add_members(role, members)

    cache = bot.get_cog("MessageCache")
    if cache is not None:
        await cache.replay_into(r)

    removed = 0
    for cid in created_channels:
        ch = guild.get_channel(cid)
        if ch is None:
            continue
        try:
            await ch.delete(reason=reason)
            removed += 1
        except discord.HTTPException as exc:
            r.fail(f"usuwanie #{ch.name}", exc)
    if removed:
        r.done.append(f"🧹 usunięto {removed} kanałów założonych przez sprawcę")

    removed = 0
    for rid in created_roles:
        role = guild.get_role(rid)
        if role is None or role.managed or role >= me.top_role:
            continue
        try:
            await role.delete(reason=reason)
            removed += 1
        except discord.HTTPException as exc:
            r.fail(f"usuwanie roli {role.name}", exc)
    if removed:
        r.done.append(f"🧹 usunięto {removed} ról założonych przez sprawcę")

    hook_ids = {_target_id(e) for e in by[A.webhook_create]} - {None}
    if hook_ids:
        removed = 0
        try:
            for hook in await guild.webhooks():
                if hook.id in hook_ids:
                    await hook.delete(reason=reason)
                    removed += 1
        except discord.HTTPException as exc:
            r.fail("usuwanie webhooków", exc)
        if removed:
            r.done.append(f"🪝 usunięto {removed} webhooków (często używanych do spamu)")

    unbanned = 0
    for e in by[A.ban]:
        uid = _target_id(e)
        if uid is None:
            continue
        try:
            await guild.unban(discord.Object(uid), reason=reason)
            unbanned += 1
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            r.fail(f"odbanowanie {uid}", exc)
    if unbanned:
        r.done.append(f"🔓 odbanowano {unbanned} osób")

    fixed = 0
    for e in reversed(by[A.role_update]):
        rid = _target_id(e)
        role = r.role(rid) if rid else None
        if role is None or role >= me.top_role:
            continue
        attrs = ("permissions",) if role.is_default() else ("name", "permissions", "colour", "hoist", "mentionable")
        kwargs = {a: getattr(e.before, a) for a in attrs if hasattr(e.before, a)}
        if not kwargs:
            continue
        try:
            await role.edit(reason=reason, **kwargs)
            fixed += 1
        except discord.HTTPException as exc:
            r.fail(f"przywracanie roli {role.name}", exc)
    if fixed:
        r.done.append(f"🛠️ cofnięto {fixed} zmian ról (nazwy, uprawnienia, kolory)")

    fixed = 0
    for e in reversed(by[A.channel_update]):
        cid = _target_id(e)
        ch = r.channel(cid) if cid else None
        if ch is None:
            continue
        allowed = {"name"}
        if isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
            allowed |= {"topic", "nsfw", "slowmode_delay"}
        elif isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            allowed |= {"bitrate", "user_limit", "nsfw"}
        kwargs = {a: getattr(e.before, a) for a in allowed if hasattr(e.before, a)}
        if "topic" in kwargs and kwargs["topic"] is None:
            kwargs["topic"] = ""
        if not kwargs:
            continue
        try:
            await ch.edit(reason=reason, **kwargs)
            fixed += 1
        except discord.HTTPException as exc:
            r.fail(f"przywracanie #{ch.name}", exc)
    if fixed:
        r.done.append(f"🛠️ cofnięto {fixed} zmian kanałów (nazwy, opisy)")

    fixed = 0
    ow_entries = by[A.overwrite_create] + by[A.overwrite_update] + by[A.overwrite_delete]
    for e in sorted(ow_entries, key=lambda x: x.id, reverse=True):
        cid = _target_id(e)
        if cid is None or cid in created_channels or e.extra is None:
            continue
        ch = r.channel(cid)
        if ch is None:
            continue
        if overwrite_target_type(e.extra) == "role":
            target: Any = r.role(e.extra.id)
        else:
            target = guild.get_member(e.extra.id)
        if target is None:
            continue
        try:
            if e.action is A.overwrite_create:
                await ch.set_permissions(target, overwrite=None, reason=reason)
            else:
                cur_allow, cur_deny = ch.overwrites_for(target).pair()
                allow = getattr(e.before, "allow", cur_allow)
                deny = getattr(e.before, "deny", cur_deny)
                await ch.set_permissions(
                    target, overwrite=discord.PermissionOverwrite.from_pair(allow, deny), reason=reason
                )
            fixed += 1
        except discord.HTTPException as exc:
            r.fail(f"uprawnienia na #{ch.name}", exc)
    if fixed:
        r.done.append(f"🔐 cofnięto {fixed} zmian uprawnień na kanałach")

    fixed = 0
    for e in reversed(by[A.member_role_update]):
        uid = _target_id(e)
        member = guild.get_member(uid) if uid else None
        if member is None:
            continue
        added = [r.role(x.id) for x in getattr(e.after, "roles", [])]
        taken = [r.role(x.id) for x in getattr(e.before, "roles", [])]
        to_remove = [x for x in added if x and not x.managed and x < me.top_role]
        to_add = [x for x in taken if x and not x.managed and not x.is_default() and x < me.top_role]
        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason=reason)
            if to_add:
                await member.add_roles(*to_add, reason=reason)
            if to_remove or to_add:
                fixed += 1
        except discord.HTTPException as exc:
            r.fail(f"role członka {member}", exc)
    if fixed:
        r.done.append(f"👥 cofnięto zmiany ról u {fixed} członków")

    fixed = 0
    now = dt.datetime.now(dt.timezone.utc)
    for e in reversed(by[A.member_update]):
        uid = _target_id(e)
        member = guild.get_member(uid) if uid else None
        if member is None or member.id == guild.owner_id or member.top_role >= me.top_role:
            continue
        kwargs: dict[str, Any] = {}
        if hasattr(e.before, "nick"):
            kwargs["nick"] = e.before.nick
        if getattr(e.after, "timed_out_until", None):
            previous = getattr(e.before, "timed_out_until", None)
            kwargs["timed_out_until"] = previous if previous and previous > now else None
        if not kwargs:
            continue
        try:
            await member.edit(reason=reason, **kwargs)
            fixed += 1
        except discord.HTTPException as exc:
            r.fail(f"przywracanie {member}", exc)
    if fixed:
        r.done.append(f"🏷️ cofnięto zmiany nicków / wyciszeń u {fixed} osób")

    guild_kwargs: dict[str, Any] = {}
    icon_changed = False
    for e in reversed(by[A.guild_update]):
        for attr in ("name", "description", "verification_level", "explicit_content_filter", "default_notifications"):
            if hasattr(e.before, attr):
                guild_kwargs[attr] = getattr(e.before, attr)
        icon_changed = icon_changed or hasattr(e.after, "icon") or hasattr(e.after, "banner")
    if guild_kwargs:
        try:
            await guild.edit(reason=reason, **guild_kwargs)
            r.done.append("🏠 przywrócono ustawienia serwera (" + ", ".join(guild_kwargs) + ")")
        except discord.HTTPException as exc:
            r.fail("ustawienia serwera", exc)
    if icon_changed:
        r.failed.append("ikona/baner serwera zostały zmienione – tego nie da się cofnąć automatycznie")

    for e in by[A.bot_add]:
        uid = _target_id(e)
        member = guild.get_member(uid) if uid else None
        if member and member.bot and member.top_role < me.top_role:
            try:
                await member.kick(reason=reason)
                r.done.append(f"🤖 wyrzucono bota **{_md(member.name)}** dodanego przez sprawcę")
            except discord.HTTPException as exc:
                r.fail(f"wyrzucanie bota {member}", exc)

    kicked = [_target_id(e) for e in by[A.kick]]
    if kicked:
        who = ", ".join(f"<@{uid}>" for uid in kicked[:15] if uid)
        more = f" i {len(kicked) - 15} innych" if len(kicked) > 15 else ""
        r.failed.append(f"wyrzucenia nie da się cofnąć – wyrzuceni: {who}{more} (muszą wejść ponownie przez zaproszenie)")
    if by[A.emoji_delete]:
        r.failed.append(f"usunięto {len(by[A.emoji_delete])} emoji – Discord nie pozwala ich odzyskać")

    config.remember_ids(guild.id, r.new_channels, r.new_roles)
    return r


def snapshot_missing(bot: "Bot", guild: discord.Guild, snap: dict) -> tuple[list[dict], list[dict]]:
    channel_map, role_map = bot.config.id_maps(guild.id)
    roles = [
        d
        for d in snap["roles"]
        if not d.get("managed") and not d.get("default") and guild.get_role(resolve(role_map, d["id"])) is None
    ]
    channels = [d for d in snap["channels"] if guild.get_channel(resolve(channel_map, d["id"])) is None]
    return roles, channels


async def restore_from_snapshot(bot: "Bot", guild: discord.Guild, snap: dict, *, reason: str) -> Restorer:
    from .snapshots import role_overwrites

    channel_map, role_map = bot.config.id_maps(guild.id)
    r = Restorer(guild, reason, channel_map=channel_map, role_map=role_map)
    missing_roles, missing_channels = snapshot_missing(bot, guild, snap)

    restored: list[tuple[discord.Role, dict]] = []
    for data in sorted(missing_roles, key=lambda d: d.get("position", 0)):
        role = await r.create_role(data)
        if role:
            restored.append((role, data))
    await r.position_roles([(role, data.get("position", 1)) for role, data in restored])

    items = []
    for data in missing_channels:
        children = [c["id"] for c in snap["channels"] if c.get("parent_id") == data["id"]]
        items.append((data, children))
    await r.restore_channels(items)

    for role, data in restored:
        await r.restore_role_overwrites(role, role_overwrites(snap, data["id"]))
        await r.add_members(role, data.get("members", []))

    cache = bot.get_cog("MessageCache")
    if cache is not None:
        await cache.replay_into(r)

    bot.config.remember_ids(guild.id, r.new_channels, r.new_roles)
    return r
