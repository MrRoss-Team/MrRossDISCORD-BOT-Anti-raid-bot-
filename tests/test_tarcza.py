from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

from tarcza.antinuke import DANGEROUS, RateTracker, perm_names
from tarcza.antiraid import Joiner, find_clusters, names_similar, skeleton
from tarcza.config import ConfigStore
from tarcza.messages import safe_webhook_name
from tarcza.restore import Restorer, resolve, undo_entries
from tarcza.snapshots import SnapshotStore
from tarcza.state import DeadChannel, DeadRole, GuildState, serialize_overwrites

A = discord.AuditLogAction


class RateTrackerTest(unittest.TestCase):
    def test_triggers_only_inside_window(self) -> None:
        t = RateTracker()
        self.assertFalse(t.hit("k", 0.0, 3, 10))
        self.assertFalse(t.hit("k", 1.0, 3, 10))
        self.assertTrue(t.hit("k", 2.0, 3, 10))

    def test_old_hits_expire(self) -> None:
        t = RateTracker()
        t.hit("k", 0.0, 3, 10)
        t.hit("k", 1.0, 3, 10)
        self.assertFalse(t.hit("k", 30.0, 3, 10))

    def test_keys_are_separate(self) -> None:
        t = RateTracker()
        t.hit(("g", 1, "ban"), 0.0, 2, 10)
        self.assertFalse(t.hit(("g", 2, "ban"), 0.5, 2, 10))
        self.assertTrue(t.hit(("g", 1, "ban"), 1.0, 2, 10))


class RaidClusterTest(unittest.TestCase):
    def test_skeleton(self) -> None:
        self.assertEqual(skeleton("Kasia_8812"), "kasia")
        self.assertEqual(skeleton("Żaba.12"), "zaba")

    def test_similar_names(self) -> None:
        self.assertTrue(names_similar("kasia_8812", "kasia_9031"))
        self.assertTrue(names_similar("xXdarkXx1", "xXdarkXx2"))
        self.assertFalse(names_similar("marek", "zuzanna"))

    def test_finds_batch_of_clones(self) -> None:
        base = 1_700_000_000.0
        clones = [Joiner(i, f"nitro_free{i}", base + i * 60, base + 9999, False) for i in range(5)]
        normal = [
            Joiner(100, "ania", base - 86400 * 400, base + 9999, False),
            Joiner(101, "tomek", base - 86400 * 30, base + 9999, True),
        ]
        groups = find_clusters(clones + normal, min_size=4)
        self.assertEqual(len(groups), 1)
        self.assertEqual({j.id for j in groups[0]}, {0, 1, 2, 3, 4})

    def test_accounts_far_apart_are_not_clones(self) -> None:
        base = 1_700_000_000.0
        users = [Joiner(i, f"gracz{i}", base + i * 86400, base, True) for i in range(6)]
        self.assertEqual(find_clusters(users, min_size=4), [])

    def test_default_avatar_needs_tight_gap(self) -> None:
        base = 1_700_000_000.0
        tight = [Joiner(i, name, base + i * 60, base, True) for i, name in enumerate(["ola", "bartek", "zenon", "iga"])]
        loose = [Joiner(i, name, base + i * 25 * 60, base, True) for i, name in enumerate(["ola", "bartek", "zenon", "iga"])]
        self.assertEqual(len(find_clusters(tight, min_size=4)), 1)
        self.assertEqual(find_clusters(loose, min_size=4), [])


class SmallHelpersTest(unittest.TestCase):
    def test_resolve_follows_chain_and_survives_loops(self) -> None:
        self.assertEqual(resolve({1: 2, 2: 3}, 1), 3)
        self.assertEqual(resolve({1: 2, 2: 1}, 1) in (1, 2), True)
        self.assertEqual(resolve({}, 5), 5)

    def test_serialize_overwrites_handles_unknown_targets(self) -> None:
        ow = discord.PermissionOverwrite(view_channel=False)
        data = serialize_overwrites(
            [(discord.Object(1, type=discord.Role), ow), (discord.Object(2, type=discord.Member), ow)]
        )
        self.assertEqual([d["type"] for d in data], ["role", "member"])
        self.assertEqual(data[0]["deny"], discord.Permissions(view_channel=True).value)

    def test_webhook_name(self) -> None:
        self.assertNotIn("discord", safe_webhook_name("Discord Fan").lower())
        self.assertNotIn("clyde", safe_webhook_name("clyde").lower())
        self.assertEqual(safe_webhook_name("   "), "Użytkownik")

    def test_perm_names(self) -> None:
        self.assertIn("Administrator", perm_names(DANGEROUS.value))

    def test_config_merge_and_id_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp))
            cfg = store.get(1)
            cfg["log_channel_id"] = 50
            cfg["raid"]["joins"] = 3
            store.remember_ids(1, {50: 60}, {7: 8})
            fresh = ConfigStore(Path(tmp)).get(1)
            self.assertEqual(fresh["raid"]["joins"], 3)
            self.assertEqual(fresh["raid"]["seconds"], 20)
            self.assertEqual(fresh["log_channel_id"], 60)
            self.assertEqual(ConfigStore(Path(tmp)).id_maps(1), ({50: 60}, {7: 8}))


