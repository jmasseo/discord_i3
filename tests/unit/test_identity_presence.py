from __future__ import annotations

import pytest

from i3bridge.bridge.identity import IdentityError, IdentityMap
from i3bridge.bridge.presence import MemberSnapshot, PresenceTable
from i3bridge.config import PresenceConfig
from i3bridge.state.db import StateStore


def test_identity_derivation_and_collisions() -> None:
    store = StateStore(":memory:")
    ident = IdentityMap(store)
    assert IdentityMap.derive_name("Zoë the Great!") == "zoethegreat"
    assert IdentityMap.derive_name("42") == "user"
    assert IdentityMap.derive_name("日本") == "riben"
    a = ident.ensure(1, "Alice")
    b = ident.ensure(2, "alice")
    assert (a.i3_name, b.i3_name) == ("alice", "alice2")
    assert ident.ensure(3, "Discord").i3_name == "discord2"
    assert ident.ensure(1, "Alice Renamed").visname == "Alice Renamed"
    assert ident.ensure(1, "x").i3_name == "alice"  # name sticks across display changes
    assert ident.by_i3_name("ALICE2") is not None and ident.by_i3_name("ALICE2").discord_id == 2  # type: ignore[union-attr]


def test_identity_settings() -> None:
    store = StateStore(":memory:")
    ident = IdentityMap(store)
    ident.ensure(1, "Alice")
    assert ident.set_nick(1, "Alice", "Ali").i3_name == "ali"
    ident.ensure(2, "Bob")
    with pytest.raises(IdentityError):
        ident.set_nick(2, "Bob", "ali")
    with pytest.raises(IdentityError):
        ident.set_nick(2, "Bob", "1bad")
    with pytest.raises(IdentityError):
        ident.set_nick(2, "Bob", "console")
    with pytest.raises(IdentityError):
        ident.set_gender(2, "Bob", "other")
    assert ident.set_gender(2, "Bob", "female").gender == 1
    assert ident.set_hidden(2, "Bob", True).hidden is True
    assert ident.set_dm(2, "Bob", False).dm_tells is False


def make_table(
    scope: str = "all", clock: list[float] | None = None
) -> tuple[PresenceTable, IdentityMap, list[float]]:
    store = StateStore(":memory:")
    ident = IdentityMap(store)
    t = clock or [1000.0]
    views = {1: {"ig"}, 2: set(), 3: {"ig"}}
    table = PresenceTable(
        ident,
        PresenceConfig(scope=scope, fallback_window_seconds=100),  # type: ignore[arg-type]
        can_view=lambda mid, ch: ch in views.get(mid, set()),
        bridged_channels=lambda: {"ig"},
        clock=lambda: t[0],
    )
    return table, ident, t


def test_presence_who_reflects_real_status() -> None:
    table, ident, t = make_table()
    table.update(MemberSnapshot(1, "Alice", False, "online", "Playing Nethack", "Admins"))
    table.update(MemberSnapshot(2, "Bob", False, "dnd", "busy", None))
    table.update(MemberSnapshot(3, "Carol", False, "offline"))
    table.update(MemberSnapshot(4, "Botty", True, "online"))
    table.update(MemberSnapshot(5, "Dave", False, "idle"))
    t[0] = 1030.0
    table.saw_message(1)
    t[0] = 1040.0
    who = table.who_list()
    assert [(w.visname, w.idle, w.extra) for w in who] == [
        ("Alice", 10, "[online] Playing Nethack"),
        ("Bob", 40, "[dnd] busy"),
        ("Dave", 40, "[idle]"),
    ]
    # dnd members are listed with their real status, idle maps to inactive for locate
    assert table.locate("dave") is not None and table.locate("dave").status == "inactive"  # type: ignore[union-attr]
    assert table.locate("bob") is not None and table.locate("bob").status is None  # type: ignore[union-attr]
    assert table.locate("carol") is None
    finger = table.finger("alice")
    assert (
        finger is not None
        and finger.title == "Admins"
        and finger.email == ""
        and finger.ip_name == ""
    )
    assert finger.loginout_time.endswith("1970") and finger.idle == 10
    # hidden users vanish from every query
    ident.set_hidden(2, "Bob", True)
    assert [w.visname for w in table.who_list()] == ["Alice", "Dave"]
    assert table.finger("bob") is None
    # status transitions update idle and login time
    table.update(MemberSnapshot(3, "Carol", False, "online"))
    t[0] = 1045.0
    carol = next(w for w in table.who_list() if w.visname == "Carol")
    assert carol.idle == 5
    assert table.chan_who("ig") == ["Alice", "Carol"]
    assert table.chan_who("nope") is None
    assert table.chan_user("alice") == ("Alice", 2)
    table.remove(1)
    assert table.count_online() == 2


def test_presence_scopes_and_fallback() -> None:
    table, _, t = make_table(scope="bridged")
    table.update(MemberSnapshot(1, "Alice", False, "online"))
    table.update(MemberSnapshot(2, "Bob", False, "online"))
    assert [w.visname for w in table.who_list()] == ["Alice"]
    table, ident, t = make_table(scope="opt-in")
    table.update(MemberSnapshot(1, "Alice", False, "online"))
    table.update(MemberSnapshot(2, "Bob", False, "online"))
    ident.ensure(2, "Bob")
    assert [w.visname for w in table.who_list()] == ["Bob"]
    table, _, t = make_table()
    table.update(MemberSnapshot(1, "Alice", False, "unknown"))
    assert table.who_list() == []
    table.saw_message(1)
    assert [w.extra for w in table.who_list()] == ["[online]"]
    t[0] += 200
    assert table.who_list() == []
