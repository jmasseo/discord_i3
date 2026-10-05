"""Discord user <-> I3 identity. I3 usernames are lowercase ASCII; visnames are display names."""

from __future__ import annotations

import dataclasses
import re
import time

from ..discord_.format import ascii_name
from ..state.db import StateStore, UserRecord

GENDERS = {"male": 0, "female": 1, "neuter": 2}
GENDER_NAMES = {v: k for k, v in GENDERS.items()}
NICK_RE = re.compile(r"^[a-z][a-z0-9]{1,19}$")
RESERVED = {"console", "bridge", "discord", "admin", "root", "system", "everyone", "here"}


class IdentityError(ValueError):
    pass


class IdentityMap:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def derive_name(display_name: str) -> str:
        base = re.sub(r"[^a-z0-9]", "", ascii_name(display_name).lower())
        base = base.lstrip("0123456789")[:20]
        return base if len(base) >= 2 else "user"

    def get(self, discord_id: int) -> UserRecord | None:
        return self.store.get_user(discord_id)

    def by_i3_name(self, name: str) -> UserRecord | None:
        return self.store.get_user_by_i3_name(name)

    def ensure(self, discord_id: int, display_name: str) -> UserRecord:
        """Return the record for a Discord user, creating one with a derived I3 name."""
        existing = self.store.get_user(discord_id)
        visname = ascii_name(display_name)
        if existing is not None:
            if existing.visname != visname:
                existing = dataclasses.replace(existing, visname=visname)
                self.store.upsert_user(existing)
            return existing
        base = self.derive_name(display_name)
        name = base
        n = 2
        while name in RESERVED or self.store.i3_name_taken(name):
            name = f"{base[: 20 - len(str(n))]}{n}"
            n += 1
        record = UserRecord(discord_id, name, visname, last_seen=time.time())
        self.store.upsert_user(record)
        return record

    def set_nick(self, discord_id: int, display_name: str, nick: str) -> UserRecord:
        nick = nick.strip().lower()
        if not NICK_RE.match(nick):
            raise IdentityError("I3 names are 2-20 characters, a-z and 0-9, starting with a letter")
        if nick in RESERVED:
            raise IdentityError(f"{nick} is reserved")
        if self.store.i3_name_taken(nick, except_discord_id=discord_id):
            raise IdentityError(f"{nick} is already taken by another member")
        current = self.ensure(discord_id, display_name)
        record = dataclasses.replace(current, i3_name=nick)
        self.store.upsert_user(record)
        return record

    def _update(self, discord_id: int, display_name: str, **fields: object) -> UserRecord:
        current = self.ensure(discord_id, display_name)
        record = dataclasses.replace(current, **fields)  # type: ignore[arg-type]
        self.store.upsert_user(record)
        return record

    def set_gender(self, discord_id: int, display_name: str, gender: str) -> UserRecord:
        if gender not in GENDERS:
            raise IdentityError("gender must be male, female or neuter")
        return self._update(discord_id, display_name, gender=GENDERS[gender])

    def set_hidden(self, discord_id: int, display_name: str, hidden: bool) -> UserRecord:
        return self._update(discord_id, display_name, hidden=hidden)

    def set_dm(self, discord_id: int, display_name: str, dm_tells: bool) -> UserRecord:
        return self._update(discord_id, display_name, dm_tells=dm_tells)
