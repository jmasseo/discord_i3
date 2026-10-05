from __future__ import annotations

import pytest

from i3bridge.i3 import packets as pk
from i3bridge.i3.lpc import decode, encode


def test_packet_round_trip() -> None:
    p = pk.Packet("tell", "Discord", "alice", "OtherMud", "bob", ["Alice", "hi"])
    assert p.to_lpc() == ["tell", 5, "Discord", "alice", "OtherMud", "bob", "Alice", "hi"]
    assert pk.Packet.from_lpc(decode(encode(p.to_lpc()))) == p


def test_from_lpc_errors() -> None:
    with pytest.raises(pk.PacketError):
        pk.Packet.from_lpc(["too", "short"])
    with pytest.raises(pk.PacketError):
        pk.Packet.from_lpc([0, 5, "a", 0, 0, 0])
    with pytest.raises(pk.PacketError):
        pk.Packet.from_lpc("not an array")


def test_startup_req_field_order() -> None:
    p = pk.build_startup_req(
        mud="Discord", router="*dalet", password=0, old_mudlist_id=0, old_chanlist_id=0,
        player_port=0, tcp_port=0, udp_port=0, mudlib="lib", base_mudlib="base", driver="drv",
        mud_type="Discord", open_status="open for public", admin_email="a@b.c",
        services={"tell": 1},
    )  # fmt: skip
    assert p.to_lpc() == [
        "startup-req-3", 5, "Discord", 0, "*dalet", 0,
        0, 0, 0, 0, 0, 0, "lib", "base", "drv", "Discord", "open for public", "a@b.c",
        {"tell": 1}, 0,
    ]  # fmt: skip


def test_parse_startup_reply() -> None:
    raw = decode(
        '({"startup-reply",5,"*dalet",0,"Discord",0,'
        '({({"*dalet","97.107.133.86 8787",}),({"*i4","204.209.44.3 8080",}),}),123456,})'
    )
    reply = pk.parse_startup_reply(pk.Packet.from_lpc(raw))
    assert reply.password == 123456
    assert reply.router_list == [
        pk.RouterEntry("*dalet", "97.107.133.86", 8787),
        pk.RouterEntry("*i4", "204.209.44.3", 8080),
    ]


def test_parse_mudlist_with_deletion_and_short_info() -> None:
    raw = decode(
        '({"mudlist",5,"*dalet",0,"Discord",0,77,(["Dead":0,'
        '"Alive":({-1,"1.2.3.4",4000,0,0,"Lib","Base","FluffOS","LP","open","x@y.z",'
        '(["tell":1,"channel":1,]),([]),}),"Odd":({0,"5.6.7.8",}),]),})'
    )
    mudlist_id, entries = pk.parse_mudlist(pk.Packet.from_lpc(raw))
    assert mudlist_id == 77
    assert entries["Dead"] is None
    alive = entries["Alive"]
    assert alive is not None and alive.up and alive.services == {"tell": 1, "channel": 1}
    odd = entries["Odd"]
    assert odd is not None and not odd.up and odd.ip == "5.6.7.8" and odd.mudlib == ""


def test_mudlist_tolerates_odd_service_values_and_bad_entries() -> None:
    raw = decode(
        '({"mudlist",5,"*dalet",0,"Discord",0,78,(['
        '"Stringy":({-1,"1.2.3.4",4000,0,0,"Lib","Base","Drv","LP","open","x@y.z",'
        '(["tell":"1","ftp":"21","http":"yes","news":"","auth":0,]),0,}),'
        '"Broken":({-1,"1.2.3.4",4000,0,0,"Lib","Base","Drv","LP","open","x@y.z",'
        '"not a mapping",0,}),'
        '"Fine":({-1,"5.6.7.8",23,0,0,"Lib","Base","Drv","LP","open","x@y.z",(["tell":1,]),0,}),'
        '"Gone":0,]),})'
    )
    mudlist_id, entries = pk.parse_mudlist(pk.Packet.from_lpc(raw))
    assert mudlist_id == 78
    stringy = entries["Stringy"]
    assert stringy is not None
    assert stringy.services == {"tell": 1, "ftp": 21, "http": 1, "news": 0, "auth": 0}
    assert "Broken" not in entries, "a bad entry is skipped, not fatal"
    assert entries["Fine"] is not None and entries["Gone"] is None


