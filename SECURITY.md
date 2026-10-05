# Security notes

## Threat model

The bridge sits between two networks with very different trust levels. Discord is
authenticated and encrypted; InterMUD-3 is a 1990s hobbyist protocol spoken over plain TCP by
dozens of independently run muds. Everything arriving from I3 is treated as hostile input.

## What the I3 protocol cannot give us

- **No transport security.** I3 routers speak unencrypted TCP; `*dalet` and `*i4` offer no
  TLS endpoint, and the specification has none. The bridge cannot add encryption
  unilaterally. Channel messages and tells are therefore readable on the path, as they are
  for every mud on the network.
- **The router password is a name-ownership token, not a secret that protects data.** It is an
  integer the router assigns on first contact and expects back on every connection, sent in
  clear text. Someone who captures it could impersonate the mud name `Discord` on the network
  until the router operator resets it. It protects nothing else: it is not reused anywhere and
  grants no access to Discord. We still keep it out of logs and in a 0600 file.

## What the bridge does

- Validates and bounds every packet: frame size cap, parser depth and numeric-literal limits,
  tolerant decoding that drops a bad packet instead of dropping the session, bounded caches.
- Strips control characters and ANSI/Pinkfish codes from inbound text, escapes Discord
  markdown, and disables all mentions on relayed messages.
- Correlates replies to our own queries by recipient, kind and the mud we asked, so a third
  mud cannot answer a `who` on another mud\'s behalf.
- Accepts slash commands only from the configured guild, leaves any other guild it is invited
  to, and only accepts DMs from members of the configured guild.
- Never sends Discord user ids, emails or IP addresses to the network; `finger` replies carry
  display name, role, idle time and status only.
- Redacts router passwords and tell bodies from packet-debug logs and from `probe` output.

## Secrets and where they live

| Secret | Location | Notes |
|--------|----------|-------|
| Discord bot token | `.env` as `DISCORD_TOKEN` | gitignored; mode 0600 recommended |
| Router password | `state/bridge.db` | created 0600; back the directory up |
| Webhook tokens | `state/bridge.db` | channel-scoped; rotated by deleting the webhook |

## Reporting

Open an issue or contact the repository owner. Please do not post captured packets that
contain other people\'s tells.
