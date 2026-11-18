from __future__ import annotations
import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import logging
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import FileServerPairingStore, InMemoryServerPairingStore
from aiosendspin.server import AudioFormat, SendspinServer
from aiosendspin.server.roles.player.v1 import PlayerV1Role
from aiosendspin.server.server import ClientConnectedEvent, ClientDisconnectedEvent

from .config import Config, EndpointConfig
from .routing import ChunkFeeder, gain_int16, split_int16_interleaved
from .sendspin_demand import LiveSendspinServer
from .wav_source import WavSource
from .pipe_source import PipePCMSource

if TYPE_CHECKING:
    from .airplay import AirPlayDevice, AirPlayOutputs
    from .web import WebControl

_LOG = logging.getLogger("opensurround")
_SPEAKER_IDLE_SECONDS = 3.0


def _load_or_create_identity(path: Path) -> Identity:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return Identity.from_private_bytes(path.read_bytes())
    identity = Identity.generate()
    path.write_bytes(identity.private_bytes)
    return identity


async def _wait_for_named_clients(server: SendspinServer, config: Config, timeout: float = 120.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        connected = server.connected_clients
        found = {}
        for ep in config.endpoints:
            matches = [c for c in connected if _matches_endpoint(ep, c)]
            if len(matches) == 1:
                found[ep.name] = matches[0]
        if len(found) == len(config.endpoints):
            return found
        if asyncio.get_running_loop().time() >= deadline:
            visible = [f"{c.name} ({c.client_id})" for c in connected]
            raise TimeoutError(f"Timed out waiting for endpoints. Connected: {visible}")
        await asyncio.sleep(0.5)


def _matches_endpoint(endpoint: EndpointConfig, client) -> bool:
    if endpoint.client_id:
        return client.client_id == endpoint.client_id
    return endpoint.match_name.casefold() in (client.name or "").casefold()


def _playback_route(ep: EndpointConfig, client, *, managed: bool) -> tuple[int, ...]:
    """Prefer the source channel count using the player's supported formats."""
    route = ep.source_channels
    if not managed and not ep.client_id:
        return route
    player = next((role for role in client.active_roles if isinstance(role, PlayerV1Role)), None)
    if player is None or player.preferred_format is None:
        return route
    current = player.preferred_format
    if len(route) != current.channels:
        supported = [fmt for fmt in player.get_supported_formats() or () if fmt.channels == len(route)]
        if supported:
            fmt = min(supported, key=lambda fmt: (fmt.codec != player.preferred_codec,
                                                 fmt.sample_rate != current.sample_rate,
                                                 fmt.bit_depth != current.bit_depth))
            if player.set_preferred_format(
                AudioFormat(sample_rate=fmt.sample_rate, bit_depth=fmt.bit_depth, channels=fmt.channels),
                fmt.codec,
            ):
                return route
        if len(route) == 1 and current.channels == 2:
            return (route[0], route[0])
    return route


def _endpoint_pcm(ep: EndpointConfig, pcm: bytes) -> bytes:
    return bytes(len(pcm)) if ep.muted else gain_int16(pcm, ep.gain_db)


def _make_feeder(ep: EndpointConfig, sample_rate: int, frames_per_chunk: int, channels: int) -> ChunkFeeder:
    """Build a chunk feeder for one endpoint, pre-filled with its delay as silence."""
    bytes_per_frame = 2 * channels
    feeder = ChunkFeeder(frames_per_chunk * bytes_per_frame)
    delay_frames = int(round(sample_rate * ep.delay_ms / 1000.0))
    if delay_frames > 0:
        feeder.feed(b"\x00" * (delay_frames * bytes_per_frame))
    return feeder


_AIRPLAY_ERRORS = (ValueError, OSError, RuntimeError)


class LiveAirPlay:
    def __init__(self, outputs: AirPlayOutputs, devices: dict[str, AirPlayDevice]):
        self.outputs = outputs
        self.devices = devices
        self.errors = set()

    def call(self, method, device_id, *args, **kwargs):
        try:
            method(*args, **kwargs)
        except _AIRPLAY_ERRORS:
            self.errors.add(device_id)
        else:
            self.errors.discard(device_id)

    def status(self, device_id):
        if device_id not in self.devices:
            return "offline"
        if device_id in self.errors:
            return "error"
        try:
            return self.outputs.status(device_id)
        except _AIRPLAY_ERRORS:
            return "error"


async def run(config: Config, wav_path: str | None = None, state_dir: str = ".opensurround", *,
              input_pipe: str | None = None, sample_rate: int = 48000,
              channels: int = 6, sample_format: str = "s32le", web: WebControl | None = None,
              airplay_socket: str | None = None) -> None:
    if bool(wav_path) == bool(input_pipe):
        raise ValueError("provide exactly one WAV or FIFO input")
    if airplay_socket is not None and not input_pipe:
        raise ValueError("AirPlay requires FIFO input")
    source = (PipePCMSource(input_pipe, config.chunk_ms, sample_rate=sample_rate,
                            channels=channels, sample_format=sample_format)
              if input_pipe else WavSource(wav_path, config.chunk_ms))
    services = AsyncExitStack()
    try:
        _validate_audio(config, source.info.channels)
    except Exception:
        source.close()
        raise

    loop = asyncio.get_running_loop()
    state = Path(state_dir)
    identity = _load_or_create_identity(state / "identity.key")
    if config.persist_pairings:
        pairing_store = await FileServerPairingStore.open(state / "pairings.json")
    else:
        # In-memory pairing store: every run starts clean. Persistent records break
        # subsequent runs with unencrypted (transition-mode) clients such as
        # Kiosk Satellite: once "known", an unencrypted connection claiming that
        # client_id is rejected as a possible identity spoof.
        pairing_store = InMemoryServerPairingStore()
    server_type = LiveSendspinServer if input_pipe else SendspinServer
    server = server_type(
        loop,
        identity,
        config.server_name,
        pairing_store=pairing_store,
        allow_noncompliant_clients=True,
        allow_unencrypted=True,
    )
    try:
        if input_pipe:
            source.start()
        await server.start_server(port=config.port, host=config.host,
                                  discover_clients=not input_pipe)
        if isinstance(server, LiveSendspinServer):
            try:
                await services.enter_async_context(server.discover())
            except OSError:
                _LOG.warning("Sendspin mDNS discovery unavailable")
        _LOG.info("Sendspin server listening on %s:%s", config.host, config.port)
        _LOG.info("Point Kiosk Satellite Sendspin 'Server' to <this-host>:%s if discovery picks Music Assistant instead.", config.port)

        airplay = None
        if airplay_socket is not None:
            try:
                from .airplay import AirPlayOutputs
                from .airplay_discovery import discover_airplay

                airplay = LiveAirPlay(AirPlayOutputs(airplay_socket), {})
                services.push_async_callback(airplay.outputs.close)
                await services.enter_async_context(discover_airplay(airplay.devices))
            except (ImportError, OSError, RuntimeError, ValueError):
                _LOG.warning("AirPlay discovery or agent unavailable")
                airplay = None
        if web is not None:
            await services.enter_async_context(web.serve(
                server, source.info.channels, airplay=airplay, sample_rate=source.info.sample_rate,
            ))
        if input_pipe:
            await _run_live(server, config, source, web=web, airplay=airplay)
            return
        config = _sendspin_config(config)
        if web is None:
            if not config.endpoints:
                raise ValueError("WAV input requires a Sendspin route")
            clients = await _wait_for_named_clients(server, config)
        else:
            while True:
                config = _sendspin_config(web.at_boundary(config))
                clients = _match_live_clients(server, config, report=False)
                if clients and len(clients) == len(config.endpoints):
                    break
                await asyncio.sleep(0.5)
        if config.auto_trust_unpaired:
            for client in clients.values():
                await server.trust_unpaired(client.client_id, approved_roles={"player"})
            await asyncio.sleep(1.0)

        leader = next(iter(clients.values()))
        group = leader.group
        for client in list(clients.values())[1:]:
            await group.add_client(client)

        channel_ids: dict[str, UUID] = {name: uuid4() for name in clients}
        client_to_channel = {clients[name].client_id: channel_ids[name] for name in clients}
        routes = {ep.name: _playback_route(ep, clients[ep.name], managed=web is not None)
                  for ep in config.endpoints}
        stream = group.start_stream(channel_resolver=lambda player_id: client_to_channel[player_id])
        fmts = {
            ep.name: AudioFormat(
                sample_rate=source.info.sample_rate,
                bit_depth=16,
                channels=len(routes[ep.name]),
                sample_type="int",
            )
            for ep in config.endpoints
        }
        feeders = {
            ep.name: _make_feeder(ep, source.info.sample_rate, source.frames_per_chunk, len(routes[ep.name]))
            for ep in config.endpoints
        }
        _LOG.info(
            "Input: %s Hz, %s channels. Routing: %s",
            source.info.sample_rate,
            source.info.channels,
            {ep.name: {"channels": ep.source_channels, "gain_db": ep.gain_db, "delay_ms": ep.delay_ms} for ep in config.endpoints},
        )

        first_start_us: int | None = None
        # Pacing strategy (fixes both observed hardware problems):
        # 1. The client buffers can only hold a few seconds (Kiosk Satellite:
        #    ~800 kB ≈ 4 s of PCM), so dumping the whole file at once plays a few
        #    seconds and then stops.
        # 2. Burst-sleep-burst filling produces ~1 s send gaps that stutter.
        # So: first commit a preroll of buffer_lead_ms of audio as fast as
        # possible (builds the client buffer), then send exactly one chunk per
        # chunk_ms in real time, keeping the client buffer at a steady lead.
        preroll_chunks = max(1, config.buffer_lead_ms // config.chunk_ms)
        sent = 0
        start_pace: float | None = None
        while raw := source.read():
            split = split_int16_interleaved(raw, source.info.channels, routes)
            for name, pcm in split.items():
                ep = next(e for e in config.endpoints if e.name == name)
                pcm = _endpoint_pcm(ep, pcm)
                feeders[name].feed(pcm)
                chunk = feeders[name].pull()
                if chunk is not None:
                    stream.prepare_audio(chunk, fmts[name], channel_id=channel_ids[name])
            if first_start_us is None:
                first_start_us = await stream.commit_audio()
                _LOG.info("Playback timeline anchored at %d us", first_start_us)
            else:
                await stream.commit_audio()
            sent += 1
            if sent <= preroll_chunks:
                continue  # preroll: fill the client buffer without pausing
            if start_pace is None:
                start_pace = loop.time()
            # Real-time pacing: one chunk every chunk_ms, keeping the preroll
            # lead in the client buffer. If we fall behind (encoding hiccup),
            # do not burst to catch up — that recreates the sawtooth.
            next_due = start_pace + (sent - preroll_chunks) * config.chunk_ms / 1000.0
            now = loop.time()
            if next_due > now:
                await asyncio.sleep(next_due - now)
            else:
                start_pace = now - (sent - preroll_chunks) * config.chunk_ms / 1000.0

        # Flush any remaining partial chunk (per-endpoint delay tail).
        for name, feeder in feeders.items():
            chunk = feeder.pull_final()
            if chunk is not None:
                stream.prepare_audio(chunk, fmts[name], channel_id=channel_ids[name])
        await stream.commit_audio()
        source.close()
        _LOG.info("End of WAV; allowing buffered audio to drain")
        await asyncio.sleep(config.buffer_lead_ms / 1000.0 + 1.0)
        await group.stop()
        if web is not None:
            config = web.at_boundary(config)
            while True:
                await asyncio.sleep(0.5)
                config = web.at_boundary(config)
    finally:
        source.close()
        try:
            await services.aclose()
        finally:
            await server.close()


def _validate_audio(config: Config, channels: int) -> None:
    import math
    if config.chunk_ms <= 0 or config.buffer_lead_ms <= 0:
        raise ValueError("chunk_ms and buffer_lead_ms must be positive")
    for ep in config.endpoints:
        if (not (ep.client_id or ep.match_name) or not ep.source_channels
                or any(c < 0 or c >= channels for c in ep.source_channels)):
            raise ValueError(f"endpoint {ep.name}: invalid match name or source channels")
        if not math.isfinite(ep.gain_db) or not math.isfinite(ep.delay_ms) or ep.delay_ms < 0:
            raise ValueError(f"endpoint {ep.name}: gain/delay must be finite and delay nonnegative")


def _sendspin_config(config: Config) -> Config:
    return replace(config, endpoints=tuple(ep for ep in config.endpoints if ep.transport == "sendspin"))


def _match_live_clients(server: SendspinServer, config: Config, *, report: bool = True) -> dict:
    """Ambiguous endpoints and clients matching multiple routes are quarantined."""
    found = {}
    for ep in config.endpoints:
        if ep.transport != "sendspin":
            continue
        matches = [c for c in server.connected_clients if _matches_endpoint(ep, c)]
        if len(matches) == 1:
            found[ep.name] = matches[0]
        elif len(matches) > 1 and report:
            _LOG.error("Ambiguous endpoint %s: %s; withholding audio", ep.name,
                       [c.client_id for c in matches])
    ids = [c.client_id for c in found.values()]
    if report and len(set(ids)) != len(ids):
        _LOG.error("One receiver matches multiple endpoints; withholding all overlapping routes")
    return {name: c for name, c in found.items() if ids.count(c.client_id) == 1}


async def _run_live(server: SendspinServer, config: Config, source: PipePCMSource,
                    *, web: WebControl | None = None, airplay: LiveAirPlay | None = None) -> None:
    loop = asyncio.get_running_loop()
    group = stream = None
    active = {}
    trusted = {}
    feeders = {}
    input_generation = None
    channel_ids = {ep.name: uuid4() for ep in config.endpoints}
    previous = None
    last_audio = None
    live_sendspin = server if isinstance(server, LiveSendspinServer) else None
    # Restart before drift exhausts the lead, not after sending late audio.
    gap_limit = source.interruption_timeout
    anchor = None
    presentation_start = None
    sent_frames = 0
    transport_changed = False
    quarantine_channel = uuid4()
    visible = None
    air_routes = {}
    air_feeders = {}
    air_prepared = set()
    air_playing = set()
    next_prepare = 0.0
    epoch = 0
    unix_offset = 0
    sample_rate = source.info.sample_rate

    def refresh_airplay():
        nonlocal air_routes, next_prepare
        if airplay is None:
            return
        routes = {ep.client_id: (ep, airplay.devices[ep.client_id])
                  for ep in config.endpoints
                  if ep.transport == "airplay" and ep.client_id in airplay.devices}
        changed = {key for key, old in air_routes.items() if routes.get(key) != old}
        for device_id in changed:
            airplay.call(airplay.outputs.stop, device_id, device_id)
            air_prepared.discard(device_id)
            air_playing.discard(device_id)
            air_feeders.pop(device_id, None)
        now = loop.time()
        for device_id, (ep, device) in routes.items():
            if air_routes.get(device_id) != (ep, device) or now >= next_prepare:
                airplay.call(airplay.outputs.prepare, device_id, device, sample_rate=sample_rate,
                             channels=len(ep.source_channels), mode=ep.airplay_mode)
                air_prepared.add(device_id)
        if now >= next_prepare:
            next_prepare = now + 0.5
        air_routes = routes

    def stop_airplay(all_outputs=False):
        nonlocal next_prepare
        stopped = air_prepared | air_playing if all_outputs else air_playing
        if stopped:
            for device_id in stopped:
                airplay.call(airplay.outputs.stop, device_id, device_id)
            air_prepared.difference_update(stopped)
            air_playing.clear()
            next_prepare = 0.0
        air_feeders.clear()

    def on_event(_, event):
        nonlocal transport_changed
        if isinstance(event, (ClientConnectedEvent, ClientDisconnectedEvent)):
            transport_changed = True
            if group is not None:
                # Invalidate caches synchronously before a retained player can
                # auto-join the old transport during its reconnect handshake.
                group.stop_stream()

    unsubscribe = server.add_event_listener(on_event)

    async def stop(*, all_outputs=False):
        nonlocal group, stream, previous, anchor, presentation_start, sent_frames
        if group is not None:
            await group.stop()
        group = stream = previous = anchor = presentation_start = None
        sent_frames = 0
        feeders.clear()
        stop_airplay(all_outputs)

    async def apply_routes():
        nonlocal config, channel_ids, visible
        updated = web.at_boundary(config)
        if _sendspin_config(updated).endpoints != _sendspin_config(config).endpoints:
            await stop()
            visible = None
            channel_ids = {ep.name: uuid4() for ep in updated.endpoints}
        config = updated

    try:
        if web is not None:
            await apply_routes()
        while True:
            try:
                chunk = await asyncio.wait_for(source.read(), timeout=gap_limit)
            except TimeoutError:
                idle = last_audio is not None and loop.time() - last_audio >= _SPEAKER_IDLE_SECONDS
                await stop(all_outputs=idle)
                if idle:
                    if live_sendspin is not None:
                        await live_sendspin.release_speakers()
                    air_routes.clear()
                    last_audio = None
                if web is not None:
                    input_generation = None
                    await apply_routes()
                continue
            if web is not None and input_generation != chunk.generation:
                await apply_routes()
                input_generation = chunk.generation
            now = loop.time()
            if now - chunk.received_at <= gap_limit:
                last_audio = chunk.received_at
                if live_sendspin is not None:
                    live_sendspin.connect_routes(config.endpoints, now)
                refresh_airplay()
            topology = tuple((c.client_id, c.connection, c.name) for c in server.connected_clients)
            matches = _match_live_clients(server, config, report=topology != visible)
            visible = topology
            trusted = {c.client_id: trusted[c.client_id] for c in matches.values()
                       if c.client_id in trusted}
            for client in matches.values():
                if config.auto_trust_unpaired and trusted.get(client.client_id) is not client.connection:
                    await server.trust_unpaired(client.client_id, approved_roles={"player"})
                    trusted[client.client_id] = client.connection
            # Pending approval/role activation must not hold back other players.
            matches = {n: c for n, c in matches.items()
                       if any(r.get_audio_requirements() is not None for r in c.active_roles)}
            # Compare transport and role identity as client IDs survive reconnects.
            signature = {n: (c.connection, tuple((id(r), r.get_min_buffer_us(), r.get_output_delay_us()) for r in c.active_roles
                                              if r.get_audio_requirements() is not None))
                         for n, c in matches.items()}
            changed = signature != active
            discontinuity = (previous is not None and
                             (chunk.generation != previous.generation or
                              chunk.sequence != previous.sequence + 1))
            stale = loop.time() - chunk.received_at > gap_limit
            drift = (anchor is not None and
                     abs(loop.time() - (anchor + sent_frames / source.info.sample_rate)) > gap_limit)
            receivers_changed = changed or (transport_changed and stream is not None)
            if receivers_changed or discontinuity or stale or drift:
                if stream is not None:
                    _LOG.info("Restarting live stream (receivers=%s, source discontinuity=%s, stale=%s, drift=%s)",
                              receivers_changed, discontinuity, stale, drift)
                await stop()
            transport_changed = False
            active = signature
            has_airplay_routes = airplay is not None and any(ep.transport == "airplay" for ep in config.endpoints)
            if stale or (not matches and not has_airplay_routes):
                continue
            if matches and stream is None:
                group = next(iter(matches.values())).group
                for member in list(group.clients):
                    if member not in matches.values():
                        await member.ungroup()
                # Stop any automatically retained stream before regrouping.
                await group.stop()
                for client in list(matches.values())[1:]:
                    await group.add_client(client)
                routes = {ep.name: _playback_route(ep, matches[ep.name], managed=web is not None)
                          for ep in config.endpoints if ep.name in matches}
                formats = {name: AudioFormat(sample_rate=source.info.sample_rate, bit_depth=16,
                                             channels=len(route), sample_type="int")
                           for name, route in routes.items()}
                mapping = {c.client_id: channel_ids[n] for n, c in matches.items()}
                stream = group.start_stream(channel_resolver=lambda player_id, m=mapping: m.get(player_id, quarantine_channel))
                stream.set_live_source(True)
                # A disconnected role was absent from the old stream's stop()
                # reset set. Its group pool may still hold a PCM encoder with
                # the previous timestamp cursor. Reset all newly grouped role
                # transformers before the first commit of this epoch.
                for client in matches.values():
                    for role in client.active_roles:
                        req = role.get_audio_requirements()
                        if req is not None and req.transformer is not None:
                            req.transformer.reset()
                feeders = {ep.name: _make_feeder(ep, source.info.sample_rate, source.frames_per_chunk,
                                                len(routes[ep.name]))
                           for ep in config.endpoints if ep.name in routes}
                # Trust/group operations may take time. Drain continues; start
                # with a fresh chunk rather than replaying the triggering one.
                source.discard()
                previous = None
                anchor = None
                sent_frames = 0
                presentation_start = None
                continue
            if stream is not None:
                split = split_int16_interleaved(chunk.pcm, source.info.channels, routes)
                for ep in config.endpoints:
                    if ep.name in matches:
                        feeders[ep.name].feed(_endpoint_pcm(ep, split[ep.name]))
                        pcm = feeders[ep.name].pull()
                        stream.prepare_audio(pcm, formats[ep.name], channel_id=channel_ids[ep.name])
            if anchor is None:
                anchor = loop.time()
            if presentation_start is None:
                lead_us = config.buffer_lead_ms * 1000
                for client in matches.values():
                    for role in client.active_roles:
                        if role.get_audio_requirements() is not None:
                            lead_us = max(lead_us, role.get_min_buffer_us() + role.get_output_delay_us() + 10_000)
                now_us = server.clock.now_us()
                if airplay is not None:
                    # Keep this clock conversion fixed for the sample epoch.
                    unix_offset = time_ns() // 1000 - now_us
                presentation_start = now_us + lead_us
                epoch += 1
                _LOG.info("Live timeline anchored at %d us for %s", presentation_start, list(matches))
            timestamp = presentation_start + sent_frames * 1_000_000 // source.info.sample_rate
            if stream is not None:
                await stream.commit_audio(play_start_us=timestamp)
            if air_routes:
                air_pcm = split_int16_interleaved(chunk.pcm, source.info.channels,
                                                  {key: ep.source_channels for key, (ep, _) in air_routes.items()})
                for device_id, (ep, device) in air_routes.items():
                    channels = len(ep.source_channels)
                    if device_id not in air_feeders:
                        air_feeders[device_id] = _make_feeder(ep, sample_rate, source.frames_per_chunk, channels)
                    feeder = air_feeders[device_id]
                    feeder.feed(_endpoint_pcm(ep, air_pcm[device_id]))
                    airplay.call(airplay.outputs.offer, device_id, device, epoch=epoch,
                                 at_unix_us=timestamp + unix_offset, pcm=feeder.pull(),
                                 sample_rate=sample_rate, channels=channels, mode=ep.airplay_mode)
                    air_playing.add(device_id)
            sent_frames += source.frames_per_chunk
            previous = chunk
    finally:
        unsubscribe()
        await stop(all_outputs=True)
        if live_sendspin is not None:
            await live_sendspin.release_speakers()
