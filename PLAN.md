# Discord ⇄ InterMUD-3 Bridge — Build Plan

Status: v3, 2026-10-02. All §2 decisions are settled except who may join channels.

## 0. Progress (2026-10-02)

| Phase | State | Notes |
|-------|-------|-------|
| 0 Bootstrap | done | repo, pyproject, ruff/mypy/pytest, CI, Dockerfile, compose, systemd unit, docs |
| 1 Wire layer | done | `i3/lpc.py`, `i3/framing.py`, `i3/packets.py`; Hypothesis round-trips; fake router |
| 2 Session | done | `i3/client.py`: handshake, password/ids persistence, deltas, redirect, failover, backoff, watchdog, shutdown |
| 3 Services | done | `i3/services/dispatcher.py` + `outbound.py`; console REPL (`i3bridge repl`); `i3bridge probe` |
| 4 Discord core | login, intent, command sync and provisioning verified live 2026-10-05 | relay and channel linking still to be exercised with a joined channel |
| 5 Bridge features | code complete; presence primed live, tells/rwho untested end to end | identity, two-way DM tells, presence for `rwho`, chanlist sync, ignores, flood control |
| 6 Ops | partial | health/metrics, JSON logs, Docker image builds; `Discord` registered on `*dalet` 2026-10-05; soak test pending |
| 7 Stretch | not started | |

85 automated tests pass (codec, framing, packets, config, state, policy, format, identity,
presence, tells, client sessions and the service dispatcher against the fake router). Nothing
has yet talked to a live router or to Discord; see "Next steps" at the end of this file.

## Next steps

1. Fill in `config.toml` (guild, category, admin role, tells channel ids) and `.env`
   (`DISCORD_TOKEN`), see `docs/SETUP.md`.
2. `i3bridge probe` from the host that will run the bridge: registers **Discord** on `*dalet`
   and stores the password in `state/`. Keep that directory.
3. `i3bridge run`, then `/i3 join <quiet test channel>` and test both directions, tells by DM,
   and `rwho Discord` from another mud.
4. Record any router quirks in `docs/PROTOCOL-NOTES.md`; soak for 48 h before announcing.

## 1. Goal and scope

Build a long-running service that:

1. Connects to an InterMUD-3 (I3) router as a **MUD** (its own mud name, router-issued
   password, declared services) so the rest of the network sees it as an ordinary mud.
2. **Manages Discord channels** on one guild: creates/links/archives a text channel per
   bridged I3 channel, keeps topics in sync with the I3 channel list, and relays traffic
   both ways.
3. Behaves like a good I3 citizen: answers remote who (`rwho Discord`), `finger` and `locate`
   with real Discord presence, delivers `tell`s both ways over DMs, replies with proper `error` packets, never loops or floods.

Out of scope for v1: OOB services (mail, news, file), hosting a filtered channel,
Grapevine, multi-guild. These are listed as stretch items in §9.

## 2. Decisions

Confirmed 2026-10-02 unless marked open.

| # | Decision | Status |
|---|----------|--------|
| 1 | I3 protocol layer | **Confirmed:** own in-process implementation (Python/asyncio), codec seeded from LuminariMUD's public-domain gateway, spec + gateway as test oracle. The sidecar alternative (running that gateway and writing a JSON-RPC client) is rejected: 0.4.x-beta, no `channel-add`/`channel-admin`/raw packets. |
| 2 | Language/stack | Python 3.12+, discord.py 2.x, asyncio, SQLite. Python because the only mature reference I3 code is Python and can be vendored. |
| 3 | Mud name | **`Discord`**. The Phase 0 probe registers it on `*dalet` (a taken name fails the handshake with `not-allowed`) and stores the assigned password in `state/`. |
| 4 | Discord channel policy | Bot creates `#i3-<channel>` under an "InterMUD-3" category on `/i3 join`; `/i3 link` maps an existing channel instead. |
| 5 | Tells | **Default to DMs, two-way:** inbound tells arrive as bot DMs; replying in the DM sends a tell back (§6 "Tells"). `#i3-tells` channel is only a fallback for users whose DMs are closed. |
| 6 | Presence | **Exposed to the network via `who-req`**, the packet behind `rwho Discord`, driven by real Discord presence and reporting each member's actual status (online, idle, dnd) without filtering or relabelling (§6 "Presence"). The same table backs `locate`, `finger` and `chan-who`. |
| 7 | Deployment | **Docker Compose** with a persistent `state/` volume (router password lives there). |
| 8 | Guild | **A new Discord server dedicated to the bridge.** Single guild in v1; guild id in config so multi-guild is a refactor, not a rewrite. Because the server exists for the bridge, presence scope defaults to all online members. |

