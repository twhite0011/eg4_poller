"""Solar forecast -- Open-Meteo GTI -> expected PV watts.

Ported from nodered/flows.json's "04 forecast fetch" + "05 forecast model"
(removed, along with the rest of Node-RED). One thing was fixed, not
preserved, during the port:

BUG FIXED: Open-Meteo returns hourly timestamps as local wall-clock strings
for the requested timezone ("2026-08-17T14:00", no UTC offset) when a
`timezone` param is passed. The original JS parsed these with `new
Date(str)` -- and a date-time string with no timezone designator is parsed
by JS as being in the RUNTIME's OWN system timezone, not the site's.
Verified directly: `new Date("2026-08-17T14:00")` on a machine whose own
system TZ is America/Los_Angeles parses to 21:00 UTC (correct, but only
because the two timezones happened to match); on a machine set to UTC (the
default for basically every Docker base image, and nothing in this project
ever set the container's TZ) the same string parses to 14:00 UTC -- silently
wrong by however many hours the site's timezone differs from the
container's. This touched "today"/"tomorrow" bucketing, "remaining today",
and the current/next-hour lookup used for the dashboard's headline numbers.
Fixed here by parsing every Open-Meteo timestamp explicitly against the
site's own tz via zoneinfo, independent of whatever timezone the container
itself happens to be running in.
"""

import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

# Panel geometry -- AS INSTALLED, not the pvlib-optimal values. Not part of
# the web-managed config (unlike site coordinates): a physical fact about
# the array, changed only if the array itself is reconfigured.
#   (60 deg was the modelled best fixed tilt for the 4-9pm peak window; the
#   ground mount is actually at 20. At 20 deg the plane is nearly
#   horizontal, so azimuth has little influence and output peaks close to
#   solar noon rather than late afternoon.)
ARRAY = {
    "stc_w": 800,              # 8 x ECO-WORTHY 100 W in series
    "tilt_deg": 20,
    "azimuth_north_deg": 270,  # 270 = due west, north-referenced
}

HOURS = 48

# NOCT cell-temperature derate model:
#   T_cell = T_air + (GTI / 800) * (NOCT - 20)
#   P = P_stc * (GTI / 1000) * (1 + GAMMA * (T_cell - 25)) * EFF
GAMMA = -0.0038   # module temp coefficient of Pmax, per deg C (typical mono/poly)
NOCT = 45         # nominal operating cell temperature, deg C
# System efficiency -- the one number to tune. Absorbs MPPT efficiency,
# wiring/connector losses, soiling, module mismatch, and any shading the
# horizon model misses. Fit against CLEAR days only, comparing against
# pv_total_w_corrected (derive.py) -- never the inverter's own *_today_kwh,
# which overstates by ~20% and would bias this up by the same amount. A
# residual that varies by TIME OF DAY rather than by amount means the
# tilt/azimuth don't match the real installation, not an eff problem --
# fix the geometry before touching this.
EFF = 0.88
GTI_FLOOR = 15  # below this the model is noise: low sun, MPPT not started


def _r(x, n):
    # float() before round(): round() on a plain int input returns an int,
    # not a rounded float -- and these values come from Open-Meteo's raw
    # JSON, where a whole-number reading (very common: cloud cover, night-
    # time irradiance) parses as a Python int. A field that's an int this
    # hour and a float the next is exactly what trips InfluxDB's per-field
    # type lock (see remaining_ah/temperatures_c's identical bug in
    # app/eg4ll.py).
    return None if x is None else round(float(x), n)


def _f(x):
    """Same float-safety as _r(), for fields that pass straight through
    from Open-Meteo with no rounding of their own (ghi/dni/dhi/cloud/t_air)."""
    return None if x is None else float(x)


