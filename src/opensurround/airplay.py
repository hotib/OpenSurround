"""Nonblocking router-side transport for interleaved signed 16-bit LE PCM."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass, field
import json
import re
import struct

_HEADER = struct.Struct("!II")
_MAX_CONTROL = 8192
_MAX_PCM = 65536
_MAX_DEVICES = 32
_QUEUE_DEPTH = 32
_TIMEOUT = 15
_MODES = {"auto", "native", "compat"}
_STATES = {"connecting", "ready", "playing", "late", "error", "offline"}
_PIN = re.compile(r"[0-9]{4}")


@dataclass(frozen=True)
class AirPlayDevice:
    id: str
    name: str
    address: str
    port: int
    txt: dict[str, str]


async def _read(reader: asyncio.StreamReader) -> tuple[dict, bytes]:
    size, audio_size = _HEADER.unpack(await reader.readexactly(_HEADER.size))
    if not 0 < size <= _MAX_CONTROL or audio_size > _MAX_PCM:
        raise ValueError("Invalid AirPlay message size")
    try:
        message = json.loads(await reader.readexactly(size))
    except (ValueError, RecursionError):
        raise ValueError("Invalid AirPlay message") from None
    if not isinstance(message, dict):
        raise ValueError("Invalid AirPlay message")
    return message, await reader.readexactly(audio_size)


async def _write(writer: asyncio.StreamWriter, message: dict, pcm: bytes = b"") -> None:
    payload = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode()
    if len(payload) > _MAX_CONTROL or len(pcm) > _MAX_PCM:
        raise ValueError("AirPlay message too large")
    writer.write(_HEADER.pack(len(payload), len(pcm)) + payload + pcm)
    await writer.drain()


async def _exchange(writer, reader, message, pcm=b""):
    async with asyncio.timeout(_TIMEOUT):
        await _write(writer, message, pcm)
        reply, audio = await _read(reader)
        if audio:
            raise ValueError("Invalid AirPlay reply")
        return reply


@dataclass
class _Output:
    device: AirPlayDevice
    sample_rate: int
    channels: int
    mode: str
    state: str = "connecting"
    epoch: int = -1
    timestamp: int = -1
    queue: deque = field(default_factory=lambda: deque(maxlen=_QUEUE_DEPTH))
    pending: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    retry_at: float = 0


class AirPlayOutputs:
    """A warm connection per output. Status 'late' means START moved forward."""

    def __init__(self, socket_path: str):
        self._path = socket_path
        self._outputs: dict[str, _Output] = {}
        self._pairs: dict[str, tuple] = {}
        self._tasks: set[asyncio.Task] = set()
        self._pairing: dict[str, asyncio.Task] = {}
        self._closed = False

    def prepare(self, device: AirPlayDevice, *, sample_rate: int, channels: int, mode: str = "auto") -> None:
        if self._closed:
            return
        loop = asyncio.get_running_loop()
        output = self._outputs.get(device.id)
        if output and (output.device, output.sample_rate, output.channels, output.mode) != (
            device, sample_rate, channels, mode,
        ):
            self.stop(device.id)
            output = None
        if output is None:
            if len(self._outputs) >= _MAX_DEVICES:
                return
            output = self._outputs[device.id] = _Output(
                AirPlayDevice(device.id, device.name, device.address, device.port, dict(device.txt)),
                sample_rate, channels, mode,
            )
        if (output.task is None or output.task.done()) and loop.time() >= output.retry_at:
            output.state = "connecting"
            output.task = loop.create_task(self._run(output))
            self._tasks.add(output.task)
            output.task.add_done_callback(self._tasks.discard)

    def offer(
        self, device: AirPlayDevice, *, epoch: int, at_unix_us: int, pcm: bytes,
        sample_rate: int, channels: int, mode: str = "auto",
    ) -> None:
        if not pcm or len(pcm) > _MAX_PCM or channels not in (1, 2) or len(pcm) % (2 * channels):
            return
        current = self._outputs.get(device.id)
        if current and (epoch < current.epoch or (
            epoch == current.epoch and at_unix_us <= current.timestamp
        )):
            return
        self.prepare(device, sample_rate=sample_rate, channels=channels, mode=mode)
        output = self._outputs.get(device.id)
        if not output:
            return
        if epoch > output.epoch:
            output.queue.clear()
            output.epoch = epoch
        output.timestamp = at_unix_us
        output.queue.append((epoch, at_unix_us, pcm))
        output.pending.set()

    async def _run(self, output: _Output) -> None:
        writer = None
        try:
            async with asyncio.timeout(_TIMEOUT):
                reader, writer = await asyncio.open_unix_connection(self._path)
            reply = await _exchange(writer, reader, {
                "op": "prepare", "device": asdict(output.device), "rate": output.sample_rate,
                "channels": output.channels, "mode": output.mode,
            })
            if reply.get("status") != "ready":
                raise RuntimeError("AirPlay renderer unavailable")
            output.state = "ready"
            while not reader.at_eof():
                if not output.queue:
                    output.pending.clear()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(output.pending.wait(), 0.5)
                    continue
                epoch, stamp, pcm = output.queue.popleft()
                reply = await _exchange(writer, reader, {"op": "pcm", "epoch": epoch, "at": stamp}, pcm)
                status = reply.get("status")
                if status not in _STATES or status == "error":
                    raise RuntimeError("AirPlay renderer failed")
                if epoch == output.epoch:
                    output.state = status
            output.state = "offline"
        except (OSError, EOFError):
            output.state = "offline" if writer is None else "error"
        except (ValueError, RuntimeError):
            output.state = "error"
        finally:
            output.retry_at = asyncio.get_running_loop().time() + 1
            if writer:
                writer.transport.abort()

    def stop(self, device_id: str | None = None) -> None:
        for key in tuple(self._outputs) if device_id is None else (device_id,):
            output = self._outputs.pop(key, None)
            if output and output.task:
                output.task.cancel()

    def status(self, device_id: str) -> str:
        output = self._outputs.get(device_id)
        return output.state if output else "offline"

    async def pair_begin(self, device: AirPlayDevice) -> None:
        if self._closed or device.id in self._pairing:
            raise RuntimeError("AirPlay pairing unavailable")
        if len(self._pairs) + len(self._pairing) >= _MAX_DEVICES and device.id not in self._pairs:
            raise RuntimeError("Too many AirPlay pairing sessions")
        previous = self._pairs.pop(device.id, None)
        if previous:
            previous[2].close()
        self._pairing[device.id] = asyncio.current_task()
        writer = None
        try:
            async with asyncio.timeout(_TIMEOUT):
                reader, writer = await asyncio.open_unix_connection(self._path)
            reply = await _exchange(writer, reader, {"op": "pair", "device": asdict(device)})
            if self._closed or reply.get("status") != "pin":
                raise RuntimeError("AirPlay PIN prompt unavailable")
            self._pairs[device.id] = (device, reader, writer)
            writer = None
        except (EOFError, OSError, ValueError):
            raise OSError("AirPlay pairing connection failed") from None
        finally:
            self._pairing.pop(device.id, None)
            if writer:
                writer.close()

    async def pair_finish(self, device: AirPlayDevice, pin: str) -> None:
        if not isinstance(pin, str) or not _PIN.fullmatch(pin):
            raise ValueError("AirPlay PIN must contain four digits")
        session = self._pairs.get(device.id)
        if self._closed or not session or (session[0].address, session[0].port) != (device.address, device.port):
            raise RuntimeError("AirPlay pairing has not started")
        _, reader, writer = self._pairs.pop(device.id)
        self._pairing[device.id] = asyncio.current_task()
        try:
            reply = await _exchange(writer, reader, {"op": "pin", "pin": pin})
            if reply.get("status") != "paired":
                raise RuntimeError("AirPlay pairing failed")
            self.stop(device.id)
        except (EOFError, OSError, ValueError):
            raise OSError("AirPlay pairing connection failed") from None
        finally:
            self._pairing.pop(device.id, None)
            writer.close()

    async def close(self) -> None:
        self._closed = True
        self.stop()
        for _, _, writer in self._pairs.values():
            writer.close()
        self._pairs.clear()
        pairs = tuple(self._pairing.values())
        for task in pairs:
            task.cancel()
        await asyncio.gather(*self._tasks, *pairs, return_exceptions=True)
