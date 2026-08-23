"""Config API for the web UI's Config tab. Internal-only -- nginx proxies
/api/ here (see nginx/nginx.conf); this port is never published to the host.

Deliberately always reachable, even when the current config is broken and
polling can't start: main() builds this app before it tries to build a
Runner, and keeps it running in a degraded mode if that fails (see
poller.py). A bad save must never be able to lock the user out of the one
page that would let them fix it.
"""

import asyncio
import logging
import os
from datetime import date, time

from aiohttp import web

import armlock as armlock_mod
import devconfig

LOG = logging.getLogger("eg4poll.webapp")


def _validate(data: dict) -> str | None:
    """Catch the mistakes that would otherwise crash-loop the container.
    Runner.__init__ checks names/ports too (belt and suspenders) -- this is
    what stops a bad save from reaching disk in the first place, so the
    *next* restart doesn't just fail the same way again."""
    if not isinstance(data.get("devices"), list):
        return "devices must be a list"
    names, ports = set(), set()
    for d in data["devices"]:
        name = d.get("name")
        if not name:
            return "every device needs a name"
        if name in names:
            return f"duplicate device name {name!r}"
        names.add(name)
        if d.get("type") not in devconfig.TYPE_FIELDS:
            return f"unknown device type {d.get('type')!r} (expected one of {sorted(devconfig.TYPE_FIELDS)})"
        port = d.get("port")
        if d.get("enabled", True) and port:
            if port in ports:
                return f"duplicate port {port!r} -- two devices can't share a USB adapter"
            ports.add(port)
        try:
            float(d.get("poll_interval", devconfig.DEFAULT_POLL_INTERVAL))
        except (TypeError, ValueError):
            return f"{name}: poll_interval must be a number"
    site = data.get("site") or {}
    for k in ("lat", "lon"):
        if site.get(k) is not None:
            try:
                float(site[k])
            except (TypeError, ValueError):
                return f"site.{k} must be a number"
    for d in site.get("holidays") or []:
        try:
            date.fromisoformat(d)
        except (TypeError, ValueError):
            return f"site.holidays: {d!r} is not a YYYY-MM-DD date"
    scale = data.get("scale") or {}
    for k in ("pv", "grid", "load", "batt"):
        if k in scale:
            try:
                if float(scale[k]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return f"scale.{k} must be a positive number"

    device_names = names   # every device name already validated above
    for a in data.get("automations") or []:
        aname = a.get("name") or "(unnamed automation)"
        if not a.get("device") or not a.get("key"):
            return f"automation {aname!r}: needs a device and a key"
        if a["device"] not in device_names:
            return f"automation {aname!r}: device {a['device']!r} is not a configured device"
        rules = a.get("rules")
        if not isinstance(rules, list) or not rules:
            return f"automation {aname!r}: needs at least one rule"
        for i, r in enumerate(rules):
            for tk in ("time_start", "time_end"):
                if tk not in r:
                    continue
                try:
                    hh, mm = str(r[tk]).split(":")
                    time(int(hh), int(mm))
                except (TypeError, ValueError):
                    return f"automation {aname!r} rule {i+1}: {tk} must be HH:MM"
            if r.get("days") not in ("any", "weekday", "weekend", "holiday"):
                return (f"automation {aname!r} rule {i+1}: days must be one of "
                        "any/weekday/weekend/holiday")
            if r.get("value") in (None, ""):
                return f"automation {aname!r} rule {i+1}: needs a value"
    return None


def build_app(devcfg_path: str, runner, startup_error: str | None, armlock=None) -> web.Application:
    if armlock is None:
        armlock = armlock_mod.ArmLock()
    app = web.Application()

    async def get_status(request):
        return web.json_response({
            "ok": runner is not None,
            "error": startup_error,
            "devices": [d.name for d in runner.devices] if runner else [],
        })

    async def get_discover(request):
        cfg = devconfig.load(devcfg_path)
        return web.json_response(devconfig.discover_devices(cfg["devices"]))

    async def get_config(request):
        return web.json_response(devconfig.load(devcfg_path))

    async def get_site(request):
        # Everything the three dashboard pages need to bootstrap themselves,
        # replacing what used to be a hand-maintained dashboard/config.js.
        # solar_dash.html/config.html are pure-read and need no MQTT account
        # at all (see mosquitto/acl's global anonymous-read rule), so only
        # settingsMqttUser/Pass are handed out here -- used solely by
        # solar_settings.html to authenticate its writes. That password
        # comes from this container's own environment (env_file: .env -- see
        # docker-compose.yml) rather than being duplicated into a second
        # file a human had to keep in sync. influxOrg/influxBucket are fixed
        # constants (see influx/entrypoint.sh) set as plain literals in
        # docker-compose.yml, not secrets -- the defaults here just keep
        # this endpoint correct even if that env var were ever omitted.
        # tz/scale come from the same config.yaml the Config page edits, so
        # there is exactly one place each of these is ever set. Visible to
        # anyone who loads a page either way -- this is no more exposed than
        # the static file it replaces (see mosquitto/acl's comments on why
        # the settings account's password doesn't need to be strong).
        cfg = devconfig.load(devcfg_path)
        return web.json_response({
            "settingsMqttUser": "settings",
            "settingsMqttPass": os.environ.get("SETTINGS_MQTT_PASS", ""),
            "influxOrg": os.environ.get("INFLUX_ORG", "eg4poll"),
            "influxBucket": os.environ.get("INFLUX_BUCKET", "energy"),
            "tz": cfg["site"].get("tz") or "UTC",
            "scale": cfg.get("scale") or devconfig.TEMPLATE["scale"],
        })

    async def put_config(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

        error = _validate(data)
        if error:
            return web.json_response({"ok": False, "error": error}, status=400)

        devconfig.save(devcfg_path, data)
        LOG.warning("config saved via web UI -- restarting to apply")

        async def _restart_soon():
            await asyncio.sleep(0.3)  # let this response actually reach the client first
            os._exit(0)  # restart: unless-stopped brings the container back with the new config

        asyncio.create_task(_restart_soon())
        return web.json_response({"ok": True, "message": "config saved, restarting"})

    async def get_arm(request):
        return web.json_response(armlock.status(request.query.get("session_id")))

    async def post_arm(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
        result = armlock.try_arm(data.get("session_id"))
        return web.json_response(result, status=200 if result["ok"] else 409)

    async def post_disarm(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
        result = armlock.disarm(data.get("session_id"))
        return web.json_response(result, status=200 if result["ok"] else 409)

    app.router.add_get("/api/status", get_status)
    app.router.add_get("/api/devices/discover", get_discover)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/api/site", get_site)
    app.router.add_get("/api/arm", get_arm)
    app.router.add_post("/api/arm", post_arm)
    app.router.add_post("/api/disarm", post_disarm)
    return app


async def start(app: web.Application, port: int = 8081) -> web.AppRunner:
    """Starts the app inside the CALLER's already-running event loop --
    web.run_app() is not usable here, since main() also runs the poll
    loop(s) in the same loop via asyncio.gather."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    LOG.info("config API listening on :%d", port)
    return runner
