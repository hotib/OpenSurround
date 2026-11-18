"""Local AirPlay receiver discovery, independent of the FIFO playback loop."""
from __future__ import annotations

from ipaddress import ip_address

import ifaddr

from .airplay import AirPlayDevice
from .mdns_discovery import discover_devices

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


def discover_airplay(devices: dict[str, AirPlayDevice]):
    local_ips = _local_ips()

    def decode(info):
        if device := _device(info, local_ips):
            return device.id, device

    return discover_devices(_SERVICE, devices, decode)
