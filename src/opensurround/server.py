from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from uuid import UUID, uuid4

from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import FileServerPairingStore, InMemoryServerPairingStore
from aiosendspin.server import AudioFormat, SendspinServer
from aiosendspin.server.server import ClientConnectedEvent, ClientDisconnectedEvent

from .config import Config, EndpointConfig
from .routing import ChunkFeeder, gain_int16, split_int16_interleaved
from .wav_source import WavSource
from .pipe_source import PipePCMSource

_LOG = logging.getLogger("opensurround")


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
            needle = ep.match_name.casefold()
            matches = [c for c in connected if needle in (c.name or "").casefold()]
            if len(matches) == 1:
                found[ep.name] = matches[0]
        if len(found) == len(config.endpoints):
            return found
        if asyncio.get_running_loop().time() >= deadline:
            visible = [f"{c.name} ({c.client_id})" for c in connected]
            raise TimeoutError(f"Timed out waiting for endpoints. Connected: {visible}")
        await asyncio.sleep(0.5)


def _make_feeder(ep: EndpointConfig, sample_rate: int, frames_per_chunk: int) -> ChunkFeeder:
    """Build a chunk feeder for one endpoint, pre-filled with its delay as silence."""
    bytes_per_frame = 2 * len(ep.source_channels)
    feeder = ChunkFeeder(frames_per_chunk * bytes_per_frame)
    delay_frames = int(round(sample_rate * ep.delay_ms / 1000.0))
    if delay_frames > 0:
        feeder.feed(b"\x00" * (delay_frames * bytes_per_frame))
    return feeder


