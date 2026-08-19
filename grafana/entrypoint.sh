#!/bin/sh
# Wraps the official grafana entrypoint so this container's InfluxDB
# datasource is self-provisioning -- nobody types a token into the UI.
# influx-init mints a bucket-scoped READ-ONLY token for Grafana the same
# way it already does for nginx's dashboard queries (see
# influx/init-read-token.sh) and shares it only via a Docker volume. This
# script waits for that file, exports it as an env var, and lets
# provisioning/datasources/influxdb.yaml pick it up via ${GF_INFLUX_TOKEN}
# -- Grafana's provisioning files support env var expansion natively, so
# there is nothing to template by hand.
set -eu

TOKEN_FILE=/influx-shared/grafana-token

i=0
until [ -f "$TOKEN_FILE" ]; do
    i=$((i + 1))
    [ "$i" -ge 30 ] && { echo "$TOKEN_FILE never appeared -- did influx-init fail?" >&2; exit 1; }
    sleep 2
done
GF_INFLUX_TOKEN="$(cat "$TOKEN_FILE")"
export GF_INFLUX_TOKEN

exec /run.sh
