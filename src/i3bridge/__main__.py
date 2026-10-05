"""Command-line entry point: ``i3bridge run|probe|repl|dump-state``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import sys

from . import __version__
from .config import Config, ConfigError, load_config
from .log import setup_logging
from .state.db import StateStore


def _load(args: argparse.Namespace) -> tuple[Config, StateStore]:
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    setup_logging(
        args.log_level or cfg.log.level, cfg.log.json, args.packet_debug or cfg.log.packet_debug
    )
    return cfg, StateStore(cfg.state.path)


async def _probe(cfg: Config, store: StateStore, dry_run: bool) -> int:
    from .i3 import packets as pk
    from .i3.client import SERVICES, I3Client
    from .i3.lpc import encode

    m = cfg.mud
    router = cfg.routers[0]
    pw = store.get_password(m.name, router.name)
    req = pk.build_startup_req(
        mud=m.name, router=router.name, password=pw or 0, old_mudlist_id=0, old_chanlist_id=0,
        player_port=m.player_port, tcp_port=m.tcp_port, udp_port=m.udp_port, mudlib=m.mudlib,
        base_mudlib=m.base_mudlib, driver=m.driver, mud_type=m.mud_type, open_status=m.open_status,
        admin_email=m.admin_email, services=SERVICES,
    )  # fmt: skip
    print(f"router      : {router.name} {router.host}:{router.port}")
    print(f"mud name    : {m.name}")
    print(f"password    : {'stored' if pw else 'none yet (will request one)'}")
    print(f"startup-req : {encode(req.to_lpc())}")
    if dry_run:
        return 0

    class Null:
        async def handle(self, client: I3Client, pkt: pk.Packet) -> None:
            print(f"<< {pkt.type} from {pkt.orig_mud}")

    client = I3Client(cfg, store, Null())
    errors: list[str] = []

    async def on_err(e: pk.ErrorPacket) -> None:
        errors.append(f"{e.code}: {e.message}")

    client.on_router_error.append(on_err)
    task = asyncio.create_task(client.run())
    try:
        await client.wait_ready(
            timeout=cfg.i3.connect_timeout_seconds + cfg.i3.startup_timeout_seconds + 5
        )
    except TimeoutError:
        client.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)
        print(f"FAILED: no startup-reply. last error: {client.stats.last_error}")
        print(f"router errors: {errors}")
        return 1
    await asyncio.sleep(3)  # let mudlist/chanlist arrive
    muds = store.get_mudlist()
    chans = store.get_chanlist()
    print(f"connected   : {client.router_name}; password stored for {m.name}")
    print(f"mudlist     : {sum(1 for x in muds.values() if x.up)} up / {len(muds)} known")
    print(f"chanlist    : {len(chans)} channels, e.g. {', '.join(list(chans)[:8])}")
    ours = muds.get(m.name)
    print(f"our entry   : {'present, state ' + str(ours.state) if ours else 'not in mudlist yet'}")
    client.stop()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, 10)
    return 0


def _dump_state(cfg: Config, store: StateStore) -> None:
    muds = store.get_mudlist()
    out = {
        "mud_name": cfg.mud.name,
        "routers": {
            r.name: {
                "password_stored": store.get_password(cfg.mud.name, r.name) is not None,
                "mudlist_id": store.get_list_ids(cfg.mud.name, r.name)[0],
                "chanlist_id": store.get_list_ids(cfg.mud.name, r.name)[1],
            }
            for r in cfg.routers
        },
        "mudlist": {"total": len(muds), "up": sum(1 for m in muds.values() if m.up)},
        "chanlist": sorted(store.get_chanlist()),
        "links": [dataclasses.asdict(lnk) for lnk in store.get_links()],
        "users": len(store.all_users()),
        "ignores": store.list_ignores(),
        "provisioned": {
            "admin_role_id": store.get_setting("admin_role_id"),
            "tells_channel_id": store.get_setting("tells_channel_id"),
        },
    }
    print(json.dumps(out, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="i3bridge", description="Discord <-> InterMUD-3 bridge")
    parser.add_argument("--version", action="version", version=f"i3bridge {__version__}")
    parser.add_argument(
        "-c", "--config", help="path to config.toml (default: $I3B_CONFIG or ./config.toml)"
    )
    parser.add_argument("--log-level", help="override log level")
    parser.add_argument("--packet-debug", action="store_true", help="log every raw I3 packet")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the Discord bridge")
    run.add_argument(
        "--no-i3", action="store_true", help="Discord side only; do not connect to the router"
    )
    probe = sub.add_parser(
        "probe", help="handshake with the router and report; registers the mud name"
    )
    probe.add_argument(
        "--dry-run", action="store_true", help="print the startup packet without connecting"
    )
    sub.add_parser("repl", help="chat on I3 from this terminal (no Discord)")
    sub.add_parser("dump-state", help="print persisted state as JSON")
    sub.add_parser(
        "forget-discord",
        help="drop Discord-specific state (channel links, provisioned role/channel ids) "
        "before pointing the bridge at a different server; keeps the router password",
    )
    args = parser.parse_args(argv)

    cfg, store = _load(args)
    try:
        if args.command == "probe":
            sys.exit(asyncio.run(_probe(cfg, store, args.dry_run)))
        elif args.command == "repl":
            from .console import repl

            asyncio.run(repl(cfg, store))
        elif args.command == "dump-state":
            _dump_state(cfg, store)
        elif args.command == "forget-discord":
            n = store.clear_links()
            store.delete_setting("admin_role_id")
            store.delete_setting("tells_channel_id")
            print(
                f"dropped {n} channel link(s) and the provisioned role/channel ids; "
                "router password, users and ignores kept"
            )
        elif args.command == "run":
            from .bridge.app import run_bridge

            sys.exit(asyncio.run(run_bridge(cfg, store, with_i3=not args.no_i3)))
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
