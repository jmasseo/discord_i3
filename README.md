# i3bridge

A Discord ⇄ InterMUD-3 bridge. It joins the I3 network as the mud **Discord**, mirrors I3
channels into Discord channels (and back), delivers tells over DMs both ways, and answers
`rwho Discord` with real Discord presence.

See `PLAN.md` for the design, `docs/SETUP.md` to get running, and `docs/PROTOCOL-NOTES.md`
for wire-format details learned along the way.

## Quick start (development)

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml   # edit guild/category/role ids
.venv/bin/pytest -q                   # runs against a local fake router
.venv/bin/i3bridge probe              # live handshake; registers the mud name on the router
DISCORD_TOKEN=... .venv/bin/i3bridge run
```

## Deployment

`deploy/docker-compose.yml` builds the image and bind-mounts the repo's `state/` directory.
The router-assigned password lives there; back it up.
