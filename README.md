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

The `Dockerfile` at the repo root builds an image that runs as an unprivileged user (uid/gid
1000 by default, override with `--build-arg UID=... GID=...`). The container reads
`/app/config.toml`, keeps its SQLite state in `/app/state`, and serves health and metrics on
port 8080. The router-assigned password lives in `state/`; back that directory up.

Docker:

```sh
docker compose -f deploy/docker-compose.yml up -d --build     # token from ./.env
```

Rootless podman needs the container user mapped onto your own uid so it can open the
bind-mounted state, and SELinux hosts (Fedora, RHEL) need the `:z` labels. With
podman-compose, add the override file:

```sh
podman-compose -f deploy/docker-compose.yml -f deploy/podman-compose.yml up -d --build
```

Or run the image directly:

```sh
podman build -t i3bridge .
podman run -d --name i3bridge --restart unless-stopped --userns=keep-id \
    --env-file .env \
    -v "$PWD/config.toml:/app/config.toml:ro,z" \
    -v "$PWD/state:/app/state:z" \
    -p 127.0.0.1:8080:8080 i3bridge
```

Check it with `curl -s localhost:8080/health/ready` and the `/i3 status` command in Discord.
