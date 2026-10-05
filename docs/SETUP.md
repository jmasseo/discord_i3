# Setup

## 1. Discord application

1. https://discord.com/developers/applications → **New Application** → name it (e.g. "I3 Bridge").
2. **Bot** tab: Reset Token, copy it into your environment as `DISCORD_TOKEN`. Never commit it.
3. Still on **Bot**, under *Privileged Gateway Intents* enable all three:
   **Presence Intent**, **Server Members Intent**, **Message Content Intent**.
   (`rwho Discord` needs the first two; relaying needs the third.)
4. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`; bot permissions
   *View Channels, Manage Channels, Manage Webhooks, Send Messages, Send Messages in Threads,
   Read Message History, Embed Links, Attach Files*. Open the generated URL and invite the bot
   to the dedicated server.

## 2. Dedicated server

Enable Developer Mode (User Settings → Advanced) so right-click → *Copy ID* works, then:

- Copy the server id into `discord.guild_id`. That is the only id you must provide.
- On first start the bot creates (or finds) the `InterMUD-3` category, an `i3-admin` role and
  an `#i3-tells` channel, and remembers their ids in `state/`. Give the role to whoever may
  manage channel links; server admins with *Manage Server* can always use the admin commands.
- To use your own role or channel instead, set `discord.admin_role_id` /
  `discord.tells_channel_id`, or turn `auto_provision` off.
- `i3bridge run --no-i3` brings up only the Discord side, handy for checking the bot before
  registering on the I3 network.

## 3. Configure and run

```sh
cp config.example.toml config.toml   # fill in the ids above
export DISCORD_TOKEN=...
.venv/bin/i3bridge probe              # registers "Discord" on *dalet, stores the password in state/
.venv/bin/i3bridge run
```

Docker: `docker compose -f deploy/docker-compose.yml up -d --build` with `DISCORD_TOKEN` in a
`.env` file next to the compose file. The bind-mounted `state/` directory holds the router
password: back it up.
With podman, build with `--format docker` if you want the image's HEALTHCHECK kept; the
`/health` endpoint works either way.