## 3. Research findings (verified 2026-10-02)

### Routers

| Router | Address | Status 2026-10-02 | Notes |
|--------|---------|-------------------|-------|
| `*dalet` | 97.107.133.86:8787 | up (confirmed by you; TCP connect OK from this host) | **Primary.** Run by Cratylus. Also accepts connections on 8080. |
| `*i4` | 204.209.44.3:8080 | up (TCP connect OK) | **Fallback.** Same operator; the routers are inter-linked ("irn"). Default in the LuminariMUD gateway, live-tested there 2026-07-28. |
| `*arthmoor` | i3.intermud.us:9900 | responding (realmofshadows.us) | Realm of Shadows' router. |
| `*yatmim` | 149.152.218.102:23 | responding (realmofshadows.us) | |
| `*wpr` | 195.242.99.94:8080 | no response | Address changed from 136.144.155.250. |
| `*Kelly` | 150.101.219.57:8080 | offline (Jul 2025 list) | |

Default `routers` order in config: `*dalet`, then `*i4`. The `startup-reply` router list can
still redirect us. Liveness pages: https://realmofshadows.us/i3.php (live),
https://wotf.org/i3/, https://zone.wotf.org/mud/mudlist.

### Protocol facts that drive the design

- **Transport ("MudMode"):** TCP; each packet is a 4-byte **big-endian** length followed by
  the LPC-serialised text and a trailing NUL; the length **includes** the NUL. Encoding is
  undefined on the wire (treat as Latin-1 on input, transliterate to ASCII on output by default).
- **Serialisation:** MudOS `save_variable` style. Arrays `({ a, b, })`, mappings
  `([ k : v, ])`, integers, rare floats, strings in double quotes with `\"` and `\\` escapes.
  Integer `0` doubles as nil anywhere a string/array/mapping is expected. Trailing commas are
  mandatory on output; tolerate whitespace and `\n`/`\t`/`\r` escapes on input.
- **Every packet:** `({ type, ttl, orig_mud, orig_user, target_mud, target_user, ...payload })`, ttl is always `5`.
- **Startup:** `startup-req-3` carries `password` (0 on first contact), `old_mudlist_id`,
  `old_chanlist_id`, ports, mudlib/driver strings, `open_status`, `admin_email`, a
  `services` mapping and `other_data`. `startup-reply` returns `router_list` and the
  **password the router assigned**. If `router_list[0]` is not the router we are on,
  we must disconnect and connect there instead.
- **State sync:** router pushes `mudlist` (mapping mud → info array or `0` = deleted, with
  `mudlist_id`) and `chanlist-reply` (mapping channel → `({ host_mud, type })` or `0`, with
  `chanlist_id`). Persisting both ids means reconnects receive deltas only.
- **Channels:** `channel-listen` (on/off) per channel; inbound traffic is `channel-m`
  (message), `channel-e` (emote, `$N` = actor) and `channel-t` (targeted emote, `$N`/`$O`).
  `chan-who-req/reply`, `chan-user-req/reply` give per-channel presence and visname/gender.
  Channel types: 0 selectively banned, 1 selectively admitted, 2 filtered.
- **Other services:** `tell`, `emoteto`, `who-req/reply`, `finger-req/reply`,
  `locate-req/reply` (broadcast, target mud 0), `ucache-update` (visname/gender cache),
  `error` (codes `unk-dst not-imp unk-type unk-src bad-pkt bad-proto not-allowed unk-user unk-channel`),
  `shutdown` with a restart-delay hint. There is no protocol-level keepalive; the mud is
  responsible for detecting a dead link and reconnecting.

### Prior art to reuse or crib from

