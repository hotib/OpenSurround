"""POSIX FIFO input: nonblocking readiness IO, bounded newest-audio queue."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import stat

import numpy as np

from .wav_source import WavInfo


@dataclass(frozen=True)
class PCMChunk:
    pcm: bytes
    generation: int
    sequence: int
    received_at: float


class PipePCMSource:
    """Drain independently of consumers; EOF drops incomplete data and old audio.

    The descriptor stays open across EOF so writers may reconnect without EPIPE.
    EOF readiness is level-triggered: temporarily unregister and retry at 100 ms
    rather than spin. No worker threads, blocking open/read, or unbounded queue.
    """

    def __init__(self, path: str | Path, chunk_ms: int = 20, *,
                 sample_rate: int = 48000, channels: int = 6,
                 sample_format: str = "s32le", max_chunks: int = 3):
        if sample_format != "s32le":
            raise ValueError("only s32le FIFO input is supported")
        if min(sample_rate, channels, chunk_ms, max_chunks) <= 0:
            raise ValueError("sample rate, channels, chunk duration and queue size must be positive")
        self.path = Path(path)
        self.info = WavInfo(sample_rate, channels, 16)
        self.frames_per_chunk = max(1, round(sample_rate * chunk_ms / 1000))
        self.input_chunk_bytes = self.frames_per_chunk * channels * 4
        self.queue: asyncio.Queue[PCMChunk] = asyncio.Queue(maxsize=max_chunks)
        self.generation = 0
        self.dropped_chunks = 0
        self._sequence = 0
        self._partial = bytearray()
        self._skip_bytes = 0
        self._fd: int | None = None
        self._retry: asyncio.TimerHandle | None = None
        self._idle: asyncio.TimerHandle | None = None
        self.interruption_timeout = max(0.05, 3 * self.frames_per_chunk / sample_rate)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._at_eof = False
        self._error: OSError | None = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        if self._fd is not None:
            raise RuntimeError("FIFO source is already started")
        self._loop = asyncio.get_running_loop()
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError(f"not a named pipe: {self.path}")
        self._fd = fd
        self.generation += 1
        self._at_eof = False
        self._skip_bytes = 0
        self._error = None
        self._arm()

    def _arm(self) -> None:
        self._retry = None
        if self._fd is not None:
            self._loop.add_reader(self._fd, self._ready)
            # Some platforms do not report EOF until a writer has first opened.
            self._ready()

    def discard(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()

    def _ready(self) -> None:
        # Limit work per callback so a fast writer cannot starve network IO.
        for _ in range(16):
            try:
                raw = os.read(self._fd, self.input_chunk_bytes)
            except BlockingIOError:
                return
            except OSError as exc:
                self._error = exc
                self.close()
                return
            if not raw:
                if self._idle is not None:
                    self._idle.cancel()
                    self._idle = None
                if not self._at_eof:
                    self.generation += 1
                    self._partial.clear()
                    self._skip_bytes = 0
                    self.discard()
                    self._at_eof = True
                self._loop.remove_reader(self._fd)
                self._retry = self._loop.call_later(0.1, self._arm)
                return
            self._at_eof = False
            if self._idle is not None:
                self._idle.cancel()
            self._idle = self._loop.call_later(self.interruption_timeout, self._interrupted)
            skip = min(self._skip_bytes, len(raw))
            self._skip_bytes -= skip
            self._partial.extend(raw[skip:])
            while len(self._partial) >= self.input_chunk_bytes:
                block = bytes(self._partial[:self.input_chunk_bytes])
                del self._partial[:self.input_chunk_bytes]
                pcm = (np.frombuffer(block, dtype="<i4") >> 16).astype("<i2").tobytes()
                self._sequence += 1
                if self.queue.full():
                    self.queue.get_nowait()
                    self.dropped_chunks += 1
                self.queue.put_nowait(PCMChunk(pcm, self.generation, self._sequence, self._loop.time()))
                self._wake.set()

    def _interrupted(self) -> None:
        self._idle = None
        self.generation += 1
        # After a stalled mid-frame write, skip its remaining bytes on resume.
        # A raw FIFO has no headers with which to resynchronize otherwise.
        self._skip_bytes = (self._skip_bytes - len(self._partial)) % (self.info.channels * 4)
        self._partial.clear()
        self.discard()

    async def read(self) -> PCMChunk:
        while True:
            if self._error is not None:
                raise self._error
            if self._fd is None:
                raise RuntimeError("FIFO source is closed")
            if not self.queue.empty():
                return self.queue.get_nowait()
            self._wake.clear()
            await self._wake.wait()

    def close(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None
        if self._fd is not None:
            self._loop.remove_reader(self._fd)
            os.close(self._fd)
            self._fd = None
        self._partial.clear()
        self.discard()
        self._wake.set()
