"""Minimal async InfluxDB 2.x line-protocol writer.

Replaces the node-red-contrib-influxdb "influxdb out"/"influxdb batch" nodes
that used to do this. Hand-rolled rather than pulling in the full
influxdb-client SDK -- matches this project's existing minimal-dependency
style (requirements.txt has always been small and deliberate), and a
line-protocol POST is genuinely simple: this whole thing is one function.
"""

import time
from datetime import datetime
from typing import Any

import aiohttp

LOG_PREFIX = "influx_write"


def _escape_tag(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def _escape_measurement(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _escape_field_key(s: str) -> str:
    return _escape_tag(s)


def _field_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return repr(v)
    # String, or anything else -- quote and escape.
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _timestamp_ns(ts) -> int:
    """Accepts a unix-seconds float or a datetime; returns nanoseconds."""
    if isinstance(ts, datetime):
        ts = ts.timestamp()
    return int(round(ts * 1_000_000_000))


def to_line(measurement: str, tags: dict, fields: dict, timestamp) -> str | None:
    """One point -> one line-protocol line. None if there are no fields --
    a point with no fields is not writable, and this can legitimately
    happen (e.g. a poll where nothing decoded)."""
    if not fields:
        return None
    tag_str = "".join(f",{_escape_tag(k)}={_escape_tag(v)}" for k, v in tags.items() if v not in (None, ""))
    field_str = ",".join(f"{_escape_field_key(k)}={_field_value(v)}" for k, v in fields.items() if v is not None)
    if not field_str:
        return None
    return f"{_escape_measurement(measurement)}{tag_str} {field_str} {_timestamp_ns(timestamp)}"


async def write_points(session: aiohttp.ClientSession, url: str, token: str,
                        org: str, bucket: str, points: list[dict]) -> None:
    """points: list of {"measurement", "tags", "fields", "timestamp"}.

    Raises on HTTP failure -- callers decide whether that's fatal (it isn't,
    for this project: a failed Influx write should not stop the poll/publish
    cycle, only be logged, since MQTT is the live path and Influx is history).
    """
    lines = [ln for p in points if (ln := to_line(p["measurement"], p.get("tags", {}), p["fields"], p["timestamp"]))]
    if not lines:
        return
    body = "\n".join(lines)
    write_url = f"{url}/api/v2/write"
    params = {"org": org, "bucket": bucket, "precision": "ns"}
    headers = {"Authorization": f"Token {token}", "Content-Type": "text/plain; charset=utf-8"}
    async with session.post(write_url, params=params, headers=headers, data=body,
                             timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status >= 300:
            text = await resp.text()
            raise RuntimeError(f"influx write {resp.status}: {text[:200]}")
