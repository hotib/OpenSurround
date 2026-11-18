"""Real FIFO + three real WebSocket Sendspin clients; no audio hardware needed."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import socket
import tempfile

from aiohttp import ClientSession, WSServerHandshakeError
import numpy as np
from aiosendspin.client.client import SendspinClient
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, Roles, GoodbyeReason
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryClientPairingStore

from opensurround.config import Config, EndpointConfig
from opensurround.server import run
from fifo_helpers import LiveWriter, pcm_block


class Collector:
    def __init__(self):
        self.chunks = []
        self.changed = asyncio.Event()

    def __call__(self, timestamp, data, fmt, ahead):
        self.chunks.append((timestamp, data))
        self.changed.set()

    async def wait(self, predicate):
        async with asyncio.timeout(8):
            while not predicate(self.chunks):
                self.changed.clear()
                await self.changed.wait()


async def cancel(task):
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def exercise(root: Path, writer_first: bool):
    fifo = root / 'surround.pcm'
    os.mkfifo(fifo)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    endpoints = tuple(EndpointConfig(n, n, (2*i, 2*i+1)) for i, n in enumerate(('LiveFront', 'LiveCenter', 'LiveRear')))
    config = Config('live-test', '127.0.0.1', port, 20, 300, True, False, endpoints)
    writer = LiveWriter(fifo, batched=not writer_first)
    writer_task = asyncio.create_task(writer.run()) if writer_first else None
    server_task = asyncio.create_task(run(config, state_dir=str(root / 'state'), input_pipe=str(fifo)))
    clients = []
    collectors = []

    async def connect(index, identity=None):
        client = SendspinClient(identity or Identity.generate(), endpoints[index].name, [Roles.PLAYER],
            pairing_store=InMemoryClientPairingStore(),
            player_support=ClientHelloPlayerSupport(supported_formats=[SupportedAudioFormat(
                codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=16)], buffer_capacity=5_000_000))
        await client.set_unpaired_access(enabled=True)
        collector = Collector()
        client.add_audio_chunk_listener(collector)
        try:
            async with asyncio.timeout(5):
                while True:
                    try:
                        await client.connect(f'ws://127.0.0.1:{port}/sendspin')
                        break
                    except WSServerHandshakeError as exc:
                        if exc.status != 503:
                            raise
                        await asyncio.sleep(.05)
        except BaseException:
            await client.disconnect()
            raise
        return client, collector

    try:
        # Observable listener readiness, with retry bounded by the outer timeout.
        async with asyncio.timeout(8):
            while True:
                if server_task.done():
                    await server_task
                try:
                    r, w = await asyncio.open_connection('127.0.0.1', port)
                    w.close()
                    await w.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.01)
        if writer_task is None:
            async with ClientSession() as session:
                async with session.get(f'http://127.0.0.1:{port}/sendspin') as response:
                    assert response.status == 503
            writer_task = asyncio.create_task(writer.run())
        # Active input with every receiver offline must continue flowing.
        await writer.wait_blocks(20)
        for i in range(3):
            c, out = await connect(i)
            clients.append(c)
            collectors.append(out)
        await asyncio.gather(*(c.wait(lambda xs: len(xs) >= 20) for c in collectors))

        def verify_shared(outs, marker):
            expected = np.frombuffer(pcm_block(marker), dtype='<i4').reshape(-1, 6)
            maps = [dict(c.chunks) for c in outs]
            common = set(maps[0]).intersection(*maps[1:])
            assert len(common) >= 10, f'not enough shared timestamps: {len(common)}'
            for t in common:
                for i, m in enumerate(maps):
                    actual = np.frombuffer(m[t], dtype='<i2').reshape(-1, 2)
                    assert np.all(actual == (expected[0, 2*i:2*i+2] >> 16).astype('<i2')), (i, actual[:2])
            ts = sorted(common)
            assert all(b-a == len(maps[0][a]) // 4 * 1_000_000 // 48000 for a,b in zip(ts, ts[1:])), 'timeline is not contiguous'
            return max(common)

        last = verify_shared(collectors, 0)
        # Receiver disconnect cannot backpressure source; reconnect same identity.
        counts = [len(c.chunks) for c in collectors[:2]]
        old_identity = clients[2].identity
        await clients[2].disconnect(reason=GoodbyeReason.RESTART if writer_first else GoodbyeReason.SHUTDOWN)
        await writer.wait_blocks(writer.blocks + 20)
        assert all(len(c.chunks) > n for c, n in zip(collectors[:2], counts)), "offline receiver stalled other outputs"
        c, out = await connect(2, old_identity)
        clients[2] = c
        collectors[2] = out
        for x in collectors[:2]:
            x.chunks.clear()
        await asyncio.gather(*(c.wait(lambda xs: len(xs) >= 20) for c in collectors))
        verify_shared(collectors, 0)

        # Prolonged writer interruption followed by distinct data must reanchor.
        await cancel(writer_task)
        writer_task = None
        # A deliberate source outage longer than the configured playback lead.
        outage = asyncio.Event()
        asyncio.get_running_loop().call_later(.45, outage.set)
        await outage.wait()
        for x in collectors:
            x.chunks.clear()
        writer = LiveWriter(fifo, marker=300)
        writer_task = asyncio.create_task(writer.run())
        await asyncio.gather(*(c.wait(lambda xs: len(xs) >= 20) for c in collectors))
        assert verify_shared(collectors, 300) > last + 450000
        if not writer_first:
            await cancel(writer_task)
            writer_task = None
            async with asyncio.timeout(6):
                while any(client.connected for client in clients):
                    await asyncio.sleep(.05)
            async with ClientSession() as session:
                async with session.get(f'http://127.0.0.1:{port}/sendspin') as response:
                    assert response.status == 503
            writer = LiveWriter(fifo, marker=700)
            writer_task = asyncio.create_task(writer.run())
            await writer.wait_blocks(2)
            old = clients[0]
            clients[0], collectors[0] = await connect(0, old.identity)
            await old.disconnect()
            await collectors[0].wait(lambda xs: len(xs) >= 20)
        assert not server_task.done(), 'live server stopped unexpectedly'
    finally:
        await cancel(writer_task)
        for c in clients:
            await c.disconnect()
        await cancel(server_task)


async def main():
    with tempfile.TemporaryDirectory(prefix='opensurround-live-') as tmp:
        for first in (True, False):
            root = Path(tmp) / str(first)
            root.mkdir()
            await asyncio.wait_for(exercise(root, first), 30)
    print('LIVE FIFO LOOPBACK: PASSED (writer before/after, offline receivers, reconnect, routing, shared timeline)')


if __name__ == '__main__':
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(main())
