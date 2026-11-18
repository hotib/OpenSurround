import asyncio
from types import SimpleNamespace

from aiosendspin.models.types import ConnectionReason
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore

from opensurround.config import EndpointConfig
from opensurround.sendspin_demand import Advertisement, LiveSendspinServer, _advertisement
from opensurround.web import sendspin_devices


def test_discovered_speakers_are_only_dialed_for_live_routes(monkeypatch):
    async def exercise():
        server = LiveSendspinServer(asyncio.get_running_loop(), Identity.generate(), "test",
                                    pairing_store=InMemoryServerPairingStore())
        calls = []
        monkeypatch.setattr(server, "connect_to_client", lambda url, **options: calls.append((url, options)))
        monkeypatch.setattr(server, "disconnect_from_client", calls.append)
        info = SimpleNamespace(
            properties={b"path": b"/sendspin"}, port=8928, name="mediaplayer._sendspin._tcp.local.",
            parsed_addresses=lambda _: ["192.0.2.10"],
        )
        url, ad = _advertisement(info)
        assert ad == Advertisement("mediaplayer", "mediaplayer")
        other = "ws://192.0.2.11:80/sendspin"
        server.advertisements = {url: ad, other: Advertisement("Other player", "other-id")}
        assert all(device["discovered"] and not device["connected"] for device in sendspin_devices(server))
        assert calls == []

        front = EndpointConfig("front", "mediaplayer", (0, 1), client_id="legacy-id", label="Front RL")
        server.connect_routes([front], now=10)
        server.connect_routes([front], now=11)
        options = dict(connection_reason=ConnectionReason.PLAYBACK, retry_initial_connection=True)
        assert calls == [(url, options)]
        await server.release_speakers()
        assert calls == [(url, options), url]

        center = EndpointConfig("center", "", (2,), client_id="other-id")
        server.connect_routes([center], now=14)
        assert calls[-1] == (other, options)
        await server.release_speakers()
        assert calls[-1] == other

    asyncio.run(exercise())
