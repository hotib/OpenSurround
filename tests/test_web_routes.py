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
