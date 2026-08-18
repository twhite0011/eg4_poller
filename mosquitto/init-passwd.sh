#!/bin/sh
# Builds /mosquitto/config/passwd, if it doesn't already exist. Runs once as
# its own short-lived service before mosquitto starts (see
# docker-compose.yml: mosquitto-init), so the hashed passwd file lives in a
# volume rather than ever being written by hand or committed to the repo.
#
# Two write-capable accounts, two different ways of getting a password:
#   poller    -- app/poller.py's own account. Never exposed to a browser --
#                purely container-to-container, on this stack's own bundled
#                broker -- so there is nothing for a human to type. Generated
#                here, shared with eg4poll only via a Docker volume (see
#                MQTT_SHARED below), same pattern as influx/entrypoint.sh
#                uses for the InfluxDB admin token.
#   settings  -- solar_settings.html's account. This one DOES get handed to
#                a browser (via /api/site), so it has to be a value a human
#                can see/rotate -- it comes from SETTINGS_MQTT_PASS in .env.
# No other accounts: reads need no password at all (mosquitto/acl's global
# anonymous-read rule covers solar_dash.html, config.html, and anything you
# wire up yourself externally).
set -eu

PW=/mosquitto/config/passwd
MQTT_SHARED=/shared
POLLER_PASS_FILE="$MQTT_SHARED/mqtt-poller-pass"

if [ -f "$PW" ]; then
    echo "passwd file already exists -- leaving it alone"
    if [ ! -f "$POLLER_PASS_FILE" ]; then
        echo "WARNING: passwd exists but $POLLER_PASS_FILE is missing -- eg4poll cannot authenticate as poller until it is restored or the stack is reset (remove the mosquitto_config and mqtt_shared volumes together)" >&2
    fi
    exit 0
fi

: "${SETTINGS_MQTT_PASS:?set SETTINGS_MQTT_PASS in .env}"

POLLER_PASS="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"

mkdir -p "$MQTT_SHARED"
printf '%s' "$POLLER_PASS" > "$POLLER_PASS_FILE"
# uid 1000 is eg4poll's own non-root user (see Dockerfile's useradd -u 1000)
# -- this file is written here as root, but read there as uid 1000, so it
# has to be handed to that uid explicitly, the same reasoning as the chown
# on $PW below (a plain chmod 600 alone would leave it root-only-readable
# and app/poller.py's _read_secret_file would just spin until its own
# timeout, silently disabling MQTT rather than erroring loudly).
chown 1000:1000 "$POLLER_PASS_FILE"
chmod 600 "$POLLER_PASS_FILE"

mosquitto_passwd -b -c "$PW" poller    "$POLLER_PASS"
mosquitto_passwd -b    "$PW" settings  "$SETTINGS_MQTT_PASS"

# mosquitto_passwd -c creates the file as whatever user ran this script
# (root, if the init container's entrypoint is overridden the way
# docker-compose.yml does). The mosquitto process itself drops to uid 1883
# (see the image's own /etc/passwd), which can't read a root-owned 600 file
# -- the broker fails at startup with "Unable to open pwfile", not an ACL
# error, so this is easy to misdiagnose as a config problem instead.
chown 1883:1883 "$PW"
chmod 600 "$PW"

echo "passwd created for 2 accounts; poller's generated password written to $POLLER_PASS_FILE"