class FakeGuild:
    def __init__(self) -> None:
        self.id = 1
        self.owner_id = 999
        self._next = 1000
        self.roles: dict[int, MagicMock] = {}
        self.channels: dict[int, MagicMock] = {}
        self.calls: list[tuple[str, dict]] = []
        self.me = SimpleNamespace(top_role=SimpleNamespace(position=50))
        self.bitrate_limit = 96000.0
        self.unban = AsyncMock()

    def _new_id(self) -> int:
        self._next += 1
        return self._next

    def get_role(self, rid: int):
        return self.roles.get(rid)

    def get_channel(self, cid: int):
        return self.channels.get(cid)

    def get_member(self, _mid: int):
        return None

    async def create_role(self, **kw):
        role = MagicMock(spec=discord.Role)
        role.id = self._new_id()
        role.name = kw["name"]
        role.permissions = kw["permissions"]
        self.roles[role.id] = role
        self.calls.append(("role", kw))
        return role

    async def edit_role_positions(self, positions, reason=None):
        self.calls.append(("positions", {"positions": positions}))

    def _channel(self, cls, kw):
        ch = MagicMock(spec=cls)
        ch.id = self._new_id()
        ch.name = kw["name"]
        category = kw.get("category")
        ch.category_id = category.id if category else None
        ch.set_permissions = AsyncMock()
        ch.edit = AsyncMock()
        ch.mention = f"<#{ch.id}>"
        self.channels[ch.id] = ch
        return ch

    async def create_category(self, **kw):
        self.calls.append(("category", kw))
        return self._channel(discord.CategoryChannel, kw)

    async def create_text_channel(self, **kw):
        self.calls.append(("text", kw))
        return self._channel(discord.TextChannel, kw)

    async def create_voice_channel(self, **kw):
        self.calls.append(("voice", kw))
        return self._channel(discord.VoiceChannel, kw)


def run(coro):
    return asyncio.run(coro)


class RestorerTest(unittest.TestCase):
    def test_category_first_then_children_with_new_role_ids(self) -> None:
        guild = FakeGuild()
        survivor = MagicMock(spec=discord.TextChannel)
        survivor.id, survivor.name, survivor.category_id = 3, "przetrwal", None
        survivor.edit = AsyncMock()
        guild.channels[3] = survivor

        r = Restorer(guild, "test")
        mod = run(r.create_role({"id": 10, "name": "Moderator", "permissions": 8, "color": 0, "position": 5}))
        allow = discord.Permissions(send_messages=True).value
        channels = [
            ({"id": 2, "name": "ogolny", "type": "text", "position": 1, "parent_id": 1,
              "overwrites": [{"id": 10, "type": "role", "allow": allow, "deny": 0}]}, []),
            ({"id": 1, "name": "Tekstowe", "type": "category", "position": 0, "parent_id": None, "overwrites": []}, [2, 3]),
        ]
        run(r.restore_channels(channels))

        kinds = [k for k, _ in guild.calls if k in ("category", "text")]
        self.assertEqual(kinds, ["category", "text"], "kategoria musi powstać przed kanałem")
        text_kw = next(kw for k, kw in guild.calls if k == "text")
        new_cat = r.channel(1)
        self.assertIs(text_kw["category"], new_cat)
        self.assertIn(mod, text_kw["overwrites"], "uprawnienia muszą wskazywać na NOWĄ rolę")
        self.assertTrue(text_kw["overwrites"][mod].send_messages)
        survivor.edit.assert_awaited_once()
        self.assertIs(survivor.edit.call_args.kwargs["category"], new_cat)
        self.assertEqual(set(r.new_channels), {1, 2})

    def test_skips_things_that_already_exist(self) -> None:
        guild = FakeGuild()
        existing = MagicMock(spec=discord.TextChannel)
        existing.id = 2000
        guild.channels[2000] = existing
        r = Restorer(guild, "test", channel_map={5: 2000})
        self.assertIsNone(run(r.create_channel({"id": 5, "name": "x", "type": "text"})))
        self.assertEqual(guild.calls, [])


