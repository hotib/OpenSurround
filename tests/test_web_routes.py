import asyncio
import os
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from opensurround.web import WebControl
from opensurround.web_store import RouteStore


def test_existing_profile_edit_and_restore(tmp_path):
    profile = tmp_path / "profile.toml"
    original = Path("configs/surround-live.toml").read_bytes()
    profile.write_bytes(original)
    token_file = tmp_path / "token"
    token_file.write_text("a" * 32)
    os.chmod(token_file, 0o600)

    async def exercise():
        store = RouteStore(profile, 6)
        control = WebControl(str(profile), "127.0.0.1", 8080, str(token_file))
        control.saved = store.read()
        config = control.at_boundary(control.saved.config)
        devices = lambda: [{"id": "test-player", "name": "Teszt", "transport": "sendspin",
                            "connected": True}]
        async with TestClient(TestServer(control.make_app(store, devices))) as client:
            headers = {"X-OpenSurround": "1", "Authorization": "Bearer " + "a" * 32}
            response = await client.get("/api/state", headers={"X-OpenSurround": "1"})
            assert response.status == 401
            response = await client.get("/api/state", headers=headers)
            assert response.status == 200
            state = await response.json()
            assert state["channels"] == ["FL", "FR", "FC", "LFE", "SL", "SR"]
            assert state["endpoints"][0]["source_channels"] == [0, 1]
            assert not state["pending"]
            assert profile.read_bytes() == original

            row = state["endpoints"][0]
            row.update(source_channels=[7], muted=True)
            edit = {"revision": state["revision"], "endpoints": [row]}
            response = await client.put("/api/routes", headers=headers, json=edit)
            assert response.status == 400
            assert profile.read_bytes() == original

            row.update(source_channels=[2], client_id="test-player")
            response = await client.put("/api/routes", headers=headers, json=edit)
            assert response.status == 200
            saved = await response.json()
            assert saved["pending"] and saved["can_restore"]
            assert config.endpoints[0].source_channels == (0, 1)
            applied = control.at_boundary(config)
            assert applied.endpoints[0].source_channels == (2,)
            assert applied.endpoints[0].muted
            assert applied.port == 8928 and applied.buffer_lead_ms == 300

            response = await client.put("/api/routes", headers=headers, json=edit)
            assert response.status == 409
            response = await client.post("/api/restore", headers=headers,
                                         json={"revision": saved["revision"]})
            assert response.status == 200
            restored = await response.json()
            assert restored["endpoints"][0]["source_channels"] == [0, 1]
            assert control.at_boundary(applied).endpoints == config.endpoints

    asyncio.run(exercise())


def test_eight_channel_web_input_layout(tmp_path):
    profile = tmp_path / "profile.toml"
    profile.write_bytes(Path("configs/surround-live.toml").read_bytes())

    async def exercise():
        store = RouteStore(profile, 8)
        control = WebControl(str(profile), "127.0.0.1", 8080, None)
        control.saved = store.read()
        app = control.make_app(store, lambda: [], sample_rate=48000)
        async with TestClient(TestServer(app)) as client:
            headers = {"X-OpenSurround": "1"}
            response = await client.get("/api/state", headers=headers)
            state = await response.json()
            assert state["channels"] == ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"]
            assert state["input_layout"] == "7.1" and state["sample_rate"] == 48000
            state["endpoints"][0]["source_channels"] = [6, 7]
            response = await client.put("/api/routes", headers=headers, json=state)
            assert response.status == 200
            assert store.read().config.endpoints[0].source_channels == (6, 7)

    asyncio.run(exercise())