async def run(config: Config, wav_path: str | None = None, state_dir: str = ".opensurround", *,
              input_pipe: str | None = None, sample_rate: int = 48000,
              channels: int = 6, sample_format: str = "s32le") -> None:
    if bool(wav_path) == bool(input_pipe):
        raise ValueError("provide exactly one WAV or FIFO input")
    source = (PipePCMSource(input_pipe, config.chunk_ms, sample_rate=sample_rate,
                            channels=channels, sample_format=sample_format)
              if input_pipe else WavSource(wav_path, config.chunk_ms))
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
    server = SendspinServer(
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
        await server.start_server(port=config.port, host=config.host, discover_clients=True)
        _LOG.info("Sendspin server listening on %s:%s", config.host, config.port)
        _LOG.info("Point Kiosk Satellite Sendspin 'Server' to <this-host>:%s if discovery picks Music Assistant instead.", config.port)

        if input_pipe:
            await _run_live(server, config, source)
            return
        clients = await _wait_for_named_clients(server, config)
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
        stream = group.start_stream(channel_resolver=lambda player_id: client_to_channel[player_id])

        routes = {ep.name: ep.source_channels for ep in config.endpoints}
        fmts = {
            ep.name: AudioFormat(
                sample_rate=source.info.sample_rate,
                bit_depth=16,
                channels=len(ep.source_channels),
                sample_type="int",
            )
            for ep in config.endpoints
        }
        feeders = {
            ep.name: _make_feeder(ep, source.info.sample_rate, source.frames_per_chunk)
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
                pcm = gain_int16(pcm, ep.gain_db)
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
    finally:
        source.close()
        await server.close()


def _validate_audio(config: Config, channels: int) -> None:
    import math
    if config.chunk_ms <= 0 or config.buffer_lead_ms <= 0:
        raise ValueError("chunk_ms and buffer_lead_ms must be positive")
    for ep in config.endpoints:
        if not ep.match_name or not ep.source_channels or any(c < 0 or c >= channels for c in ep.source_channels):
            raise ValueError(f"endpoint {ep.name}: invalid match name or source channels")
        if not math.isfinite(ep.gain_db) or not math.isfinite(ep.delay_ms) or ep.delay_ms < 0:
            raise ValueError(f"endpoint {ep.name}: gain/delay must be finite and delay nonnegative")


def _match_live_clients(server: SendspinServer, config: Config, *, report: bool = True) -> dict:
    """Ambiguous endpoints and clients matching multiple routes are quarantined."""
    found = {}
    for ep in config.endpoints:
        matches = [c for c in server.connected_clients
                   if ep.match_name.casefold() in (c.name or "").casefold()]
        if len(matches) == 1:
            found[ep.name] = matches[0]
        elif len(matches) > 1 and report:
            _LOG.error("Ambiguous endpoint %s: %s; withholding audio", ep.name,
                       [c.client_id for c in matches])
    ids = [c.client_id for c in found.values()]
    if report and len(set(ids)) != len(ids):
        _LOG.error("One receiver matches multiple endpoints; withholding all overlapping routes")
    return {name: c for name, c in found.items() if ids.count(c.client_id) == 1}


async def _run_live(server: SendspinServer, config: Config, source: PipePCMSource) -> None:
    loop = asyncio.get_running_loop()
    group = stream = None
    active = {}
    trusted = {}
    feeders = {}
    channel_ids = {ep.name: uuid4() for ep in config.endpoints}
    formats = {ep.name: AudioFormat(sample_rate=source.info.sample_rate, bit_depth=16,
                                    channels=len(ep.source_channels), sample_type="int")
               for ep in config.endpoints}
    routes = {ep.name: ep.source_channels for ep in config.endpoints}
    previous = None
    # Restart before drift exhausts the lead, not after sending late audio.
    gap_limit = source.interruption_timeout
    anchor = None
    presentation_start = None
    sent_frames = 0
    transport_changed = False
    quarantine_channel = uuid4()
    visible = None

    def on_event(_, event):
        nonlocal transport_changed
        if isinstance(event, (ClientConnectedEvent, ClientDisconnectedEvent)):
            transport_changed = True
            if group is not None:
                # Invalidate caches synchronously before a retained player can
                # auto-join the old transport during its reconnect handshake.
                group.stop_stream()

    unsubscribe = server.add_event_listener(on_event)

    async def stop():
        nonlocal group, stream, previous, anchor
        if group is not None:
            await group.stop()
        group = stream = previous = anchor = None
        feeders.clear()

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(source.read(), timeout=gap_limit)
            except TimeoutError:
                await stop()
                continue
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
            if transport_changed or changed or discontinuity or stale or drift:
                if stream is not None:
                    _LOG.info("Restarting live stream (receivers=%s, source discontinuity=%s, stale=%s, drift=%s)",
                              changed or transport_changed, discontinuity, stale, drift)
                await stop()
            transport_changed = False
            active = signature
            if not matches or stale:
                continue
            if stream is None:
                group = next(iter(matches.values())).group
                for member in list(group.clients):
                    if member not in matches.values():
                        await member.ungroup()
                # Stop any automatically retained stream before regrouping.
                await group.stop()
                for client in list(matches.values())[1:]:
                    await group.add_client(client)
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
                feeders = {ep.name: _make_feeder(ep, source.info.sample_rate, source.frames_per_chunk)
                           for ep in config.endpoints if ep.name in matches}
                # Trust/group operations may take time. Drain continues; start
                # with a fresh chunk rather than replaying the triggering one.
                source.discard()
                previous = None
                anchor = None
                sent_frames = 0
                presentation_start = None
                continue
            split = split_int16_interleaved(chunk.pcm, source.info.channels, routes)
            for ep in config.endpoints:
                if ep.name in matches:
                    feeders[ep.name].feed(gain_int16(split[ep.name], ep.gain_db))
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
                presentation_start = stream.now_us() + lead_us
                _LOG.info("Live timeline anchored at %d us for %s", presentation_start, list(matches))
            timestamp = presentation_start + sent_frames * 1_000_000 // source.info.sample_rate
            await stream.commit_audio(play_start_us=timestamp)
            sent_frames += source.frames_per_chunk
            previous = chunk
    finally:
        unsubscribe()
        await stop()