async def fetch_raw(session: aiohttp.ClientSession, lat: float, lon: float, tz: str) -> dict:
    """Raw Open-Meteo response. Raises on network/HTTP failure."""
    # AZIMUTH CONVENTION -- the usual trap. Open-Meteo is SOUTH-referenced
    # (0=south, -90=east, +90=west), not the north-referenced 0-360 used by
    # pvlib, HA, and most compass apps.
    om_az = ARRAY["azimuth_north_deg"] - 180
    params = {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "forecast_days": math.ceil(HOURS / 24) + 1,
        "tilt": ARRAY["tilt_deg"], "azimuth": om_az,
        "hourly": ",".join([
            "global_tilted_irradiance",   # plane-of-array, what drives output
            "shortwave_radiation",        # horizontal GHI, for sanity checking
            "direct_normal_irradiance",
            "diffuse_radiation",
            "cloud_cover",
            "temperature_2m",             # for the cell-temperature derate
            "wind_speed_10m",             # panels run cooler in wind
        ]),
    }
    async with session.get(
        "https://api.open-meteo.com/v1/forecast", params=params,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


def model(raw: dict, tz: str) -> dict:
    """GTI -> expected watts, plus forecast-vs-actual bookkeeping.

    Returns {"fields", "hourly_points"}: fields is the current-conditions
    point (measurement "forecast") and the energy/derived/forecast MQTT
    payload; hourly_points is the full per-hour series (measurement
    "forecast_hourly"), one row per hour including future ones, so the
    dashboard's chart survives a restart instead of depending on a retained
    MQTT payload alone.
    """
    zone = ZoneInfo(tz)
    hourly = raw.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        raise ValueError("Open-Meteo returned no hourly block")

    # GTI is a BACKWARD average over the preceding hour, assuming a fixed
    # 20% albedo and an isotropic sky -- the crude transposition model; at a
    # steep tilt and low sun it will differ from Perez by a few percent.
    series = []
    for i, t in enumerate(times):
        gti = (hourly.get("global_tilted_irradiance") or [None] * len(times))[i]
        if gti is None:
            continue
        # Open-Meteo's local-time string, explicitly parsed as the SITE's
        # timezone -- not the container's. See module docstring.
        local_dt = datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=zone)
        t_air = (hourly.get("temperature_2m") or [None] * len(times))[i]

        w, t_cell = 0.0, t_air
        if gti >= GTI_FLOOR:
            t_cell = t_air + (gti / 800) * (NOCT - 20) if t_air is not None else None
            if t_cell is not None:
                w = ARRAY["stc_w"] * (gti / 1000) * (1 + GAMMA * (t_cell - 25)) * EFF
                # The inverter cannot pass more than the array can make;
                # also guards against a bad GTI value producing nonsense.
                w = max(0.0, min(w, ARRAY["stc_w"]))

        series.append({
            "dt": local_dt,
            "day": local_dt.date(),
            "gti": _r(gti, 1),
            "ghi": _f((hourly.get("shortwave_radiation") or [None] * len(times))[i]),
            "dni": _f((hourly.get("direct_normal_irradiance") or [None] * len(times))[i]),
            "dhi": _f((hourly.get("diffuse_radiation") or [None] * len(times))[i]),
            "cloud": _f((hourly.get("cloud_cover") or [None] * len(times))[i]),
            "t_air": _f(t_air),
            "t_cell": _r(t_cell, 1) if t_cell is not None else None,
            "w": _r(w, 1),
        })

    now = datetime.now(zone)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    # Daily totals. GTI-derived watts is a backward-hour average, so
    # watts x 1h = Wh directly -- no integration needed.
    days_kwh: dict = {}
    for p in series:
        days_kwh[p["day"]] = days_kwh.get(p["day"], 0.0) + p["w"] / 1000

    # Current and next-hour values for the live dashboard. Open-Meteo's
    # 14:00 row is the average over 13:00-14:00, so "now" falls in the row
    # whose timestamp is the END of the current hour.
    cur = nxt = None
    for i, p in enumerate(series):
        if p["dt"] <= now and (i + 1 >= len(series) or series[i + 1]["dt"] > now):
            cur = p
            nxt = series[i + 1] if i + 1 < len(series) else None
            break

    fields = {
        # 0.0, not 0 -- these are float fields once a current/next point
        # exists; falling back to a bare int here on the (rare) edge case
        # of no matching hour would be the same type flip _f()/_r() above
        # exist to prevent, just triggered by absence instead of a zero
        # reading.
        "forecast_w": cur["w"] if cur else 0.0,
        "forecast_w_next": nxt["w"] if nxt else 0.0,
        "gti": cur["gti"] if cur else 0.0,
        "ghi": cur["ghi"] if cur else 0.0,
        "cloud_cover": cur["cloud"] if cur else None,
        "t_air": cur["t_air"] if cur else None,
        "t_cell": cur["t_cell"] if cur else None,
        "forecast_today_kwh": _r(days_kwh.get(today, 0.0), 3),
        "hours": len(series),
    }

    # Remaining generation today -- the number that actually drives a
    # decision about whether to grid-charge before the peak window.
    remain = sum(p["w"] / 1000 for p in series if p["day"] == today and p["dt"] >= now)
    fields["forecast_remaining_kwh"] = _r(remain, 3)
    fields["forecast_tomorrow_kwh"] = _r(days_kwh.get(tomorrow, 0.0), 3)

    hourly_points = [{
        "measurement": "forecast_hourly",
        "timestamp": p["dt"],
        "tags": {"role": "forecast", "source": "open-meteo"},
        "fields": {
            # `or 0` would substitute a bare int 0 whenever the real value is
            # exactly 0.0 (falsy) -- cloud cover and nighttime irradiance are
            # both routinely exactly zero, so that pattern reintroduces the
            # same int/float type flip _f()/_r() above exist to prevent.
            "w": p["w"], "gti": p["gti"],
            "ghi": p["ghi"] if p["ghi"] is not None else 0.0,
            "cloud": p["cloud"] if p["cloud"] is not None else 0.0,
            "t_air": p["t_air"] if p["t_air"] is not None else 0.0,
            "t_cell": p["t_cell"] if p["t_cell"] is not None else 0.0,
        },
    } for p in series]

    return {
        "fields": fields,
        "tick_ts": now.timestamp(),
        "hourly_points": hourly_points,
        # For the retained energy/derived/forecast MQTT payload -- unlike
        # live state topics, this one IS retained: it publishes only once an
        # hour, so a page loaded in between would otherwise see nothing, and
        # a slightly-old hourly forecast is still correct (unlike a stale
        # power reading, which would be a lie).
        "series_for_mqtt": [
            {"t": p["dt"].strftime("%Y-%m-%dT%H:%M"), "w": p["w"], "cloud": p["cloud"]}
            for p in series[:48]
        ],
    }
