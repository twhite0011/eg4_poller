#!/bin/sh
# Wraps the official influxdb entrypoint so this stack's own instance is
# fully self-provisioning -- nobody chooses or types an org name, a bucket
# name, an admin username, an admin password, or a token. Only the admin
# TOKEN is shared outward at all (to /shared/influx-admin-token), because
# eg4poll and influx-init are the only other things that ever need to
# authenticate to this Influx; nothing (not even this stack) logs into
# Influx's own UI, so the admin PASSWORD is generated, used once by the
# setup step below, and then thrown away -- it is never written anywhere.
set -eu

DATA_DIR=/var/lib/influxdb2
TOKEN_FILE=/shared/influx-admin-token

# The official image's own setup wrapper already checks for this file to
# decide whether to run `influx setup` -- mirror that check here so this
# script's behaviour stays in sync with it, rather than keying off the
# token file (which lives on a DIFFERENT volume and could theoretically
# vanish independently of the real, already-initialized data).
if [ ! -f "$DATA_DIR/influxd.bolt" ]; then
    export DOCKER_INFLUXDB_INIT_MODE=setup
    export DOCKER_INFLUXDB_INIT_USERNAME=admin
    export DOCKER_INFLUXDB_INIT_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
    export DOCKER_INFLUXDB_INIT_ORG=eg4poll
    export DOCKER_INFLUXDB_INIT_BUCKET=energy
    export DOCKER_INFLUXDB_INIT_RETENTION=26w
    export DOCKER_INFLUXDB_INIT_ADMIN_TOKEN="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 43)"

    mkdir -p "$(dirname "$TOKEN_FILE")"
    printf '%s' "$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "first boot: generated org=$DOCKER_INFLUXDB_INIT_ORG bucket=$DOCKER_INFLUXDB_INIT_BUCKET, wrote admin token to $TOKEN_FILE"
elif [ ! -f "$TOKEN_FILE" ]; then
    # Already initialized, but the shared volume holding the token is gone
    # -- there is nothing to regenerate FROM (Influx will not hand back an
    # existing token without one to authenticate with already). eg4poll and
    # influx-init will fail to authenticate until this is fixed by hand:
    # either restore the token file, or accept losing history and reset by
    # removing both the influxdb_data and influx_shared volumes together.
    echo "WARNING: influxdb is already initialized but $TOKEN_FILE is missing -- eg4poll and influx-init cannot authenticate until it is restored or the stack is reset" >&2
fi

exec /entrypoint.sh influxd
