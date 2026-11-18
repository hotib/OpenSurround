"""Resolve LAN advertisements without opening a playback connection."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf


@asynccontextmanager
async def discover_devices(service_type, devices, decode, *, ip_version=IPVersion.All):
    zeroconf = AsyncZeroconf(ip_version=ip_version)
    records = {}
    pending = {}

    def publish():
        devices.clear()
        devices.update(records[name] for name in sorted(records))

    async def resolve(zc, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            if info.load_from_cache(zc) or await info.async_request(zc, 1500):
                if item := decode(info):
                    records[name] = item
                else:
                    records.pop(name, None)
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
            records.pop(name, None)
            publish()
        else:
            pending[name] = asyncio.create_task(resolve(zeroconf, name))

    try:
        browser = AsyncServiceBrowser(zeroconf.zeroconf, service_type, handlers=[changed])
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
