"""Optional HTTP settings, kept on the same event loop as playback."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hmac
from ipaddress import ip_address
import logging
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from aiohttp import web

from .config import Config
from .sendspin_demand import LiveSendspinServer
from .web_store import NoPreviousRevision, RevisionConflict, RouteStore

_LOG = logging.getLogger("opensurround.web")
_ASSETS = Path(__file__).with_name("web_static")
_INPUT_LAYOUTS = {
    2: ("2.0", ("FL", "FR")),
    6: ("5.1", ("FL", "FR", "FC", "LFE", "SL", "SR")),
    8: ("7.1", ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR")),
}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _read_token(path: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("the web token file must be a private regular file owned by the process user (chmod 600)")
        token = file.read(1025).strip()
        if not re.fullmatch(rb"[A-Za-z0-9._~-]{32,256}", token):
            raise ValueError("the web token must contain 32–256 URL-safe ASCII characters")
        return token


def sendspin_devices(server) -> list[dict]:
    """Inventory adapter for the transport-agnostic settings response."""
    devices = {
        client.client_id: {"id": client.client_id, "name": client.name or client.client_id,
                           "transport": "sendspin", "connected": client.is_connected}
        for client in server.clients
        if client.info_or_none is not None and client.info_or_none.player_support is not None
    }
    if isinstance(server, LiveSendspinServer):
        for url, ad in server.advertisements.items():
            identifier = server.get_client_id_for_url(url)
            device_id = identifier or f"mdns:{url}"
            device = devices.setdefault(device_id, {
                "id": device_id, "name": ad.name, "transport": "sendspin",
                "connected": False, "discovery_only": identifier is None,
            })
            device["discovered"] = True
    return list(devices.values())


class WebControl:
    def __init__(self, config_path: str, host: str, port: int, token_file: str | None):
        self.config_path = config_path
        self.host = host
        self.port = port
        self.local_only = _is_loopback(host)
        if host != "localhost":
            ip_address(host)
        if not self.local_only and token_file is None:
            raise ValueError("--web-token-file is required for a non-loopback --web-host")
        self.token = _read_token(token_file) if token_file is not None else None
        self.saved = None
        self.active_endpoints = None

    def at_boundary(self, config: Config) -> Config:
        """Adopt only the routing settings, leaving the running input and server in place."""
        endpoints = self.saved.config.endpoints
        self.active_endpoints = endpoints
        return replace(config, endpoints=endpoints) if endpoints != config.endpoints else config

    @asynccontextmanager
    async def serve(self, server, channels: int, *, airplay=None, sample_rate: int | None = None):
        store = RouteStore(self.config_path, channels)
        self.saved = await asyncio.to_thread(store.read)
        app = self.make_app(store, lambda: sendspin_devices(server), airplay=airplay,
                            sample_rate=sample_rate)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
            _LOG.info("Web settings listening on %s:%s", self.host, self.port)
            yield
        finally:
            await runner.cleanup()

    def make_app(self, store: RouteStore, devices, *, airplay=None,
                 sample_rate: int | None = None) -> web.Application:
        lock = asyncio.Lock()
        pairing = asyncio.Lock()

        @web.middleware
        async def protect(request, handler):
            origin = request.headers.get("Origin")
            try:
                hostname = urlsplit(f"//{request.host}").hostname or ""
            except ValueError:
                hostname = ""
            if self.local_only and not _is_loopback(hostname):
                response = web.json_response({"error": "Érvénytelen helyi cím."}, status=403)
            elif origin is not None and origin != f"{request.scheme}://{request.host}":
                response = web.json_response({"error": "Érvénytelen kérésforrás."}, status=403)
            elif request.path.startswith("/api/"):
                supplied = request.headers.get("Authorization", "").encode("utf-8", errors="replace")
                if request.headers.get("X-OpenSurround") != "1":
                    response = web.json_response({"error": "Érvénytelen kérés."}, status=403)
                elif self.token is not None and not hmac.compare_digest(supplied, b"Bearer " + self.token):
                    response = web.json_response({"error": "A belépéshez érvényes token kell."}, status=401)
                else:
                    response = await handler(request)
            else:
                response = await handler(request)
            response.headers.update(_SECURITY_HEADERS)
            return response

        def state(snapshot):
            layout, channels = _INPUT_LAYOUTS.get(store.channels, (None, ()))
            if not channels:
                channels = tuple(str(i + 1) for i in range(store.channels))
            inventory = devices()
            discovered = airplay.devices if airplay is not None else {}
            known = {ep.client_id for ep in snapshot.config.endpoints if ep.transport == "airplay"}
            for device_id in sorted(known | discovered.keys()):
                device = discovered.get(device_id)
                status = airplay.status(device_id) if airplay is not None else "offline"
                inventory.append({
                    "id": device_id, "name": device.name if device else device_id,
                    "transport": "airplay", "connected": device is not None,
                    "status": status, "error": "AirPlay-kimeneti hiba." if status == "error" else None,
                })
            return web.json_response({
                "revision": snapshot.id,
                "can_restore": snapshot.can_restore,
                "pending": self.active_endpoints != snapshot.config.endpoints,
                "channels": channels,
                "input_layout": layout,
                "sample_rate": sample_rate,
                "devices": inventory,
                "endpoints": [
                    {"key": ep.name, "label": ep.label if ep.label is not None else ep.name,
                     "match_name": ep.match_name, "client_id": ep.client_id,
                     "source_channels": ep.source_channels, "gain_db": ep.gain_db,
                     "delay_ms": ep.delay_ms, "muted": ep.muted, "transport": ep.transport,
                     "airplay_mode": ep.airplay_mode}
                    for ep in snapshot.config.endpoints
                ],
            })

        async def get_state(request):
            async with lock:
                try:
                    return state(await asyncio.to_thread(store.read))
                except (ValueError, KeyError, TypeError, OSError):
                    return web.json_response({"error": "A konfiguráció nem olvasható."}, status=500)

        async def edit(request):
            if request.content_type != "application/json":
                return web.json_response({"error": "JSON kérés szükséges."}, status=415)
            try:
                payload = await request.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("revision"), str):
                    return web.json_response({"error": "A mentett verzió azonosítója szükséges."}, status=428)
            except (ValueError, UnicodeError):
                return web.json_response({"error": "Érvénytelen kérés."}, status=400)
            async with lock:
                try:
                    if request.method == "PUT":
                        snapshot = await asyncio.to_thread(store.save, payload["revision"], payload.get("endpoints"))
                    else:
                        snapshot = await asyncio.to_thread(store.restore, payload["revision"])
                except RevisionConflict:
                    return web.json_response({"error": "A mentett beállítások megváltoztak. Töltsd be a friss verziót."},
                                             status=409)
                except NoPreviousRevision:
                    return web.json_response({"error": "Nincs előző mentés."}, status=409)
                except (ValueError, OverflowError) as exc:
                    return web.json_response({"error": str(exc)}, status=400)
                except (KeyError, TypeError, OSError):
                    return web.json_response({"error": "A konfiguráció nem írható."}, status=500)
                self.saved = snapshot
                return state(snapshot)

        async def pair(request):
            if request.content_type != "application/json":
                return web.json_response({"error": "JSON kérés szükséges."}, status=415)
            finish = request.match_info["action"] == "finish"
            try:
                payload = await request.json()
                device_id = payload.get("id")
                pin = payload.get("pin")
                if not isinstance(device_id, str) or (finish and (
                        not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,8}", pin))):
                    raise ValueError
            except (AttributeError, ValueError):
                return web.json_response({"error": "Érvénytelen eszközazonosító vagy PIN."}, status=400)
            snapshot = self.saved
            if snapshot is None or not any(ep.transport == "airplay" and ep.client_id == device_id
                                           for ep in snapshot.config.endpoints):
                return web.json_response({"error": "Előbb mentsd az eszköz útvonalát."}, status=404)
            device = airplay.devices.get(device_id) if airplay is not None else None
            if device is None:
                return web.json_response({"error": "Az AirPlay-eszköz nem érhető el."}, status=503)
            if pairing.locked():
                return web.json_response({"error": "Egy párosítási kérés már fut."}, status=409)
            async with pairing:
                try:
                    async with asyncio.timeout(8):
                        if finish:
                            await airplay.outputs.pair_finish(device, pin)
                        else:
                            await airplay.outputs.pair_begin(device)
                except TimeoutError:
                    return web.json_response({"error": "A párosítási kérés időtúllépés miatt megszakadt."}, status=504)
                except (ValueError, OSError, RuntimeError):
                    return web.json_response({"error": "Az AirPlay-párosítás sikertelen."}, status=502)
            return web.json_response({"ok": True})

        async def index(request):
            return web.FileResponse(_ASSETS / "index.html")

        async def script(request):
            return web.FileResponse(_ASSETS / "app.js")

        async def stylesheet(request):
            return web.FileResponse(_ASSETS / "styles.css")

        app = web.Application(middlewares=[protect], client_max_size=256 * 1024)
        app.router.add_get("/", index)
        app.router.add_get("/app.js", script)
        app.router.add_get("/styles.css", stylesheet)
        app.router.add_get("/api/state", get_state)
        app.router.add_put("/api/routes", edit)
        app.router.add_post("/api/restore", edit)
        app.router.add_post(r"/api/airplay/pair/{action:begin|finish}", pair)
        return app