- **LuminariMUD/Intermud3** (Python 3.12, Unlicense): `src/network/lpc.py` and
  `src/network/mudmode.py` are a working codec + framer; `src/state/manager.py` shows
  password/mudlist persistence; `config/config.yaml` lists a sane services set
  (`tell emoteto channel who finger locate chanlist-req chanlist-reply chan-who-req chan-who-reply auth ucache`)
  and timing defaults (reconnect 30 s, keepalive 60 s). Confirm the LICENSE file says Unlicense
  before vendoring (their `pyproject.toml` says MIT; either is fine, note which).
- **LuminariMUD/discord-mud-chat** (Node, ISC): Discord ⇄ MUD JSON bridge, not I3, but its
  mention-stripping, emoji-removal and rate-limit handling are directly relevant.
- **CoffeeMud** (Java) and **Tricky1702/I3Bukkit** (Java) have independent I3 clients useful
  as second opinions on edge cases.
- Spec mirrors: https://mudstandards.org/intermud/intermud3/ and
  https://wl.mud.de/mud/doc/misc/intermud3.html.

## 4. Architecture

```
            Discord Gateway/REST                       I3 router (*i4 / *dalet)
                   │  (discord.py, webhooks)                      │  (MudMode TCP)
                   ▼                                              ▼
 ┌───────────────────────────┐    in-proc asyncio     ┌───────────────────────────┐
 │ discord_/                 │◄──── events/queues ───►│ i3/                       │
 │  bot, commands, channels, │                        │  framing, lpc codec,      │
 │  relay (rate-limited),    │                        │  client state machine,    │
 │  format                   │                        │  services (channel, tell, │
 └─────────────┬─────────────┘                        │  who, finger, locate,     │
               │                                      │  ucache, error)           │
               ▼                                      └─────────────┬─────────────┘
       ┌───────────────────────────── bridge/ ─────────────────────┐│
       │ core routing · identity map · presence · policy (ignore,  │◄┘
       │ flood, limits) · loop prevention                          │
       └───────────────────────────┬───────────────────────────────┘
                                   ▼
                  state/ (SQLite): router password, mudlist_id,
                  chanlist_id, mudlist, chanlist, channel links,
                  webhooks, user identities, ignore lists
                                   ▲
                  health.py (HTTP /health, /metrics) · config.py (TOML + env)
```

One Python process, three asyncio subsystems joined by typed in-memory queues. Discord
and I3 I/O are each behind a small interface so the bridge core is testable with fakes.

## 5. I3 layer — detailed design

**framing.py** — `MudModeReader(StreamReader)` / `MudModeWriter`. Reads 4-byte header, enforces a
max packet size (default 64 KiB; drop + log + continue), strips trailing NUL if present, yields
bytes. Writer appends NUL and prefixes length. Handles partial reads.

**lpc.py** — `encode(obj) -> str`, `decode(text) -> obj`. Python `list` ↔ array, `dict` ↔ mapping,
`int`, `float`, `str`, `None`→`0`. Decoder is a hand-written recursive-descent parser with a
depth limit (e.g. 32) and tolerant whitespace. Hypothesis round-trip tests plus golden
fixtures captured from a real router.

**packets.py** — dataclasses per packet type with `from_lpc()` / `to_lpc()`; unknown types
pass through as `RawPacket` so we can reply `unk-type`.

**client.py** — connection state machine:

```
DISCONNECTED → CONNECTING → AWAIT_STARTUP_REPLY → SYNCING (mudlist/chanlist) → READY
      ▲                                                                  │
      └──────────── backoff (1s·2ⁿ, cap 5 min, jitter) ◄── error/EOF/idle ┘
```

- Router list from config, ordered; honour `router_list[0]` from `startup-reply`.
- Persist password immediately on `startup-reply`; never send a different password for the
  same mud name. Password `0` is sent only when state has none.
- Send `old_mudlist_id`/`old_chanlist_id` from state; apply deltas (value `0` = delete).
- Idle watchdog: routers emit `mudlist` updates regularly; if nothing arrives for
  `idle_reconnect_after` (default 30 min) reconnect. Enable TCP keepalive on the socket.
