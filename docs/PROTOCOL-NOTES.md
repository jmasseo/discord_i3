# InterMUD-3 protocol notes

Facts the implementation relies on, with where they came from. Update this file whenever a
live router does something the specification does not say.

## Wire format ("MudMode")

- Each packet: 4-byte big-endian length, then the LPC text, then one NUL byte. The length
  **includes** the NUL (mudstandards.org spec text; LuminariMUD gateway `mudmode.py`).
  We strip a trailing NUL on input if present and tolerate its absence.
- Encoding on the wire is undefined. We decode as UTF-8 when valid and Latin-1 otherwise;
  we encode UTF-8 (configurable) after the format layer has already transliterated text to
  ASCII, so in practice our packets are 7-bit.
- Maximum accepted frame is configurable (`i3.max_packet_bytes`, default 2 MiB). A full
  mudlist can be hundreds of KB. An oversize header is unrecoverable: we reconnect.

## LPC serialisation

- Arrays `({ a, b, })`, mappings `([ k : v, ])`, both with trailing commas as MudOS writes
  them. We emit no whitespace; we accept whitespace between tokens and a missing final comma.
- `0` is the null for any type. Python `None` encodes as `0`; a `0` where a string is
  expected is read as the empty string by the packet parsers.
- String escapes follow MudOS `restore_string`: a backslash makes the next character literal,
  whatever it is. So `\"` is a quote, `\\` a backslash, and `\n` is the letter **n**. A raw
  newline inside a string is a newline. We never send newlines: the format layer flattens
  them. (The LuminariMUD decoder treats `\n` as newline, which MudOS would not; we follow MudOS.)
- Floats are rare in I3 traffic; we accept `-?d+.d+` with an optional exponent.

## Session

- `startup-req-3` field order is in `packets.build_startup_req`. Password `0` means "new
  mud, please assign one". The `startup-reply` password is stored immediately and reused
  forever for that mud name. A mud name that already exists with a different password is
  refused (`not-allowed`).
- `startup-reply.router_list` is `({ ({ "*name", "host port" }), ... })`. If the first entry
  is not the router we are on, we must move there.
- Routers push `mudlist` and `chanlist-reply` after startup. Entries whose value is `0` are
  deletions. Persisting `mudlist_id`/`chanlist_id` and sending them in the next
  `startup-req-3` gets us deltas only.
- No protocol-level keepalive exists. LuminariMUD's code comments that the router closes idle
  connections; we re-affirm a `channel-listen` when quiet (configurable) and reconnect after
  a long silence.
- `shutdown` carries a restart-delay hint in seconds; we send it on graceful stop.

## Observed on `*dalet` (2026-10-05)

- `player_port = 0` is accepted; `Discord` registered on first contact and the password was
  returned in `startup-reply`.
- The `startup-reply` router list contains only `*dalet` itself, so no redirect happens.
- The full mudlist arrives as many `mudlist` packets of ten entries each, and the
  `mudlist_id` on those chunks is not monotonic (values like 1202776, 2039478, ...). Live
  updates afterwards carry a monotonic id (665201xx) and usually one entry each. The router
  re-sent the full list even though we presented the id stored by the previous session, so
  treat `old_mudlist_id` as a hint, not a guarantee of deltas. About 450 mudlist packets
  arrived in the first 45 seconds, including a burst of single-entry packets sharing one id;
  per-packet logging is therefore at DEBUG.
- At least one mud advertises services with string values (for example `"1"` or a port as a
  string). The parser accepts numeric strings as numbers and any other non-empty string as
  "supported", and skips an individual unparseable mud instead of rejecting the packet.
- The chanlist had 169 channels and arrived in one packet.
- Muds on the network send UTF-8 emoji in channel text and they display fine on Discord, so
  the outbound default (`format.transliterate = "text"`) folds accented letters to ASCII but
  passes emoji through as UTF-8.

## Open items

- Whether the router echoes our own `channel-m` back to us (we drop own-mud packets either way).
- Whether `*dalet` and `*i4` share mud registrations (irn), i.e. whether the password issued
  by one is valid on the other.
