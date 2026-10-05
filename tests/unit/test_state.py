from __future__ import annotations

from i3bridge.i3.packets import ChannelInfo, MudInfo
from i3bridge.state.db import ChannelLink, StateStore, UserRecord


def info(state: int = -1) -> MudInfo:
    return MudInfo(
        state, "1.2.3.4", 4000, 0, 0, "lib", "base", "drv", "LP", "open", "a@b", {"tell": 1}, {}
    )


def test_router_state() -> None:
    s = StateStore(":memory:")
    assert s.get_password("Discord", "*a") is None
    s.set_password("Discord", "*a", 7)
    assert s.get_password("Discord", "*a") == 7
    assert s.any_password("Discord") == 7
    assert s.get_list_ids("Discord", "*a") == (0, 0)
    s.set_list_ids("Discord", "*a", mudlist_id=3)
    s.set_list_ids("Discord", "*b", chanlist_id=4)
    assert s.get_list_ids("Discord", "*a") == (3, 0)
    assert s.get_list_ids("Discord", "*b") == (0, 4)
    assert s.get_password("Discord", "*b") is None


def test_lists_apply_and_replace() -> None:
    s = StateStore(":memory:")
    s.apply_mudlist({"A": info(), "B": info(0)})
    assert set(s.get_mudlist()) == {"A", "B"}
    s.apply_mudlist({"B": None, "C": info()})
    assert set(s.get_mudlist()) == {"A", "C"}
    s.apply_mudlist({"Z": info()}, replace=True)
    assert set(s.get_mudlist()) == {"Z"}
    assert s.find_mud("z") == ("Z", info())
    s.apply_chanlist({"ig": ChannelInfo("*a", 0), "x": ChannelInfo("M", 2)})
    s.apply_chanlist({"x": None})
    assert s.get_chanlist() == {"ig": ChannelInfo("*a", 0)}
    assert s.get_channel("ig") == ChannelInfo("*a", 0)


def test_links_users_targets_ignores_settings() -> None:
    s = StateStore(":memory:")
    s.add_link(ChannelLink("ig", 100, 5, "tok", "created"))
    assert (
        s.link_for_i3("ig")
        == s.link_for_discord(100)
        == ChannelLink("ig", 100, 5, "tok", "created")
    )
    s.set_link_webhook("ig", None, None)
    assert s.link_for_i3("ig") == ChannelLink("ig", 100, None, None, "created")
    s.remove_link("ig")
    assert s.get_links() == []
    s.add_link(ChannelLink("a", 1, None, None, "created"))
    s.add_link(ChannelLink("b", 2, None, None, "linked"))
    assert s.clear_links() == 2 and s.get_links() == []

    s.upsert_user(UserRecord(1, "alice", "Alice"))
    assert s.get_user(1) == UserRecord(1, "alice", "Alice")
    assert s.get_user_by_i3_name("ALICE") is not None
    assert s.i3_name_taken("alice") and not s.i3_name_taken("alice", except_discord_id=1)
    s.touch_user(1)
    user = s.get_user(1)
    assert user is not None and user.last_seen > 0

    s.set_tell_target(1, "bob", "Foo")
    target = s.get_tell_target(1)
    assert target is not None and (target.peer_user, target.peer_mud) == ("bob", "Foo")

    s.add_ignore("Spammer@BadMud")
    assert s.list_ignores() == ["spammer@badmud"]
    assert s.remove_ignore("spammer@badmud") and not s.remove_ignore("nope")

    assert s.get_setting("k") is None
    s.set_setting("k", "v")
    assert s.get_setting("k") == "v"
    s.delete_setting("k")
    assert s.get_setting("k") is None