- On every (re)connect once READY: re-send `channel-listen on` for every bridged channel,
  then push `ucache-update` for active users (optional).
- Graceful stop: send `shutdown` with restart delay, flush, close.
- Service dispatch table; anything unimplemented → `error not-imp`; unknown target user →
  `error unk-user`; malformed → `error bad-pkt` (never crash).
- Loop prevention: drop any inbound packet whose `orig_mud` equals our mud name.

**services/** — one module each: `channel` (listen, m/e/t in+out, chan-who, chan-user),
`tell` (tell + emoteto in/out), `who`, `finger`, `locate` (reply only when the user is
present; never return IPs; email `0`), `ucache`.

Declared services (v1): `tell emoteto channel who finger locate chan-who-req chan-who-reply ucache`.
`auth`, OOB and `channel-filter` stay `0`.

## 6. Discord layer — detailed design

**Intents:** Guilds, Guild Messages, Direct Messages, Message Content, Guild Members, Presences.
The last three are privileged: enable them in the Developer Portal (no verification needed under
100 guilds). Members + Presences are what let `rwho Discord` report real online status.

**Channel management (channels.py):**
- Ensure an `InterMUD-3` category exists (configurable name/id).
- `/i3 join <channel>` (admin): validate against chanlist → `channel-listen on` → create
  `#i3-<channel>` (or reuse via `/i3 link <channel> #existing`) → create webhook → set topic
  `I3 channel "<name>" hosted by <mud> · type <admitted|banned|filtered>` → persist link.
- `/i3 leave <channel>`: `channel-listen off`, mark topic `[unlinked]`, keep history
  (`--delete` flag removes the channel).
- Reconciliation task: on startup and on every `chanlist-reply`, verify each linked
  Discord channel and webhook still exist (recreate webhook if deleted), update topics,
  flag channels that vanished from the network (post a notice, make read-only).

**Relay (relay.py):** per-Discord-channel asyncio queue → webhook `send` with
`username="Visname@Mud"`, `allowed_mentions=none`, `wait=False`. Coalesce consecutive
lines from the same sender within 1 s into one message. Respect discord.py's rate-limit
handling; cap queue depth and drop with a summary notice on I3 floods (default > 20 msgs/10 s
per channel). Webhook usernames must be 1–80 chars and may not contain "clyde" or "discord";
sanitise and fall back to `Visname (Mud)`.

**Formatting (format.py):**
- I3 → Discord: strip Pinkfish `%^…%^` codes (optional ANSI mode renders them in an
  ```ansi``` block), strip control bytes / telnet IAC, escape Discord markdown, render
  `channel-e` as `*Visname@Mud message*`, `channel-t` using `$N`/`$O` substitution.
- Discord → I3: resolve `<@id>` → `@DisplayName`, `<#id>` → `#name`, `<:emoji:id>` →
  `:emoji:`, unwrap light markdown, append attachment URLs, prefix replies with
  `[re Author]`. Join multi-line to one line (configurable: split into several `channel-m`).
  Transliterate non-ASCII to ASCII by default (`unidecode`), cap length (default 900 chars,
  split on sentence boundary). Messages that are fully italic `_text_` (Discord's built-in
  `/me`) or sent via `/i3 me` become `channel-e`.

**Identity (identity.py):** Discord user → I3 username = lowercase alnum of display name,
collision-resolved and persisted; visname = display name. `/i3 nick <name>` and
`/i3 gender` let users override. Reverse lookup serves inbound tells and finger.

**Tells (tells.py), two-way over DMs:** an inbound `tell`/`emoteto` whose `target_user`
resolves through the identity map is delivered as a bot DM: `Visname@Mud tells you: …`
(emoteto rendered as `Visname@Mud <emote>`). Each DM conversation is a tell console:

- a plain message typed in the DM goes back to that user's most recent correspondent as a
  `tell`; the reply target is stored per user and updated on every inbound or outbound tell;
- `user@mud message` or `/i3 tell user@mud message` addresses someone explicitly,
  `/i3 emoteto` sends an emote, and `:text` is shorthand for an emoteto to the reply target;
- remote `error` packets (`unk-user`, `unk-dst`) are relayed into the DM so the user knows
  the tell failed; the bridge echoes `You tell Visname@Mud: …` as confirmation.

Delivery: if the DM is refused (user blocks DMs from server members) fall back to a mention
in `#i3-tells`; if `target_user` is unknown, or the user ran `/i3 dm off` and no tells channel
is configured, reply `error unk-user`. Slash commands used in DMs must be registered
**globally**, not guild-scoped, so the text syntax is the primary DM interface. The per-user
outbound rate limit (default 10 tells/min) and the ignore list apply.

**Presence (presence.py), answers `rwho Discord`:** the bridge keeps an in-memory presence
table fed by `on_presence_update`, `on_member_update`, `on_message`, and a full member
scan at startup (member chunking on). A member appears in `who-reply` when:

- their Discord status is not `offline` (invisible members look offline to bots, so they are
  excluded, which is what invisibility asks for);
- they are not a bot or webhook;
- they are in scope (`presence.scope = "all" | "bridged" | "opt-in"`, default `all` because
  the server is dedicated to the bridge);
- they have not run `/i3 hide`.

`who-reply` rows are `({ visname, idle, xtra_info })`: visname is the display name from the
identity map; idle is seconds since the later of last message and last status change
(Discord does not expose idle duration, so this is the closest honest figure); xtra_info
reports the member's real status faithfully as `[online]`, `[idle]` or `[dnd]`, followed by
their custom status text or current activity when present, e.g. `[dnd] Playing Nethack` or
`[idle] Lurking`, truncated to 60 chars. Nobody is dropped or relabelled because of `dnd`.
`locate-reply` uses the spec's status vocabulary: `inactive` for idle, `0` (active) for
online and dnd since I3 has no "busy" state; the dnd tag still shows in `rwho`.
`finger-reply` returns visname, title (top role), login time (last transition from
offline), idle, and `0` for email and IP. `chan-who-reply` lists members who are
online and can view that linked channel. Presence changes also emit `ucache-update` for
members who have interacted with the bridge. If the Presences intent is ever unavailable,
fall back to activity-based presence (posted within `presence_window`, default 2 h) so the
bridge still answers instead of erroring.

**Slash commands (`/i3 …`):**

| Command | Who | Effect |
|---------|-----|--------|
| `channels [filter]` | all | List I3 chanlist with host mud/type and whether bridged |
| `join`, `leave`, `link` | admin role | Manage channel links (above) |
| `mudlist [filter]` | all | Summarise mudlist (up/down, driver, services) |
| `who <mud>`, `finger <user@mud>`, `locate <user>` | all | Send request, post reply ephemerally |
| `tell <user@mud> <msg>`, `emoteto …` | all | Outbound tell/emoteto from the user's I3 identity; registered globally so they work in DMs |
| `me <text>` | all | Emote to the current bridged channel |
| `nick`, `gender`, `hide|show`, `dm on|off` | all | Identity, `rwho` visibility and DM settings |
| `ignore add|remove <user@mud or mud>` | admin | Drop inbound traffic from source |
| `status` | admin | Router, state ids, queue depths, uptime |

## 7. Data model and configuration

**SQLite (`state/bridge.db`)**: `router_state(mud_name, router, password, mudlist_id, chanlist_id)`,
`mudlist(name, info_json, updated_at)`, `chanlist(name, host_mud, type, updated_at)`,
`channel_links(i3_channel, discord_channel_id, webhook_id, webhook_token, mode, created_at)`,
`users(discord_id, i3_name, visname, gender, dm_tells, hidden, last_seen)`,
`tell_targets(discord_id, peer_user, peer_mud, updated_at)`, `ignores(pattern)`.
Password and webhook tokens are secrets: file mode 0600, volume-mounted, backed up.

**config.toml** (env overrides `I3B_*`): mud identity (`mud_name`, `admin_email`, `mudlib`,
`base_mudlib`, `driver`, `mud_type`, `open_status`, `player_port`), `routers = [{name,host,port}]`,
Discord (`token` via env only, `guild_id`, `category`, `admin_role_id`, `tells_channel_id`),
formatting/limits, presence window, flood thresholds, health bind address, log level.

## 8. Repository layout and tooling

```
cc_discord_i3/
  pyproject.toml            # uv-managed; deps: discord.py, aiosqlite, unidecode, aiohttp
  config.example.toml
  src/i3bridge/
    __main__.py             # `i3bridge run`, `i3bridge probe`, `i3bridge dump-state`
    config.py  log.py  health.py
    state/db.py
    i3/  framing.py lpc.py packets.py client.py services/{channel,tell,who,finger,locate,ucache}.py
    discord_/ bot.py commands.py channels.py relay.py format.py
    bridge/ core.py identity.py presence.py tells.py policy.py
  tests/
    unit/        test_lpc.py test_framing.py test_format.py test_identity.py test_policy.py
    integration/ fake_router.py test_session.py test_bridge_flow.py
    fixtures/    captured I3 packets (golden files)
  deploy/ Dockerfile docker-compose.yml i3bridge.service
  docs/ SETUP.md OPERATIONS.md PROTOCOL-NOTES.md
```

Tooling: `uv`, `ruff`, `mypy --strict` on `i3/` and `bridge/`, `pytest` + `pytest-asyncio`,
`hypothesis`, GitHub Actions CI (lint, type-check, tests), pre-commit.

## 9. Phases

Estimates assume one developer working with Claude Code; they are rough.

### Phase 0 — Bootstrap (½ day)
- `git init`, pyproject, lint/type/test scaffolding, CI, Dockerfile, `config.example.toml`.
- `i3bridge probe` from the deployment host: full startup handshake against `*dalet` as
  `Discord`, printing the `startup-reply`. This is the real registration: it confirms the name
  is free and writes the assigned password into the `state/` volume.
- Confirm LuminariMUD license text; vendor `lpc.py`/`mudmode.py` with an attribution note.
- Create the dedicated Discord server: an `InterMUD-3` category, an `i3-admin` role and an
  `#i3-tells` fallback channel; record guild, category, role and channel ids in config.
- Register the Discord application, enable the Message Content, Server Members and Presence
  privileged intents, invite the bot with `Manage Channels`, `Manage Webhooks`, `Send Messages`,
  `Read Message History`.

Exit: CI green on an empty package; probe output recorded in `docs/PROTOCOL-NOTES.md`.

### Phase 1 — Wire layer (1–2 days)
- `framing.py`, `lpc.py`, `packets.py` with unit tests, Hypothesis round-trips, golden fixtures.
- `tests/integration/fake_router.py`: asyncio server implementing startup-reply (assigns a
  password), mudlist/chanlist push, channel-listen bookkeeping, channel-m fan-out, error
  injection, forced disconnects.

Exit: 100 % branch coverage on codec/framing; fuzzing finds no crash.

### Phase 2 — Session (1–2 days)
- `client.py` state machine, persistence of password/ids, delta application, reconnect
  with backoff, router-list failover, idle watchdog, graceful `shutdown`.
- `i3bridge run --no-discord` mode logs traffic for soak testing.

Exit: bridge appears on a live router's mudlist with the chosen mud name and survives a
simulated router restart (fake router) and a 24 h soak on the live network.

### Phase 3 — I3 services (1–2 days)
- channel listen/m/e/t in and out, chan-who/chan-user, tell/emoteto in and out,
  who/finger/locate replies and requests, ucache, error handling matrix.
- A tiny console REPL (`i3bridge repl`) to chat on a test channel without Discord.

Exit: two-way chat on a low-traffic test channel from the REPL; every inbound packet type
in the spec either handled or answered with the correct `error` code.

### Phase 4 — Discord bot core (1–2 days)
- Bot lifecycle, slash-command registration (guild-scoped for instant updates), category /
  channel / webhook manager, relay queues, formatting transforms with unit tests.
- Bridge core wiring: I3 `channel-m` → webhook; Discord message → `channel-m`; loop prevention
  (ignore webhook/bot authors, ignore own mud).

Exit: `/i3 join <test-channel>` creates the channel and messages flow both ways.

### Phase 5 — Bridge features (2 days)
- Identity map + `/i3 nick`, two-way tells over DMs with fallback, presence table and
  who/finger/locate/chan-who replies (`rwho Discord`),
  chanlist → topic sync and vanished-channel handling, `/i3 leave|link`, ignore lists,
  flood control, emotes, mudlist/chanlist browsing commands, `/i3 status`.

Exit: all commands in §6 table implemented and covered by bridge-flow tests using the fake
router and a fake Discord transport.

### Phase 6 — Ops and hardening (1–2 days)
- Health/readiness endpoints, Prometheus metrics (packets in/out, queue depth, reconnects),
  structured JSON logs with a raw-packet debug toggle, Docker Compose with state volume,
  systemd unit, backup note for `state/`.
- Security pass (see §11), dependency pinning, 48 h soak with a real channel.
- Docs: `SETUP.md`, `OPERATIONS.md`, `PROTOCOL-NOTES.md` (observed router quirks).

Exit: v1 tagged; runs unattended for a week.

### Phase 7 — Stretch (unscheduled)
- Host our own I3 channel via `channel-add` with `channel-admin` moderation
  (`/i3 channel create|ban-mud`).
- ANSI colour rendering of Pinkfish codes; per-mud avatars.
- Multi-guild; Grapevine (JSON/WebSocket intermud) as a second network adapter.
- Web status page from the health server.

## 10. Testing strategy

- **Unit:** codec, framing, formatting, identity, policy are pure and heavily tested.
- **Property-based:** Hypothesis round-trips for the codec; random byte streams into the framer.
- **Integration:** fake router drives full session and service flows in CI, including
  password assignment, delta sync, forced disconnects, `error` replies, and router-list redirect.
- **Discord:** transport behind an interface; bridge-flow tests use a fake. One manual
  checklist against a scratch guild per release.
- **Live:** `probe` and REPL against `*i4`/`*dalet` with a test mud name; soak tests before
  announcing the bridge on `intergossip`.

## 11. Security, privacy, etiquette

- Treat all I3 text as untrusted: strip control bytes, escape markdown, `allowed_mentions=none`,
  bound lengths, parser depth and packet size limits, never crash on malformed input.
- Never leak Discord ids, guild info, emails or IPs onto I3; `finger` replies omit them.
- `rwho` lists online members of the dedicated server with their true status; never
  offline or invisible members, bots, or users who ran `/i3 hide`.
- Secrets (bot token, router password, webhook tokens) live in env/volume, never in git.
- Per-user outbound rate limit; admin commands gated by role; `ignore` lists for abusive sources.
- Pick a quiet test channel for development; do not join `intergossip`/`intercre` with
  automated traffic until stable; announce the bridge and its admin contact once live.

## 12. Risks and open questions

| Risk | Mitigation |
|------|-----------|
| Router outage | Fake router for all dev/CI; ordered router list (`*dalet`, `*i4`) honouring `router_list` redirects; backoff reconnect. |
| Losing the router password | Persist on receipt, back up `state/`, document recovery (contact router admin). |
| Spec ambiguity (newline escaping, encoding) | Tolerant decoder, conservative encoder (no raw newlines), golden captures from the live router, `PROTOCOL-NOTES.md`. |
| Discord rate limits / webhook deletion by moderators | Queued relay, coalescing, reconciliation recreates webhooks. |
| Loops between bridge and other gateways on the same channel | Drop own-mud packets, ignore webhook/bot authors, per-channel flood breaker. |
| Privileged intents (Message Content, Members, Presences) | Required for relay and `rwho`; single guild keeps us under the 100-guild verification threshold; activity-based presence fallback if Presences is revoked. |

Open question for you: whether non-admins may `/i3 join` channels (the plan assumes the
`i3-admin` role only). Everything else in §2 is settled.

## 13. References

- Spec mirrors: https://mudstandards.org/intermud/intermud3/ · https://wl.mud.de/mud/doc/misc/intermud3.html
- Router liveness: https://realmofshadows.us/i3.php · https://wotf.org/i3/ · https://zone.wotf.org/mud/mudlist
- LuminariMUD gateway (Python, public domain): https://github.com/LuminariMUD/Intermud3
- LuminariMUD Discord⇄MUD bridge (Node): https://github.com/LuminariMUD/discord-mud-chat
- I3Bukkit (Java I3 client): https://github.com/Tricky1702/I3Bukkit