def test_chanlist_skips_bad_entries() -> None:
    raw = decode(
        '({"chanlist-reply",5,"*dalet",0,"Discord",0,9,'
        '(["ok":({"*dalet",0,}),"bad":({"only-one",}),"worse":"string",]),})'
    )
    _, entries = pk.parse_chanlist(pk.Packet.from_lpc(raw))
    assert entries == {"ok": pk.ChannelInfo("*dalet", 0)}


def test_parse_chanlist() -> None:
    raw = decode(
        '({"chanlist-reply",5,"*dalet",0,"Discord",0,9,'
        '(["intergossip":({"*dalet",0,}),"gone":0,"dchat":({"Dead Souls",1,}),]),})'
    )
    chanlist_id, entries = pk.parse_chanlist(pk.Packet.from_lpc(raw))
    assert chanlist_id == 9
    assert entries["intergossip"] == pk.ChannelInfo("*dalet", 0)
    assert entries["gone"] is None
    assert entries["dchat"] == pk.ChannelInfo("Dead Souls", 1)


def test_channel_packets() -> None:
    m = pk.Packet.from_lpc(
        decode('({"channel-m",5,"Foo",  "bob",0,0,"intergossip","Bob","hello",})')
    )
    assert pk.parse_channel_message(m) == pk.ChannelMessage("intergossip", "Bob", "hello")
    t = pk.Packet.from_lpc(
        decode(
            '({"channel-t",5,"Foo","bob",0,0,"ig","Discord","alice",'
            '"$N pokes $O","$N pokes you","Bob","Alice",})'
        )
    )
    parsed = pk.parse_channel_t(t)
    assert parsed.target_user == "alice" and parsed.message_target == "$N pokes you"
    with pytest.raises(pk.PacketError):
        pk.parse_channel_message(
            pk.Packet.from_lpc(decode('({"channel-m",5,"Foo","bob",0,0,"ig",})'))
        )


def test_who_and_error_and_locate_parsing() -> None:
    who = pk.Packet.from_lpc(
        decode(
            '({"who-reply",5,"Foo",0,"Discord","alice",'
            '({({"Bob",12,"the Brave",}),({"Eve",0,0,}),}),})'
        )
    )
    assert pk.parse_who_reply(who) == [
        pk.WhoEntry("Bob", 12, "the Brave"),
        pk.WhoEntry("Eve", 0, ""),
    ]
    err = pk.Packet.from_lpc(
        decode('({"error",5,"*dalet",0,"Discord","alice","unk-user","no such user",0,})')
    )
    assert pk.parse_error(err).code == "unk-user"
    loc = pk.Packet.from_lpc(
        decode('({"locate-reply",5,"Foo",0,"Discord","alice","Foo","Bob",5,0,})')
    )
    assert pk.parse_locate_reply(loc) == pk.LocateReply("Foo", "Bob", 5, "")


def test_builders_match_spec_shapes() -> None:
    assert pk.build_channel_listen("Discord", "*dalet", "ig", True).to_lpc() == [
        "channel-listen", 5, "Discord", 0, "*dalet", 0, "ig", 1,
    ]  # fmt: skip
    assert pk.build_channel_m("Discord", "alice", "ig", "Alice", "hi").to_lpc() == [
        "channel-m", 5, "Discord", "alice", 0, 0, "ig", "Alice", "hi",
    ]  # fmt: skip
    who = pk.build_who_reply("Discord", "Foo", "bob", [pk.WhoEntry("Alice", 3, "[online]")])
    assert who.to_lpc() == [
        "who-reply", 5, "Discord", 0, "Foo", "bob", [["Alice", 3, "[online]"]],
    ]  # fmt: skip
    assert (
        pk.build_locate_reply("Discord", "Foo", "bob", "Discord", "Alice", 0, None).to_lpc()[-1]
        == 0
    )
    assert pk.build_error("Discord", "Foo", "bob", "unk-user", "nope", ["x"]).to_lpc() == [
        "error", 5, "Discord", 0, "Foo", "bob", "unk-user", "nope", ["x"],
    ]  # fmt: skip
    assert pk.build_shutdown("Discord", "*dalet", 30).to_lpc() == [
        "shutdown",
        5,
        "Discord",
        0,
        "*dalet",
        0,
        30,
    ]
    assert pk.build_ucache_update("Discord", "alice", "Alice", 2).to_lpc() == [
        "ucache-update", 5, "Discord", 0, 0, 0, "alice", "Alice", 2,
    ]  # fmt: skip
