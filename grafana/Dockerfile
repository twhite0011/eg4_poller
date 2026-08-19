# Wraps the official image so this stack's Grafana instance provisions its
# own InfluxDB datasource and starter dashboard -- see entrypoint.sh for how
# the read-only token gets in, never typed by hand.
FROM grafana/grafana-oss:11.3.0

COPY --chown=grafana:grafana provisioning /etc/grafana/provisioning

USER root
COPY entrypoint.sh /eg4poll-entrypoint.sh
RUN chmod +x /eg4poll-entrypoint.sh
USER grafana

ENTRYPOINT ["/eg4poll-entrypoint.sh"]
