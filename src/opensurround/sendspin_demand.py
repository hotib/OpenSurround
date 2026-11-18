"""Claim live Sendspin speakers only for the duration of source activity."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from ipaddress import ip_address

from aiohttp import web
from aiosendspin.models.types import ConnectionReason
from aiosendspin.server import SendspinServer
from zeroconf import IPVersion

from .mdns_discovery import discover_devices

_SERVICE = "_sendspin._tcp.local."
_RETRY_SECONDS = 2


@dataclass(frozen=True)
class Advertisement:
    name: str
    instance: str


def _advertisement(info):
    path = info.properties.get(b"path") or b""
    path = path.decode("utf-8")
    if (not info.port or not path.startswith("/") or
            any(character.isspace() or ord(character) < 32 for character in path)):
        return None
    instance = info.name.removesuffix("." + _SERVICE)
    name = (info.properties.get(b"name") or b"").decode("utf-8", errors="replace") or instance
    for address in map(ip_address, info.parsed_addresses(IPVersion.V4Only)):
        if not (address.is_unspecified or address.is_loopback or address.is_multicast):
            return f"ws://{address}:{info.port}{path}", Advertisement(name, instance)
    return None


class LiveSendspinServer(SendspinServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.advertisements: dict[str, Advertisement] = {}
        self._dialed: dict[str, float] = {}
        self._incoming: set[asyncio.Task] = set()
        self._playing = False

    def discover(self):
        return discover_devices(_SERVICE, self.advertisements, _advertisement,
                                ip_version=IPVersion.V4Only)

    async def on_client_connect(self, request):
        if not self._playing:
            raise web.HTTPServiceUnavailable(text="Waiting for live audio")
        task = asyncio.current_task()
        self._incoming.add(task)
        try:
            return await super().on_client_connect(request)
        finally:
            self._incoming.discard(task)

    def connect_routes(self, endpoints, now: float) -> None:
        self._playing = True
        desired = set()
        known = {url: self.get_client_id_for_url(url) for url in self.advertisements}
        connected = {self.get_client_url(player.client_id) for player in self.connected_clients}
        for endpoint in endpoints:
            if endpoint.transport != "sendspin":
                continue
            known_url = self.get_client_url(endpoint.client_id) if endpoint.client_id else None
            if known_url in self.advertisements:
                desired.add(known_url)
                continue
            candidates = {url: ad for url, ad in self.advertisements.items()
                          if not endpoint.client_id or known[url] in (None, endpoint.client_id)}
            matches = [url for url, ad in candidates.items() if ad.instance == endpoint.client_id]
            if not matches and (name := (endpoint.match_name or endpoint.label or "").casefold()):
                matches = [url for url, ad in candidates.items() if name == ad.name.casefold()]
                if not matches:
                    matches = [url for url, ad in candidates.items() if name in ad.name.casefold()]
            if len(matches) == 1:
                desired.add(matches[0])

        for url in self._dialed.keys() - desired:
            self.disconnect_from_client(url)
            del self._dialed[url]
        for url in desired - connected:
            if now >= self._dialed.get(url, 0):
                self.connect_to_client(url, connection_reason=ConnectionReason.PLAYBACK,
                                       retry_initial_connection=True)
                self._dialed[url] = now + _RETRY_SECONDS

    async def release_speakers(self) -> None:
        self._playing = False
        for url in self._dialed:
            self.disconnect_from_client(url)
        self._dialed.clear()
        for player in self.connected_clients:
            if connection := player.connection:
                await connection.disconnect(retry_connection=False)
        pending = list(self._incoming)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
