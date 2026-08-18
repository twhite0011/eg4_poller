# Wraps the official image so this stack's InfluxDB instance provisions
# itself -- see entrypoint.sh for what "provisions itself" means and why
# only the admin token (not the password, not org/bucket/username) is ever
# shared outward.
FROM influxdb:2
COPY entrypoint.sh /eg4poll-entrypoint.sh
RUN chmod +x /eg4poll-entrypoint.sh
ENTRYPOINT ["/eg4poll-entrypoint.sh"]
