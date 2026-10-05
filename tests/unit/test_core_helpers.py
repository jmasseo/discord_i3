from __future__ import annotations

from i3bridge.bridge.core import format_who, human_idle
from i3bridge.discord_.channels import ChannelManager
from i3bridge.i3.packets import ChannelInfo, WhoEntry


def test_human_idle() -> None:
    assert [human_idle(n) for n in (5, 61, 3700, 90000)] == ["5s", "1m", "1h", "1d"]


def test_format_who() -> None:
    assert format_who("Foo", []) == "**Foo**: nobody is on."
    text = format_who("Foo", [WhoEntry("Bob", 65, "%^RED%^the Brave%^RESET%^")])
    assert (
        text.startswith("**Foo**: 1 on\n```\n")
        and "Bob" in text
        and "the Brave" in text
        and "%^" not in text
    )


def test_channel_manager_pure_helpers() -> None:
    assert ChannelManager.slug("Inter Gossip!") == "inter-gossip"
    assert ChannelManager.slug("###") == "channel"
    topic = ChannelManager.topic_for("intergossip", ChannelInfo("*dalet", 0))
    assert "intergossip" in topic and "*dalet" in topic and "selectively banned" in topic
    assert "no longer" in ChannelManager.topic_for("gone", None)
