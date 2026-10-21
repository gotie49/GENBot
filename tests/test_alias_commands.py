"""/player add|edit|status with aliases, driven through the real command handlers."""

from types import SimpleNamespace

import pytest

from statustw_bot.commands import PlayerCommands
from statustw_bot.config import Settings
from statustw_bot.notifier import LinkTemplates
from statustw_bot.storage import Storage
from test_monitor import GER3, FakeApi

ADMIN_ID = 42


class FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=ADMIN_ID)
        self.command = SimpleNamespace(name="test")
        self.messages: list[str] = []
        outer = self

        class Response:
            done = False

            async def send_message(self, content=None, **kw):
                outer.messages.append(content)

            async def defer(self, **kw):
                pass

        class Followup:
            async def send(self, content=None, **kw):
                outer.messages.append(content if content is not None else kw.get("embed"))

        self.response = Response()
        self.followup = Followup()

    @property
    def last(self):
        return self.messages[-1]


@pytest.fixture
async def setup(tmp_path):
    storage = Storage(str(tmp_path / "bot.sqlite3"))
    await storage.open()
    api = FakeApi()
    forgotten = []
    bot = SimpleNamespace(
        settings=Settings.from_env({"DISCORD_BOT_TOKEN": "x" * 30, "DISCORD_CHANNEL_ID": "1",
                                    "ADMIN_USER_IDS": str(ADMIN_ID)}),
        storage=storage,
        api=api,
        monitor=SimpleNamespace(forget=forgotten.append),
        refresh_board=lambda: None,
        links=LinkTemplates(player_profile="https://status.tw/players/{name}"),
    )
    yield PlayerCommands(bot), storage, api, forgotten
    await storage.close()


async def run(group, command, **kwargs):
    interaction = FakeInteraction()
    await group.get_command(command).callback(group, interaction, **kwargs)
    return interaction


async def test_add_with_aliases(setup):
    group, storage, api, _ = setup
    api.online["G0tie"] = [GER3]
    i = await run(group, "add", player="Gotie", aliases="G0tie, gotie49")
    assert "Now watching **Gotie**" in i.last and "Aliases: G0tie, gotie49" in i.last
    assert "online" in i.last and "as G0tie" in i.last
    assert (await storage.get_player("Gotie")).aliases == ("G0tie", "gotie49")


async def test_add_rejects_unknown_alias_and_stores_nothing(setup):
    group, storage, api, _ = setup
    api.unknown.add("typo")
    i = await run(group, "add", player="Gotie", aliases="G0tie, typo")
    assert "never seen typo" in i.last
    assert await storage.get_player("Gotie") is None


async def test_add_rejects_names_of_other_people(setup):
    group, storage, _, _ = setup
    await run(group, "add", player="KOF", aliases="k0f")
    i = await run(group, "add", player="Gotie", aliases="k0f")
    assert "already belongs to **KOF**" in i.last
    i = await run(group, "add", player="k0f")
    assert "already belongs to **KOF**" in i.last
    assert await storage.get_player("Gotie") is None and await storage.get_player("k0f") is None


async def test_edit_replaces_and_clears_aliases(setup):
    group, storage, _, forgotten = setup
    await run(group, "add", player="Gotie", aliases="G0tie")
    i = await run(group, "edit", player="Gotie", aliases="gotie49, xGotie")
    assert "Aliases: gotie49, xGotie" in i.last
    assert (await storage.get_player("Gotie")).aliases == ("gotie49", "xGotie")
    rec_id = (await storage.get_player("Gotie")).id
    assert forgotten.count(rec_id) == 2  # add + edit: next result is a fresh baseline

    i = await run(group, "edit", player="xGotie", aliases="-")  # found via alias
    assert "No aliases." in i.last
    assert (await storage.get_player("Gotie")).aliases == ()


async def test_edit_rejects_invalid_input(setup):
    group, storage, api, _ = setup
    await run(group, "add", player="Gotie")
    await run(group, "add", player="KOF")
    assert "main name" in (await run(group, "edit", player="Gotie", aliases="Gotie")).last
    assert "already belongs to **KOF**" in (await run(group, "edit", player="Gotie", aliases="KOF")).last
    api.unknown.add("typo")
    assert "never seen typo" in (await run(group, "edit", player="Gotie", aliases="typo")).last
    assert "not registered" in (await run(group, "edit", player="nobody", aliases="x")).last
    assert (await storage.get_player("Gotie")).aliases == ()


async def test_status_looks_up_all_names(setup):
    group, _, api, _ = setup
    await run(group, "add", player="Gotie", aliases="G0tie")
    api.online["G0tie"] = [GER3]
    i = await run(group, "status", player="Gotie")
    embed = i.last
    assert "online" in embed.description and "as G0tie" in embed.description
    assert any(f.name == "Aliases" and f.value == "G0tie" for f in embed.fields)
