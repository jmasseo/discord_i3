from __future__ import annotations

import pytest

from i3bridge.bridge.identity import IdentityMap
from i3bridge.bridge.policy import RateLimiter
from i3bridge.bridge.ports import TellEvent
from i3bridge.bridge.tells import TellRouter
from i3bridge.i3.packets import ErrorPacket, MudInfo
from i3bridge.i3.services import BridgeOffline
from i3bridge.state.db import StateStore


class FakeOutbound:
    def __init__(self) -> None:
        self.sent: list[tuple[str, ...]] = []
        self.offline = False

    async def tell(self, user: str, visname: str, mud: str, target: str, text: str) -> None:
        if self.offline:
            raise BridgeOffline("down")
        self.sent.append(("tell", user, visname, mud, target, text))

    async def emoteto(self, user: str, visname: str, mud: str, target: str, text: str) -> None:
        self.sent.append(("emoteto", user, visname, mud, target, text))


@pytest.fixture
def setup() -> tuple[
    TellRouter, FakeOutbound, StateStore, list[tuple[int, str]], list[tuple[int, str]]
]:
    store = StateStore(":memory:")
    store.apply_mudlist({"Foo": MudInfo(-1, "", 0, 0, 0, "", "", "", "", "", "", {}, {})})
    ident = IdentityMap(store)
    out = FakeOutbound()
    dms: list[tuple[int, str]] = []
    fallback: list[tuple[int, str]] = []
    dm_ok = {1: True, 2: False}

    async def send_dm(uid: int, text: str) -> bool:
        if dm_ok.get(uid, True):
            dms.append((uid, text))
            return True
        return False

    async def send_fallback(uid: int, text: str) -> bool:
        fallback.append((uid, text))
        return True

    router = TellRouter(
        ident,
        store,
        out,
        limiter=RateLimiter(3, 60),
        send_dm=send_dm,
        send_fallback=send_fallback,  # type: ignore[arg-type]
    )
    ident.ensure(1, "Alice")
    ident.ensure(2, "Bob")
    return router, out, store, dms, fallback


async def test_inbound_dm_and_fallback(setup) -> None:  # type: ignore[no-untyped-def]
    router, _out, store, dms, fallback = setup
    assert await router.inbound(TellEvent("Foo", "zed", "Zed", "alice", "hi *there*", False))
    assert dms == [(1, "**Zed@Foo** tells you: hi \\*there\\*")]
    assert await router.inbound(TellEvent("Foo", "zed", "Zed", "bob", "waves", True))
    assert fallback == [(2, "**Zed@Foo** waves")]
    assert not await router.inbound(TellEvent("Foo", "zed", "Zed", "nobody", "x", False))
    target = store.get_tell_target(1)
    assert target is not None and (target.peer_user, target.peer_mud) == ("zed", "Foo")


async def test_dm_console_replies_and_explicit_targets(setup) -> None:  # type: ignore[no-untyped-def]
    router, out, _store, _dms, _fallback = setup
    msg = await router.from_dm(1, "Alice", "hello?")
    assert msg.startswith("Nobody has told you anything yet")
    await router.inbound(TellEvent("Foo", "zed", "Zed", "alice", "hi", False))
    assert await router.from_dm(1, "Alice", "hey Zed") == "You tell zed@Foo: hey Zed"
    assert out.sent[-1] == ("tell", "alice", "Alice", "Foo", "zed", "hey Zed")
    assert await router.from_dm(1, "Alice", ":grins") == "You emote to zed@Foo: Alice grins"
    assert out.sent[-1][0] == "emoteto"
    assert await router.from_dm(1, "Alice", "bob@foo yo") == "You tell bob@Foo: yo"
    assert out.sent[-1][3:] == ("Foo", "bob", "yo")
    assert "no mud named Nope" in await router.from_dm(1, "Alice", "bob@Nope yo")
    assert "user@mud" in await router.from_dm(1, "Alice", "/i3 tell x")
    assert await router.from_dm(1, "Alice", "   ") == ""
    # rate limit: 3 per window already used (hey, :grins, yo)
    assert "too quickly" in await router.from_dm(1, "Alice", "spam")
    out.offline = True
    assert "Not sent" in await router.from_dm(2, "Bob", "zed@Foo hi")
    assert router.format_error("Foo", ErrorPacket("unk-user", "no zed", 0)) == (
        "**Foo** reports an error: `unk-user` no zed"
    )
    with pytest.raises(ValueError):
        router.parse_target("nobody")
