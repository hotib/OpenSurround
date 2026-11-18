import asyncio
import os

import numpy as np
import pytest

from opensurround.pipe_source import PipePCMSource
from opensurround.routing import split_int16_interleaved
from fifo_helpers import pcm_block, open_writer, write_bytes


def test_conversion_partial_reads_reconnect_and_shutdown(tmp_path):
    async def scenario():
        path = tmp_path / 'pcm'
        os.mkfifo(path)
        source = PipePCMSource(path)
        source.start()
        assert source.frames_per_chunk == 960
        assert source.input_chunk_bytes == 23040
        # No writer must leave the event loop usable and read cancellable.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(source.read(), .15)
        fd = await open_writer(path)
        try:
            raw = pcm_block()
            await write_bytes(fd, raw[:13])  # deliberately split inside a sample/frame
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(source.read(), .01)
            await write_bytes(fd, raw[13:])
            chunk = await asyncio.wait_for(source.read(), 1)
            actual = np.frombuffer(chunk.pcm, dtype='<i2').reshape(-1, 6)
            assert len(chunk.pcm) == 11520
            assert np.all(actual == [12000, -12000, 32767, -32768, 1, -1])
            await write_bytes(fd, b'\xff' * 7)
        finally:
            os.close(fd)
        # Wait on observable generation change, not a guessed reconnect delay.
        async with asyncio.timeout(1):
            while source.generation == chunk.generation:
                await asyncio.sleep(.001)
        fd = await open_writer(path)
        try:
            await write_bytes(fd, pcm_block(100))
            new = await asyncio.wait_for(source.read(), 1)
            assert new.generation > chunk.generation
            assert np.frombuffer(new.pcm, dtype='<i2')[:6].tolist() == [12100, -12100, 32767, -32768, 101, -101]
            # Flood without consuming; producer remains independent and queue bounded.
            for _ in range(15):
                await write_bytes(fd, pcm_block())
            async with asyncio.timeout(1):
                while source.dropped_chunks == 0:
                    await asyncio.sleep(.001)
            assert source.queue.qsize() <= 3
            assert len(source._partial) < source.input_chunk_bytes
            pending = asyncio.create_task(source.read())
            source.close()
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(pending, 1)
            assert source._fd is None and source._retry is None
            source.close()
        finally:
            os.close(fd)
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_all_signed_conversion_edges(tmp_path):
    async def scenario():
        path = tmp_path / 'pcm'
        os.mkfifo(path)
        source = PipePCMSource(path, sample_rate=1000, channels=1, chunk_ms=6)
        source.start()
        fd = await open_writer(path)
        try:
            values = np.array([0, 65535, 65536, -1, 2147483647, -2147483648], dtype='<i4')
            await write_bytes(fd, values.tobytes())
            chunk = await asyncio.wait_for(source.read(), 1)
            assert np.frombuffer(chunk.pcm, dtype='<i2').tolist() == [0, 0, 1, -1, 32767, -32768]
        finally:
            source.close()
            os.close(fd)
    asyncio.run(scenario())


def test_eight_channel_input_keeps_side_and_back_distinct(tmp_path):
    async def scenario():
        path = tmp_path / "pcm71"
        os.mkfifo(path)
        source = PipePCMSource(path, channels=8)
        source.start()
        fd = await open_writer(path)
        try:
            values = np.arange(1, 9, dtype="<i4") << 16
            await write_bytes(fd, values.tobytes() * source.frames_per_chunk)
            chunk = await asyncio.wait_for(source.read(), 1)
            routes = {"front": (0, 1), "center_lfe": (2, 3), "back": (4, 5), "side": (6, 7)}
            outputs = split_int16_interleaved(chunk.pcm, 8, routes)
            for name, channels in routes.items():
                pcm = np.frombuffer(outputs[name], dtype="<i2").reshape(-1, 2)
                assert np.all(pcm == [index + 1 for index in channels])
        finally:
            os.close(fd)
            source.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('kwargs', [{'channels': 0}, {'sample_rate': 0}, {'chunk_ms': 0}, {'max_chunks': 0}, {'sample_format': 's16le'}])
def test_invalid_format(kwargs):
    with pytest.raises(ValueError):
        PipePCMSource('/unused', **kwargs)


def test_reject_regular_file(tmp_path):
    path = tmp_path / 'file'
    path.write_bytes(b'')
    async def scenario():
        with pytest.raises(ValueError):
            PipePCMSource(path).start()
    asyncio.run(scenario())


def test_stalled_midframe_and_source_restart(tmp_path):
    async def scenario():
        path = tmp_path / 'pcm'
        os.mkfifo(path)
        source = PipePCMSource(path)
        source.start()
        fd = await open_writer(path)
        try:
            initial = source.generation
            await write_bytes(fd, pcm_block()[:13])
            async with asyncio.timeout(1):
                while source.generation == initial:
                    await asyncio.sleep(.001)
            # Finish the abandoned frame, then send a fresh complete chunk.
            await write_bytes(fd, pcm_block()[13:24] + pcm_block(200))
            chunk = await asyncio.wait_for(source.read(), 1)
            assert np.frombuffer(chunk.pcm, dtype='<i2')[:6].tolist() == [12200, -12200, 32767, -32768, 201, -201]
        finally:
            os.close(fd)
            source.close()
        source.start()
        fd = await open_writer(path)
        try:
            await write_bytes(fd, pcm_block())
            assert len((await asyncio.wait_for(source.read(), 1)).pcm) == 11520
            source.discard()
            pending = asyncio.create_task(source.read())
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            source.close()
            os.close(fd)
    asyncio.run(asyncio.wait_for(scenario(), 3))
