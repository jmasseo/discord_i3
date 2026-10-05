"""SQLite-backed persistent state.

Everything the bridge must remember across restarts lives here: router credentials and
list ids, the synchronised mudlist and chanlist, Discord channel links and webhooks, user
identities, tell reply targets and ignore patterns. Operations are tiny and synchronous;
they run on the event loop thread, which is fine at the bridge's traffic levels.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..i3.packets import ChannelInfo, MudInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS router_state (
    mud_name TEXT NOT NULL, router TEXT NOT NULL,
    password INTEGER, mudlist_id INTEGER NOT NULL DEFAULT 0, chanlist_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (mud_name, router)
);
CREATE TABLE IF NOT EXISTS mudlist (
    name TEXT PRIMARY KEY, info TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chanlist (
    name TEXT PRIMARY KEY, host_mud TEXT NOT NULL, type INTEGER NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_links (
    i3_channel TEXT PRIMARY KEY, discord_channel_id INTEGER NOT NULL UNIQUE,
    webhook_id INTEGER, webhook_token TEXT, mode TEXT NOT NULL DEFAULT 'created',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    discord_id INTEGER PRIMARY KEY, i3_name TEXT NOT NULL UNIQUE, visname TEXT NOT NULL,
    gender INTEGER NOT NULL DEFAULT 2, dm_tells INTEGER NOT NULL DEFAULT 1,
    hidden INTEGER NOT NULL DEFAULT 0, last_seen REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tell_targets (
    discord_id INTEGER PRIMARY KEY, peer_user TEXT NOT NULL, peer_mud TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ignores (pattern TEXT PRIMARY KEY, added_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass(slots=True, frozen=True)
class ChannelLink:
    i3_channel: str
    discord_channel_id: int
    webhook_id: int | None
    webhook_token: str | None
    mode: str  # "created" (we made the Discord channel) or "linked" (pre-existing)


@dataclass(slots=True, frozen=True)
class UserRecord:
    discord_id: int
    i3_name: str
    visname: str
    gender: int = 2
    dm_tells: bool = True
    hidden: bool = False
    last_seen: float = 0.0


@dataclass(slots=True, frozen=True)
class TellTarget:
    peer_user: str
    peer_mud: str
    updated_at: float


class StateStore:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        if str(path) != ":memory:":
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)  # holds the router password and webhook tokens
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------- router credentials

    def get_password(self, mud: str, router: str) -> int | None:
        row = self._db.execute(
            "SELECT password FROM router_state WHERE mud_name=? AND router=?", (mud, router)
        ).fetchone()
        return None if row is None or row["password"] is None else int(row["password"])

    def any_password(self, mud: str) -> int | None:
        row = self._db.execute(
            "SELECT password FROM router_state WHERE mud_name=? AND password IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1",
            (mud,),
        ).fetchone()
        return None if row is None else int(row["password"])

    def set_password(self, mud: str, router: str, password: int) -> None:
        self._db.execute(
            "INSERT INTO router_state (mud_name, router, password, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(mud_name, router) DO UPDATE SET password=excluded.password, "
            "updated_at=excluded.updated_at",
            (mud, router, password, time.time()),
        )

    def get_list_ids(self, mud: str, router: str) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT mudlist_id, chanlist_id FROM router_state WHERE mud_name=? AND router=?",
            (mud, router),
        ).fetchone()
        return (0, 0) if row is None else (int(row["mudlist_id"]), int(row["chanlist_id"]))

    def set_list_ids(
        self,
        mud: str,
        router: str,
        *,
        mudlist_id: int | None = None,
        chanlist_id: int | None = None,
    ) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO router_state (mud_name, router, updated_at) VALUES (?,?,?)",
            (mud, router, time.time()),
        )
        if mudlist_id is not None:
            self._db.execute(
                "UPDATE router_state SET mudlist_id=?, updated_at=? WHERE mud_name=? AND router=?",
                (mudlist_id, time.time(), mud, router),
            )
        if chanlist_id is not None:
            self._db.execute(
                "UPDATE router_state SET chanlist_id=?, updated_at=? WHERE mud_name=? AND router=?",
                (chanlist_id, time.time(), mud, router),
            )

    # ------------------------------------------------------------------------ mudlist

    def apply_mudlist(self, entries: dict[str, MudInfo | None], *, replace: bool = False) -> None:
        now = time.time()
        with self._db:
            if replace:
                self._db.execute("DELETE FROM mudlist")
            for name, info in entries.items():
                if info is None:
                    self._db.execute("DELETE FROM mudlist WHERE name=?", (name,))
                else:
                    self._db.execute(
                        "INSERT INTO mudlist (name, info, updated_at) VALUES (?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET info=excluded.info, "
                        "updated_at=excluded.updated_at",
                        (name, json.dumps(dataclasses.asdict(info)), now),
                    )

    def get_mudlist(self) -> dict[str, MudInfo]:
        rows = self._db.execute("SELECT name, info FROM mudlist ORDER BY name").fetchall()
        return {row["name"]: MudInfo(**json.loads(row["info"])) for row in rows}

    def get_mud(self, name: str) -> MudInfo | None:
        row = self._db.execute("SELECT info FROM mudlist WHERE name=?", (name,)).fetchone()
        return None if row is None else MudInfo(**json.loads(row["info"]))

    def find_mud(self, name: str) -> tuple[str, MudInfo] | None:
        """Case-insensitive lookup, exact match preferred."""
        exact = self.get_mud(name)
        if exact is not None:
            return name, exact
        row = self._db.execute(
            "SELECT name, info FROM mudlist WHERE lower(name)=lower(?) LIMIT 1", (name,)
        ).fetchone()
        return None if row is None else (row["name"], MudInfo(**json.loads(row["info"])))

    # ----------------------------------------------------------------------- chanlist

    def apply_chanlist(
        self, entries: dict[str, ChannelInfo | None], *, replace: bool = False
    ) -> None:
        now = time.time()
        with self._db:
            if replace:
                self._db.execute("DELETE FROM chanlist")
            for name, info in entries.items():
                if info is None:
                    self._db.execute("DELETE FROM chanlist WHERE name=?", (name,))
                else:
                    self._db.execute(
                        "INSERT INTO chanlist (name, host_mud, type, updated_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET host_mud=excluded.host_mud, "
                        "type=excluded.type, updated_at=excluded.updated_at",
                        (name, info.host_mud, info.type, now),
                    )

    def get_chanlist(self) -> dict[str, ChannelInfo]:
        rows = self._db.execute(
            "SELECT name, host_mud, type FROM chanlist ORDER BY name"
        ).fetchall()
        return {row["name"]: ChannelInfo(row["host_mud"], int(row["type"])) for row in rows}

    def get_channel(self, name: str) -> ChannelInfo | None:
        row = self._db.execute(
            "SELECT host_mud, type FROM chanlist WHERE name=?", (name,)
        ).fetchone()
        return None if row is None else ChannelInfo(row["host_mud"], int(row["type"]))

    # ------------------------------------------------------------------ channel links

    def add_link(self, link: ChannelLink) -> None:
        self._db.execute(
            "INSERT INTO channel_links (i3_channel, discord_channel_id, webhook_id, webhook_token, "
            "mode, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(i3_channel) DO UPDATE SET "
            "discord_channel_id=excluded.discord_channel_id, webhook_id=excluded.webhook_id, "
            "webhook_token=excluded.webhook_token, mode=excluded.mode",
            (
                link.i3_channel,
                link.discord_channel_id,
                link.webhook_id,
                link.webhook_token,
                link.mode,
                time.time(),
            ),
        )

    def set_link_webhook(self, i3_channel: str, webhook_id: int | None, token: str | None) -> None:
        self._db.execute(
            "UPDATE channel_links SET webhook_id=?, webhook_token=? WHERE i3_channel=?",
            (webhook_id, token, i3_channel),
        )

    def remove_link(self, i3_channel: str) -> None:
        self._db.execute("DELETE FROM channel_links WHERE i3_channel=?", (i3_channel,))

    def clear_links(self) -> int:
        return self._db.execute("DELETE FROM channel_links").rowcount

    def get_links(self) -> list[ChannelLink]:
        rows = self._db.execute("SELECT * FROM channel_links ORDER BY i3_channel").fetchall()
        return [self._link(row) for row in rows]

    def link_for_i3(self, i3_channel: str) -> ChannelLink | None:
        row = self._db.execute(
            "SELECT * FROM channel_links WHERE i3_channel=?", (i3_channel,)
        ).fetchone()
        return None if row is None else self._link(row)

    def link_for_discord(self, discord_channel_id: int) -> ChannelLink | None:
        row = self._db.execute(
            "SELECT * FROM channel_links WHERE discord_channel_id=?", (discord_channel_id,)
        ).fetchone()
        return None if row is None else self._link(row)

    @staticmethod
    def _link(row: sqlite3.Row) -> ChannelLink:
        return ChannelLink(
            row["i3_channel"],
            int(row["discord_channel_id"]),
            None if row["webhook_id"] is None else int(row["webhook_id"]),
            row["webhook_token"],
            row["mode"],
        )

    # -------------------------------------------------------------------------- users

    def get_user(self, discord_id: int) -> UserRecord | None:
        row = self._db.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,)).fetchone()
        return None if row is None else self._user(row)

    def get_user_by_i3_name(self, i3_name: str) -> UserRecord | None:
        row = self._db.execute(
            "SELECT * FROM users WHERE lower(i3_name)=lower(?)", (i3_name,)
        ).fetchone()
        return None if row is None else self._user(row)

    def i3_name_taken(self, i3_name: str, *, except_discord_id: int | None = None) -> bool:
        row = self._db.execute(
            "SELECT discord_id FROM users WHERE lower(i3_name)=lower(?)", (i3_name,)
        ).fetchone()
        return row is not None and int(row["discord_id"]) != except_discord_id

    def upsert_user(self, user: UserRecord) -> None:
        self._db.execute(
            "INSERT INTO users (discord_id, i3_name, visname, gender, dm_tells, hidden, last_seen) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(discord_id) DO UPDATE SET "
            "i3_name=excluded.i3_name, "
            "visname=excluded.visname, gender=excluded.gender, dm_tells=excluded.dm_tells, "
            "hidden=excluded.hidden, last_seen=excluded.last_seen",
            (
                user.discord_id,
                user.i3_name,
                user.visname,
                user.gender,
                int(user.dm_tells),
                int(user.hidden),
                user.last_seen,
            ),
        )

    def touch_user(self, discord_id: int) -> None:
        self._db.execute(
            "UPDATE users SET last_seen=? WHERE discord_id=?", (time.time(), discord_id)
        )

    def all_users(self) -> list[UserRecord]:
        return [self._user(r) for r in self._db.execute("SELECT * FROM users").fetchall()]

    @staticmethod
    def _user(row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            int(row["discord_id"]),
            row["i3_name"],
            row["visname"],
            int(row["gender"]),
            bool(row["dm_tells"]),
            bool(row["hidden"]),
            float(row["last_seen"]),
        )

    # ------------------------------------------------------------------- tell targets

    def set_tell_target(self, discord_id: int, peer_user: str, peer_mud: str) -> None:
        self._db.execute(
            "INSERT INTO tell_targets (discord_id, peer_user, peer_mud, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(discord_id) DO UPDATE SET peer_user=excluded.peer_user, "
            "peer_mud=excluded.peer_mud, updated_at=excluded.updated_at",
            (discord_id, peer_user, peer_mud, time.time()),
        )

    def get_tell_target(self, discord_id: int) -> TellTarget | None:
        row = self._db.execute(
            "SELECT peer_user, peer_mud, updated_at FROM tell_targets WHERE discord_id=?",
            (discord_id,),
        ).fetchone()
        return (
            None
            if row is None
            else TellTarget(row["peer_user"], row["peer_mud"], row["updated_at"])
        )

    # ------------------------------------------------------------------------ ignores

    def add_ignore(self, pattern: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO ignores (pattern, added_at) VALUES (?,?)",
            (pattern.lower(), time.time()),
        )

    def remove_ignore(self, pattern: str) -> bool:
        cur = self._db.execute("DELETE FROM ignores WHERE pattern=?", (pattern.lower(),))
        return cur.rowcount > 0

    def list_ignores(self) -> list[str]:
        return [
            r["pattern"] for r in self._db.execute("SELECT pattern FROM ignores ORDER BY pattern")
        ]

    # ----------------------------------------------------------------------- settings

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def delete_setting(self, key: str) -> None:
        self._db.execute("DELETE FROM settings WHERE key=?", (key,))

    def set_setting(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