def test_airplay_routes_and_pairing(tmp_path):
    import json
    from types import SimpleNamespace

    from aiohttp.test_utils import make_mocked_request

    from opensurround.server import LiveAirPlay

    profile = tmp_path / "profile.toml"
    profile.write_text("")
    calls = []
    device = SimpleNamespace(id="AA:BB:CC:DD:EE:FF", name="Living Room")

    class Outputs:
        def status(self, device_id):
            return "ready"

        async def pair_begin(self, selected):
            calls.append(("begin", selected.id))

        async def pair_finish(self, selected, pin):
            calls.append(("finish", selected.id, pin))

    async def exercise():
        store = RouteStore(profile, 2)
        control = WebControl(str(profile), "127.0.0.1", 8080, None)
        control.saved = store.read()
        airplay = LiveAirPlay(Outputs(), {device.id: device})
        sendspin = {"id": "spin", "name": "Sendspin", "transport": "sendspin", "connected": True}
        app = control.make_app(store, lambda: [sendspin], airplay=airplay)
        app.freeze()

        async def request(method, path, body=None, *, authenticated=True):
            headers = {"X-OpenSurround": "1"} if authenticated else {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            req = make_mocked_request(method, path, headers=headers, app=app)
            if body is not None:
                req._read_bytes = json.dumps(body).encode()
            return await app._handle(req)

        response = await request("GET", "/api/state")
        state = json.loads(response.body)
        assert [entry["transport"] for entry in state["devices"]] == ["sendspin", "airplay"]
        begin = "/api/airplay/pair/begin"
        target = {"id": device.id}
        assert (await request("POST", begin, target, authenticated=False)).status == 403
        assert (await request("POST", begin, target)).status == 404
        route = dict(label="Living Room", transport="airplay", client_id=device.id,
                     airplay_mode="compat", source_channels=[1], gain_db=0, delay_ms=0, muted=False)

        async def save(routes):
            return await request("PUT", "/api/routes", {"revision": state["revision"], "endpoints": routes})

        assert (await save([route, route])).status == 400
        response = await save([route])
        assert response.status == 200
        assert json.loads(response.body)["pending"]
        applied = control.at_boundary(control.saved.config)
        assert applied.endpoints[0].airplay_mode == "compat"
        assert (await request("POST", begin, target)).status == 200
        assert (await request("POST", "/api/airplay/pair/finish", {**target, "pin": "0123"})).status == 200
        airplay.devices.clear()
        offline = json.loads((await request("GET", "/api/state")).body)
        assert offline["devices"][1]["status"] == "offline"
        assert offline["devices"][0] == sendspin
        assert (await request("POST", begin, target)).status == 503

    asyncio.run(exercise())
    assert calls == [("begin", device.id), ("finish", device.id, "0123")]


def test_live_airplay_only_and_mixed_sample_clock(monkeypatch):
    from types import SimpleNamespace

    from opensurround import server
    from opensurround.config import parse_config
    from opensurround.pipe_source import PCMChunk

    async def exercise(mixed):
        unix_us = 1_700_000_000_000_000
        clock_us = 10_000_000
        monkeypatch.setattr(server, "time_ns", lambda: unix_us * 1000)
        config = parse_config("""
[audio]
buffer_lead_ms = 300
[endpoints.air]
transport = "airplay"
client_id = "air"
source_channels = [1]
""" + ("""
[endpoints.spin]
client_id = "spin"
source_channels = [0]
""" if mixed else ""))
        prepared, offers, stopped, committed = [], [], [], []
        device = SimpleNamespace(id="air", name="AirPlay")
        loop = asyncio.get_running_loop()

        class Outputs:
            def prepare(self, selected, **kwargs):
                prepared.append((selected.id, kwargs))

            def offer(self, selected, **kwargs):
                offers.append(kwargs)
                if mixed and len(offers) == 1:
                    raise OSError("agent unavailable")

            def stop(self, device_id):
                stopped.append(device_id)

        class Source:
            interruption_timeout = 1
            frames_per_chunk = 2
            info = SimpleNamespace(sample_rate=1000, channels=2)
            sequence = 0

            async def read(self):
                assert bool(prepared) == (self.sequence > 0)
                self.sequence += 1
                if self.sequence > 3:
                    raise EOFError
                return PCMChunk(b"\x01\x00\x02\x00\x03\x00\x04\x00", 0, self.sequence, loop.time())

            def discard(self):
                pass

        class Stream:
            def set_live_source(self, live):
                assert live

            def prepare_audio(self, pcm, fmt, **kwargs):
                assert pcm == b"\x01\x00\x03\x00"

            async def commit_audio(self, *, play_start_us):
                committed.append(play_start_us)

        class Group:
            def start_stream(self, **kwargs):
                return Stream()

            async def stop(self):
                pass

        group = Group()
        role = SimpleNamespace(get_audio_requirements=lambda: SimpleNamespace(transformer=None),
                               get_min_buffer_us=lambda: 400_000, get_output_delay_us=lambda: 20_000)
        client = SimpleNamespace(client_id="spin", name="Spin", connection=object(),
                                 group=group, active_roles=[role])
        group.clients = [client]
        sendspin = SimpleNamespace(connected_clients=[client] if mixed else [],
                                   add_event_listener=lambda callback: lambda: None,
                                   clock=SimpleNamespace(now_us=lambda: clock_us))

        async def trust(*args, **kwargs):
            pass

        sendspin.trust_unpaired = trust
        airplay = server.LiveAirPlay(Outputs(), {device.id: device})
        try:
            await server._run_live(sendspin, config, Source(), airplay=airplay)
        except EOFError:
            pass
        lead = 430_000 if mixed else 300_000
        count = 2 if mixed else 3
        assert prepared == [("air", {"sample_rate": 1000, "channels": 1, "mode": "auto"})]
        assert stopped == ["air"]
        assert [offer["at_unix_us"] for offer in offers] == [unix_us + lead + 2000*i for i in range(count)]
        assert all(offer["pcm"] == b"\x02\x00\x04\x00" and offer["channels"] == 1 for offer in offers)
        assert len({offer["epoch"] for offer in offers}) == 1
        assert committed == ([clock_us + lead, clock_us + lead + 2000] if mixed else [])

    asyncio.run(exercise(False))
    asyncio.run(exercise(True))
