# Discord <-> InterMUD-3 bridge.
#
#   docker build -t i3bridge .
#   docker run -d --name i3bridge --restart unless-stopped \
#       --env-file .env \
#       -v "$PWD/config.toml:/app/config.toml:ro,z" \
#       -v "$PWD/state:/app/state:z" \
#       -p 127.0.0.1:8080:8080 i3bridge
#
# The container runs as an unprivileged user. Pass --build-arg UID/GID to match the owner
# of the bind-mounted state directory (rootless podman: add --userns=keep-id at run time).
# The ":z" suffixes are SELinux labels needed on Fedora/RHEL hosts; harmless elsewhere.

FROM python:3.12-slim

ARG UID=1000
ARG GID=1000

LABEL org.opencontainers.image.title="i3bridge" \
      org.opencontainers.image.description="Discord <-> InterMUD-3 bridge that appears on I3 as the mud 'Discord'" \
      org.opencontainers.image.source="https://github.com/jmasseo/cc_discord_i3"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    I3B_CONFIG=/app/config.toml \
    I3B_STATE__PATH=/app/state/bridge.db \
    I3B_HEALTH__BIND=0.0.0.0

WORKDIR /app

# Install the package first so the dependency layer is cached across source edits.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --root-user-action=ignore . \
    && groupadd --gid "${GID}" i3bridge \
    && useradd --uid "${UID}" --gid "${GID}" --home-dir /app --no-create-home --shell /usr/sbin/nologin i3bridge \
    && mkdir -p /app/state \
    && chown -R i3bridge:i3bridge /app

USER i3bridge

VOLUME ["/app/state"]
EXPOSE 8080

# python:slim has no curl; probe the liveness endpoint with the stdlib instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

STOPSIGNAL SIGTERM
CMD ["i3bridge", "run"]
