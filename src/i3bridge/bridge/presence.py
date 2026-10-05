"""Who is online on Discord, for ``rwho Discord``, ``finger``, ``locate`` and ``chan-who``.

The table is fed with :class:`MemberSnapshot` values from Discord events (and a full scan at
startup) and reports each member's actual status. Idle is measured from the later of the
member's last message and last status change, because Discord does not expose idle time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import PresenceConfig
from ..i3.packets import FingerReply, WhoEntry
from .identity import GENDER_NAMES, IdentityMap
from .ports import LocateInfo

ONLINE_STATUSES = frozenset({"online", "idle", "dnd"})


@dataclass(slots=True, frozen=True)
class MemberSnapshot:
    id: int
    display_name: str
    bot: bool
    status: str  # online | idle | dnd | offline | unknown (no Presences intent)
    activity: str | None = None  # "Playing Nethack", custom status text, ...
    top_role: str | None = None


@dataclass(slots=True)
class _Entry:
    snap: MemberSnapshot
    last_status_change: float
    last_message: float = 0.0
    login_time: float | None = None
    history: list[str] = field(default_factory=list)


class PresenceTable:
    def __init__(
        self,
        identity: IdentityMap,
        cfg: PresenceConfig,
        *,
        can_view: Callable[[int, str], bool],
        bridged_channels: Callable[[], set[str]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.identity = identity
        self.cfg = cfg
        self._can_view = can_view
        self._bridged = bridged_channels
        self._clock = clock
        self._entries: dict[int, _Entry] = {}

    # ------------------------------------------------------------------- feeding

    def update(self, snap: MemberSnapshot) -> None:
        now = self._clock()
        entry = self._entries.get(snap.id)
        if entry is None:
            self._entries[snap.id] = _Entry(
                snap, now, login_time=now if snap.status in ONLINE_STATUSES else None
            )
            return
        if entry.snap.status != snap.status:
            entry.last_status_change = now
            if snap.status in ONLINE_STATUSES and entry.snap.status not in ONLINE_STATUSES:
                entry.login_time = now
        entry.snap = snap

    def remove(self, member_id: int) -> None:
        self._entries.pop(member_id, None)

    def saw_message(self, member_id: int) -> None:
        entry = self._entries.get(member_id)
        if entry is not None:
            entry.last_message = self._clock()

    def snapshot(self, member_id: int) -> MemberSnapshot | None:
        entry = self._entries.get(member_id)
        return None if entry is None else entry.snap

    # ------------------------------------------------------------------ queries

    def is_online(self, member_id: int) -> bool:
        entry = self._entries.get(member_id)
        if entry is None or entry.snap.bot:
            return False
        if entry.snap.status == "unknown":
            return self._clock() - entry.last_message < self.cfg.fallback_window_seconds
        return entry.snap.status in ONLINE_STATUSES

    def _in_scope(self, member_id: int) -> bool:
        scope = self.cfg.scope
        if scope == "all":
            return True
        if scope == "bridged":
            return any(self._can_view(member_id, ch) for ch in self._bridged())
        record = self.identity.get(member_id)
        return record is not None

    def _listed(self, member_id: int) -> bool:
        if not self.is_online(member_id) or not self._in_scope(member_id):
            return False
        record = self.identity.get(member_id)
        return not (record is not None and record.hidden)

    def online_ids(self) -> list[int]:
        return [mid for mid in self._entries if self._listed(mid)]

    def _idle(self, entry: _Entry) -> int:
        return max(0, int(self._clock() - max(entry.last_message, entry.last_status_change)))

    def _extra(self, entry: _Entry) -> str:
        status = entry.snap.status if entry.snap.status != "unknown" else "online"
        tag = f"[{status}]"
        return f"{tag} {entry.snap.activity}"[:60] if entry.snap.activity else tag

    def _record(self, member_id: int) -> tuple[str, int]:
        snap = self._entries[member_id].snap
        record = self.identity.ensure(member_id, snap.display_name)
        return record.visname, record.gender

    def who_list(self) -> list[WhoEntry]:
        out: list[WhoEntry] = []
        for mid in self.online_ids():
            entry = self._entries[mid]
            visname, _ = self._record(mid)
            out.append(WhoEntry(visname, self._idle(entry), self._extra(entry)))
        out.sort(key=lambda e: e.visname.lower())
        return out

    def _find(self, username: str) -> tuple[int, _Entry] | None:
        record = self.identity.by_i3_name(username)
        if record is None:
            return None
        entry = self._entries.get(record.discord_id)
        if entry is None or not self._listed(record.discord_id):
            return None
        return record.discord_id, entry

    def finger(self, username: str) -> FingerReply | None:
        found = self._find(username)
        if found is None:
            return None
        mid, entry = found
        visname, gender = self._record(mid)
        login = (
            time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(entry.login_time))
            if entry.login_time
            else ""
        )
        return FingerReply(
            visname=visname,
            title=entry.snap.top_role or "Discord member",
            real_name="",
            email="",
            loginout_time=login,
            idle=self._idle(entry),
            ip_name="",
            level="",
            extra=f"{self._extra(entry)}; gender {GENDER_NAMES.get(gender, 'neuter')}",
        )

    def locate(self, username: str) -> LocateInfo | None:
        found = self._find(username)
        if found is None:
            return None
        mid, entry = found
        visname, _ = self._record(mid)
        status = "inactive" if entry.snap.status == "idle" else None
        return LocateInfo(visname, self._idle(entry), status)

    def chan_who(self, channel: str) -> list[str] | None:
        if channel not in self._bridged():
            return None
        names = [self._record(mid)[0] for mid in self.online_ids() if self._can_view(mid, channel)]
        return sorted(names, key=str.lower)

    def chan_user(self, username: str) -> tuple[str, int] | None:
        record = self.identity.by_i3_name(username)
        return None if record is None else (record.visname, record.gender)

    def count_online(self) -> int:
        return len(self.online_ids())
