"""Deterministic POSIX FIFO writer with cancellable open/write and no threads."""
import asyncio
import errno
import os

import numpy as np


def pcm_block(marker=0, frames=960):
    values = np.array([12000 + marker, -12000 - marker, 32767, -32768, 1 + marker, -1 - marker], dtype="<i4")
    return np.tile(values << 16, (frames, 1)).astype("<i4").tobytes()


async def write_bytes(fd, raw):
    loop = asyncio.get_running_loop()
    view = memoryview(raw)
    while view:
        try:
            written = os.write(fd, view)
            view = view[written:]
        except BlockingIOError:
            ready = loop.create_future()
            def writable():
                if not ready.done():
                    ready.set_result(None)
            loop.add_writer(fd, writable)
            try:
                await ready
            finally:
                loop.remove_writer(fd)


async def open_writer(path):
    # Bounded retry for the actual FIFO open condition, not playback timing.
    async with asyncio.timeout(5):
        while True:
            try:
                return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
                await asyncio.sleep(0.01)


class LiveWriter:
    def __init__(self, path, marker=0, *, batched=False):
        self.path = path
        self.marker = marker
        self.batched = batched
        self.blocks = 0
        self.progress = asyncio.Event()

    async def run(self):
        fd = await open_writer(self.path)
        loop = asyncio.get_running_loop()
        due = loop.time()
        size = 5 if self.batched else 1
        jitter = 0.02 if self.batched else 0
        delay = 0
        try:
            while True:
                # The sample rate is constant even when the writer batches.
                await asyncio.sleep(max(0, due + delay - loop.time()))
                for _ in range(size):
                    await write_bytes(fd, pcm_block(self.marker))
                    self.blocks += 1
                self.progress.set()
                due += size * 0.02
                delay = jitter - delay
        finally:
            os.close(fd)

    async def wait_blocks(self, count):
        async with asyncio.timeout(5):
            while self.blocks < count:
                self.progress.clear()
                await self.progress.wait()
