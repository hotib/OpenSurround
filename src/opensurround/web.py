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
from .web_store import NoPreviousRevision, RevisionConflict, RouteStore

_LOG = logging.getLogger("opensurround.web")
_ASSETS = Path(__file__).with_name("web_static")
_CHANNELS_51 = ("FL", "FR", "FC", "LFE", "SL", "SR")
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
    return [
        {"id": client.client_id, "name": client.name or client.client_id, "transport": "sendspin",
         "connected": client.is_connected}
        for client in server.clients
        if client.info_or_none is not None and client.info_or_none.player_support is not None
    ]


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
    async def serve(self, server, channels: int):
        store = RouteStore(self.config_path, channels)
        self.saved = await asyncio.to_thread(store.read)
        app = self.make_app(store, lambda: sendspin_devices(server))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
            _LOG.info("Web settings listening on %s:%s", self.host, self.port)
            yield
        finally:
            await runner.cleanup()

    def make_app(self, store: RouteStore, devices) -> web.Application:
        lock = asyncio.Lock()

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
            channels = _CHANNELS_51 if store.channels == 6 else tuple(str(i + 1) for i in range(store.channels))
            return web.json_response({
                "revision": snapshot.id,
                "can_restore": snapshot.can_restore,
                "pending": self.active_endpoints != snapshot.config.endpoints,
                "channels": channels,
                "devices": devices(),
                "endpoints": [
                    {"key": ep.name, "label": ep.label if ep.label is not None else ep.name,
                     "match_name": ep.match_name, "client_id": ep.client_id,
                     "source_channels": ep.source_channels, "gain_db": ep.gain_db,
                     "delay_ms": ep.delay_ms, "muted": ep.muted}
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
        return app
