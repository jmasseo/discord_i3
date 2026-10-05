# Operations

## Endpoints

| Path | Meaning |
|------|---------|
| `GET /health` | process is alive; JSON with I3 state, router, Discord readiness, linked channels |
| `GET /health/ready` | 200 only when both the router link and Discord are up |
| `GET /metrics` | Prometheus text: `i3bridge_i3_connected`, packets in/out, reconnects, relay counters, queue depth, linked channels, online members |

## Logs

`log.level` and `log.json` in `config.toml`. `i3bridge --packet-debug run` (or `log.packet_debug = true`)
logs every I3 packet on the `i3bridge.packets` logger; useful when a router does something new.
Router passwords and tell bodies are redacted in that output, but channel traffic and user
names are not, so treat packet-debug logs as private and do not leave the mode on in production.

## State

`state/bridge.db` (SQLite, WAL) holds the router-issued password, mudlist/chanlist ids and
caches, channel links with webhook tokens, user identities, tell reply targets and ignore
patterns. Back it up; losing the password means asking the router operator to reset the
mud name. `i3bridge dump-state` prints a summary.

## Routine tasks

- `/i3 status` in Discord shows link state, counters and listed members.
- `/i3 join <channel>` / `/i3 leave <channel> [delete]` / `/i3 link <channel> #existing`.
- `/i3 ignore add user@mud` silences an abusive source; `@mud` silences a whole mud.
- The bridge re-sends `channel-listen` for every linked channel after each reconnect and
  reconciles Discord channels, topics and webhooks after every chanlist update.

## Shutdown

SIGTERM/SIGINT send a `shutdown` packet to the router, flush the relay and close Discord.
Routers mark a mud down after a grace period; a quick restart is invisible to the network.

## Container notes

- The image runs as uid/gid 1000. If your `state/` directory is owned by another user, build
  with `--build-arg UID=<uid> --build-arg GID=<gid>`.
- Rootless podman maps container uid 1000 to a subordinate uid, so add `--userns=keep-id`
  (or include `deploy/podman-compose.yml`, which sets `userns_mode: keep-id`) or the bridge
  cannot open the database.
- On SELinux hosts bind mounts need the `:z` label, as in the README examples.
- `docker logs -f i3bridge` shows the bridge log; `--log-level DEBUG` can be appended to the
  container command for a verbose run.

## Moving to another host

The mud name `Discord` is bound to the router-issued password in `state/bridge.db`; that
file is the whole identity. To move:

1. Stop the bridge on the old host (so the SQLite WAL is checkpointed and the router sees a
   clean `shutdown`). Never run two copies at once: the router keeps one session per name
   and the newer connection displaces the older one.
2. Copy `config.toml`, `.env` (the token) and the `state/` directory. The compose file
   bind-mounts `./state` into the container, so it is an ordinary directory on the host.
3. Start the bridge. Nothing changes on Discord: the same guild, role, channels and webhook
   tokens are in the database. The router simply sees the same mud from a new IP.

## Moving to another Discord server

1. Create the server and invite the bot (the same application and token work in any server;
   a new application just means a new `DISCORD_TOKEN`). Kick the old bot application from the
   old server if you made a new one, or its stale slash commands linger there.
2. Stop the bridge and run `i3bridge forget-discord`. It drops the channel links (whose
   Discord channel and webhook ids belong to the old server) and the remembered role and
   tells-channel ids, and keeps the router password, user identities and ignore list.
   Skipping this step is safe but noisier: on startup the bridge notices the old channels are
   gone, unlinks them, leaves those I3 channels, and provisions a new role and tells channel.
3. Set `discord.guild_id` in `config.toml` to the new server's id and start the bridge. It
   creates the category, `i3-admin` role and `#i3-tells` channel, then `/i3 join` the channels
   you want again.

User identities are keyed by Discord user id, so members who join the new server keep their
I3 names, tell reply targets and settings. Other muds see the same mud `Discord` throughout;
only the people behind it change.
