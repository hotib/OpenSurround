"""Local AirPlay receiver discovery, independent of the FIFO playback loop."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from ipaddress import ip_address

import ifaddr
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .airplay import AirPlayDevice

_SERVICE = "_airplay._tcp.local."


def _local_ips() -> set[str]:
    return {
        str(ip_address((ip.ip if isinstance(ip.ip, str) else ip.ip[0]).split("%", 1)[0]))
        for adapter in ifaddr.get_adapters() for ip in adapter.ips
    }


def _device(info, local_ips: set[str]) -> AirPlayDevice | None:
    txt = {key.decode("utf-8", "replace"): value.decode("utf-8", "replace") if value is not None else ""
           for key, value in info.properties.items()}
    device_id = next((value.strip().upper() for key, value in txt.items()
                      if key.casefold() == "deviceid"), "")
    addresses = info.parsed_scoped_addresses()
    if not device_id or not info.port or not addresses:
        return None
    if any(str(ip_address(address.split("%", 1)[0])) in local_ips for address in addresses):
        return None
    # IPv4 first on a dual-stack LAN. Scoped link-local IPv6 interface indices
    # are local to the router and cannot be passed into the agent's namespace.
    addresses = [address for address in addresses if not (":" in address and ip_address(address).is_link_local)]
    if not addresses:
        return None
    address = min(addresses, key=lambda value: (":" in value, value))
    return AirPlayDevice(device_id, info.name.removesuffix("." + _SERVICE), address, info.port, txt)


@asynccontextmanager
async def discover_airplay(devices: dict[str, AirPlayDevice]):
    local_ips = _local_ips()
    zeroconf = AsyncZeroconf(ip_version=IPVersion.All)
    services: dict[str, AirPlayDevice] = {}
    pending: dict[str, asyncio.Task] = {}

    def publish():
        devices.clear()
        devices.update((device.id, device) for _, device in sorted(services.items()))

    async def resolve(zc, name):
        try:
            info = AsyncServiceInfo(_SERVICE, name)
            if await info.async_request(zc, 1500):
                device = _device(info, local_ips)
                if device is not None:
                    services[name] = device
                else:
                    services.pop(name, None)
                publish()
        except (OSError, ValueError):
            pass
        finally:
            if pending.get(name) is asyncio.current_task():
                pending.pop(name)

    def changed(zeroconf, service_type, name, state_change):
        if name in pending:
            pending.pop(name).cancel()
        if state_change is ServiceStateChange.Removed:
            services.pop(name, None)
            publish()
        else:
            pending[name] = asyncio.create_task(resolve(zeroconf, name))

    try:
        browser = AsyncServiceBrowser(zeroconf.zeroconf, _SERVICE, handlers=[changed])
        try:
            yield
        finally:
            await browser.async_cancel()
            tasks = list(pending.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            devices.clear()
    finally:
        await zeroconf.async_close()