class UndoEntriesTest(unittest.TestCase):
    def test_full_nuke_is_rolled_back(self) -> None:
        guild = FakeGuild()
        spam = MagicMock(spec=discord.TextChannel)
        spam.id, spam.name = 77, "hacked"
        spam.delete = AsyncMock()
        guild.channels[77] = spam

        state = GuildState()
        state.dead_roles[10] = DeadRole(
            {"id": 10, "name": "Admin", "permissions": 8, "color": 0, "position": 3, "managed": False, "default": False},
            members=[],
            overwrites={},
            deleted_at=1e12,
        )
        state.dead_channels[20] = DeadChannel(
            {"id": 20, "name": "zasady", "type": "text", "position": 0, "parent_id": None,
             "overwrites": [{"id": 10, "type": "role", "allow": 0, "deny": 0}]},
            children=[],
            deleted_at=1e12,
        )

        def entry(eid, action, target):
            return SimpleNamespace(id=eid, action=action, target=SimpleNamespace(id=target),
                                   before=SimpleNamespace(), after=SimpleNamespace(), extra=None)

        entries = [
            entry(4, A.ban, 555),
            entry(2, A.channel_delete, 20),
            entry(1, A.role_delete, 10),
            entry(3, A.channel_create, 77),
            entry(5, A.kick, 666),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config = ConfigStore(Path(tmp))
            bot = SimpleNamespace(
                config=config,
                snapshots=SnapshotStore(Path(tmp)),
                get_cog=lambda name: SimpleNamespace(get=lambda g: state) if name == "StateTracker" else None,
            )
            r = run(undo_entries(bot, guild, entries, reason="test"))
            channel_map, role_map = config.id_maps(1)

        self.assertIn(10, role_map)
        self.assertIn(20, channel_map)
        text_kw = next(kw for k, kw in guild.calls if k == "text")
        self.assertIn(guild.roles[role_map[10]], text_kw["overwrites"])
        spam.delete.assert_awaited_once()
        guild.unban.assert_awaited_once()
        self.assertTrue(any("wyrzucenia nie da się cofnąć" in f for f in r.failed))
        self.assertNotIn(10, state.dead_roles)
        self.assertNotIn(20, state.dead_channels)


class LevelsTest(unittest.TestCase):
    def test_add_xp_carries_leftover_and_multiple_levels(self) -> None:
        from tarcza.levels import add_xp

        self.assertEqual(add_xp(63, 240, 10, 250), (64, 0, 1))
        self.assertEqual(add_xp(0, 249, 2, 250), (1, 1, 1))
        self.assertEqual(add_xp(0, 0, 600, 250), (2, 100, 2))
        self.assertEqual(add_xp(5, 10, 5, 250), (5, 15, 0))

    def test_store_and_ranking(self) -> None:
        from tarcza.levels import LevelStore

        with tempfile.TemporaryDirectory() as tmp:
            store = LevelStore(Path(tmp) / "levels.db")
            store.set(1, 10, 5, 100, 1350)
            store.set(1, 11, 7, 0, 1750)
            store.set(1, 12, 5, 200, 1450)
            store.set(2, 10, 99, 0, 0)
            self.assertEqual([uid for uid, *_ in store.top(1)], [11, 12, 10])
            self.assertEqual(store.rank(1, 10), 3)
            self.assertEqual(store.get(1, 99), (0, 0, 0))
            store.close()

    def test_messages_give_xp_and_announce_level_up(self) -> None:
        from tarcza.levels import Levels, LevelStore

        with tempfile.TemporaryDirectory() as tmp:
            config = ConfigStore(Path(tmp))
            config.get(1)["levels"].update(xp_min=20, xp_max=20, cooldown=0, xp_per_level=250)
            store = LevelStore(Path(tmp) / "levels.db")
            cog = Levels(SimpleNamespace(config=config), store)
            cog.announce = AsyncMock()

            author = MagicMock(spec=discord.Member)
            author.id, author.bot = 10, False
            message = SimpleNamespace(
                guild=SimpleNamespace(id=1), author=author, webhook_id=None,
                type=discord.MessageType.default, channel="kanal",
            )
            for _ in range(13):
                run(cog.on_message(message))

            self.assertEqual(store.get(1, 10), (1, 10, 260))
            cog.announce.assert_awaited_once_with(author, "kanal", 1, 10)

            author.bot = True
            run(cog.on_message(message))
            self.assertEqual(store.get(1, 10)[2], 260, "boty nie dostają expa")
            store.close()

    def test_card_renders_png(self) -> None:
        from io import BytesIO

        from PIL import Image

        from tarcza.cards import BASE_H, BASE_W, OUT_SCALE, render_card

        png = render_card(
            "Awans poziomu",
            [("Użytkownik", "bardzo_dluga_nazwa_uzytkownika_ktora_sie_nie_miesci_123"), ("Nowy poziom", "245"), ("Exp", "1 / 250")],
            avatar=b"to nie jest obrazek",
            footer="Statystyki • 2026-11-06 23:06",
        )
        img = Image.open(BytesIO(png))
        self.assertEqual(img.size, (BASE_W * OUT_SCALE, BASE_H * OUT_SCALE))
        self.assertEqual(img.mode, "RGBA")


class BotWiringTest(unittest.TestCase):
    def test_all_commands_register(self) -> None:
        from bot import TarczaBot

        async def build(data_dir: Path) -> list[str]:
            bot = TarczaBot(read_messages=True, data_dir=data_dir)
            await bot.load_cogs()
            names = sorted(c.qualified_name for c in bot.tree.walk_commands())
            for name in list(bot.cogs):
                await bot.remove_cog(name)
            return names

        with tempfile.TemporaryDirectory() as tmp:
            names = run(build(Path(tmp)))
        for expected in (
            "tarcza status", "tarcza zaufani", "kopia przywroc", "rajd start", "cofnij",
            "poziom", "ranking", "poziomy ustaw", "poziomy kanal", "poziomy test",
        ):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
