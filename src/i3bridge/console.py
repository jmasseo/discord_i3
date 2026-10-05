"""A console frontend: chat on I3 from a terminal, without Discord. Used by ``i3bridge repl``."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

from .bridge.ports import ChannelEvent, LocateInfo, TellEvent
from .config import Config
from .i3 import packets as pk
from .i3.client import I3Client
from .i3.services import BridgeOffline, Dispatcher, Outbound
from .state.db import StateStore

HELP = """commands:
  /listen <chan>        /unlisten <chan>      /say <chan> <text>     /emote <chan> <text>
  /tell <user@mud> <t>  /emoteto <user@mud> <t>
  /who <mud>            /finger <user@mud>    /locate <user>         /chanwho <mud> <chan>
  /mudlist [filter]     /chanlist [filter]    /status                /quit
plain text is sent to the last channel you used with /say."""


class ConsoleFrontend:
    """Frontend + PresenceProvider for a single console user."""

    def __init__(self, username: str = "console", visname: str = "Console") -> None:
        self.username = username
        self.visname = visname
        self.started = time.time()
        self.last_input = time.monotonic()
        self.channels: set[str] = set()

    # Frontend -------------------------------------------------------------------
    async def on_channel(self, e: ChannelEvent) -> None:
        if e.kind == "message":
            print(f"[{e.channel}] {e.visname}@{e.mud}: {e.text}")
        elif e.kind == "emote":
            print(f"[{e.channel}] {e.text.replace('$N', f'{e.visname}@{e.mud}')}")
        else:
            print(f"[{e.channel}] {e.text}")

    async def on_tell(self, e: TellEvent) -> bool:
        if e.to_user.lower() != self.username:
            return False
        verb = "" if e.emote else " tells you:"
        print(f"** {e.from_visname}@{e.from_mud}{verb} {e.text}")
        return True

    async def on_who_reply(self, to_user: str, from_mud: str, entries: list[pk.WhoEntry]) -> None:
        print(f"-- who @ {from_mud}: {len(entries)} users")
        for e in entries:
            print(f"   {e.visname:<20} idle {e.idle:>6}s  {e.extra}")

    async def on_finger_reply(self, to_user: str, from_mud: str, r: pk.FingerReply) -> None:
        print(f"-- finger @ {from_mud}: {r.visname} {r.title}")
        print(f"   login {r.loginout_time} idle {r.idle}s {r.extra}")

    async def on_locate_reply(self, to_user: str, from_mud: str, r: pk.LocateReply) -> None:
        print(f"-- locate: {r.located_visname} is on {r.located_mud} (idle {r.idle}s) {r.status}")

    async def on_chan_who_reply(self, to_user: str, from_mud: str, r: pk.ChanWhoReply) -> None:
        print(f"-- {r.channel} @ {from_mud}: {', '.join(r.users) or 'nobody'}")

    async def on_error(self, to_user: str, from_mud: str, err: pk.ErrorPacket) -> None:
        print(f"!! error from {from_mud}: {err.code}: {err.message}")

    # PresenceProvider --------------------------------------------------------------
    def _idle(self) -> int:
        return int(time.monotonic() - self.last_input)

    def who_list(self) -> list[pk.WhoEntry]:
        return [pk.WhoEntry(self.visname, self._idle(), "[online] console")]

    def finger(self, username: str) -> pk.FingerReply | None:
        if username.lower() != self.username:
            return None
        return pk.FingerReply(
            self.visname, "the console", "", "", time.strftime("%c", time.localtime(self.started)),
            self._idle(), "", "", "i3bridge console session",
        )  # fmt: skip

    def locate(self, username: str) -> LocateInfo | None:
        if username.lower() != self.username:
            return None
        return LocateInfo(self.visname, self._idle(), None)

    def chan_who(self, channel: str) -> list[str] | None:
        return [self.visname] if channel in self.channels else None

    def chan_user(self, username: str) -> tuple[str, int] | None:
        return (self.visname, 2) if username.lower() == self.username else None


async def repl(cfg: Config, store: StateStore) -> None:
    frontend = ConsoleFrontend()
    dispatcher = Dispatcher(frontend, frontend, ignore_patterns=store.list_ignores)
    client = I3Client(cfg, store, dispatcher)
    out = Outbound(client)

    async def announce() -> None:
        print(f"== connected to {client.router_name} as {client.mud_name}")

    client.on_ready.append(announce)
    task = asyncio.create_task(client.run())
    print(HELP)
    loop = asyncio.get_running_loop()
    last_channel: str | None = None

    def split_target(spec: str) -> tuple[str, str]:
        user, _, mud = spec.partition("@")
        if not user or not mud:
            raise ValueError("use user@mud")
        found = store.find_mud(mud)
        return user.lower(), found[0] if found else mud

    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            frontend.last_input = time.monotonic()
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                if not line.startswith("/"):
                    if last_channel is None:
                        print("no channel yet; use /say <chan> <text>")
                        continue
                    await out.channel_message(
                        last_channel, frontend.username, frontend.visname, line
                    )
                    continue
                cmd, _, rest = line[1:].partition(" ")
                cmd = cmd.lower()
                if cmd == "quit":
                    break
                elif cmd == "help":
                    print(HELP)
                elif cmd in ("listen", "unlisten"):
                    chan = rest.strip()
                    await out.listen(chan, cmd == "listen")
                    (frontend.channels.add if cmd == "listen" else frontend.channels.discard)(chan)
                    print(f"== {cmd} {chan}")
                elif cmd in ("say", "emote"):
                    chan, _, text = rest.partition(" ")
                    last_channel = chan
                    if cmd == "say":
                        await out.channel_message(chan, frontend.username, frontend.visname, text)
                    else:
                        await out.channel_emote(chan, frontend.username, frontend.visname, text)
                elif cmd in ("tell", "emoteto"):
                    spec, _, text = rest.partition(" ")
                    user, mud = split_target(spec)
                    fn = out.tell if cmd == "tell" else out.emoteto
                    await fn(frontend.username, frontend.visname, mud, user, text)
                elif cmd == "who":
                    found = store.find_mud(rest.strip())
                    await out.who(frontend.username, found[0] if found else rest.strip())
                elif cmd == "finger":
                    user, mud = split_target(rest.strip())
                    await out.finger(frontend.username, mud, user)
                elif cmd == "locate":
                    await out.locate(frontend.username, rest.strip().lower())
                elif cmd == "chanwho":
                    mud, _, chan = rest.partition(" ")
                    found = store.find_mud(mud)
                    await out.chan_who(frontend.username, found[0] if found else mud, chan.strip())
                elif cmd == "mudlist":
                    needle = rest.strip().lower()
                    muds = store.get_mudlist()
                    for mname, minfo in muds.items():
                        if needle and needle not in mname.lower():
                            continue
                        print(
                            f"  {'UP  ' if minfo.up else 'down'} {mname:<24} "
                            f"{minfo.driver:<14} {minfo.mud_type:<8} {minfo.mudlib}"
                        )
                    print(f"== {sum(1 for m in muds.values() if m.up)} up / {len(muds)} total")
                elif cmd == "chanlist":
                    needle = rest.strip().lower()
                    chans = store.get_chanlist()
                    for cname, cinfo in chans.items():
                        if needle and needle not in cname.lower():
                            continue
                        flag = "*" if cname in client.stats.listened_channels else " "
                        print(f" {flag} {cname:<24} host {cinfo.host_mud:<20} type {cinfo.type}")
                    print(f"== {len(chans)} channels")
                elif cmd == "status":
                    s = client.stats
                    print(
                        f"== state {s.state.value} router {s.router} "
                        f"in {s.packets_in} out {s.packets_out} reconnects {s.reconnects}"
                    )
                    print(f"== listening {sorted(s.listened_channels)} last error {s.last_error}")
                else:
                    print("unknown command; /help")
            except BridgeOffline as exc:
                print(f"!! {exc}")
            except ValueError as exc:
                print(f"!! {exc}")
    finally:
        client.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)
