import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import errno
import json
from pathlib import Path
import socket
import stat
import struct
import sys
from tempfile import TemporaryDirectory
import time

import pytest

from opensurround.airplay import AirPlayDevice, AirPlayOutputs, _HEADER, _read
from opensurround.airplay_agent import AirPlayAgent


_FAKE = r"""
import json
import os
import select
import sys
import threading
import time

args = sys.argv[1:]
with open(LOG + "/args", "a") as log:
    log.write(json.dumps([arg if index == 0 or args[index - 1] != "--auth" else "<auth>"
                         for index, arg in enumerate(args)]) + "\n")
if "--ptp-daemon" in args:
    print("[14:19:06.054] ap2_ptp_run_daemon:1948 [PTP] daemon up: engine on 319/320", flush=True)
    threading.Event().wait()
if "--pair-setup" in args:
    print("Enter PIN: ", end="", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip() == "1234":
        print("CREDENTIALS: " + "ab" * 96, flush=True)
    else:
        print("CREDENTIALS: " + "cd" * 96, flush=True)
        sys.exit(1)
    sys.exit()

address = args[-1]
def event(value):
    with open(LOG + "/events-" + address, "a") as log:
        log.write(value + "\n")

def audio():
    with open(LOG + "/pcm-" + address, "ab", buffering=0) as log:
        while pcm := os.read(0, 4096):
            log.write(pcm)

fifo = args[args.index("--cmdpipe") + 1]
fd = os.open(fifo, os.O_RDWR)
print("[STATUS] connected", file=sys.stderr, flush=True)
with os.fdopen(fd) as commands:
    first = True
    offset = 10 if address == "127.0.0.1" else 0
    for line in commands:
        line = line.strip()
        event(line)
        if line.startswith("START_UNIX_MS="):
            requested = int(line.split("=")[1])
        elif line == "ACTION=START":
            time.sleep(.03)
            if first and select.select([0], [], [], 0)[0]:
                event("PCM before ack")
            print(f"[STATUS] started requested_unix_ms={requested} at_unix_ms={requested + offset}",
                  file=sys.stderr, flush=True)
            if first:
                threading.Thread(target=audio, daemon=True).start()
                first = False
        elif line == "ACTION=FLUSH":
            print("[STATUS] flushed head_unix_ms=0", file=sys.stderr, flush=True)
"""


async def _until(check):
    async with asyncio.timeout(5):
        while not check():
            await asyncio.sleep(0.01)


def _fake(directory):
    binary = directory / "cliairplay"
    binary.write_text(f"#!{sys.executable}\nLOG = {str(directory)!r}\n" + _FAKE)
    binary.chmod(0o700)
    return str(binary)


@pytest.fixture(params=["pathname", "socketpair"])
def listen(request, monkeypatch):
    @asynccontextmanager
    async def context(agent):
        if request.param == "pathname":
            with socket.socket(socket.AF_UNIX) as probe:
                path = agent.path.with_suffix(".probe")
                try:
                    probe.bind(str(path))
                except OSError as error:
                    if error.errno in (errno.EACCES, errno.EPERM):
                        pytest.skip("Sandbox prohibits AF_UNIX pathname bind")
                    raise
                path.unlink()
            async with agent.listen():
                assert stat.S_IMODE(agent.path.stat().st_mode) == 0o600
                yield
            return

        handlers = []
        async def connect(path):
            assert str(path) == str(agent.path)
            server, client = socket.socketpair()
            reader, writer = await real_connect(sock=server)
            handlers.append(asyncio.create_task(agent._client(reader, writer)))
            return await real_connect(sock=client)

        real_connect = asyncio.open_unix_connection
        monkeypatch.setattr(asyncio, "open_unix_connection", connect)
        async with agent._running():
            yield
        results = await asyncio.gather(*handlers, return_exceptions=True)
        assert all(result is None or isinstance(result, asyncio.CancelledError) for result in results)

    return context


def test_warm_native_pairing_and_late_pcm(listen):
    async def run(directory):
        binary = _fake(directory)
        path = directory / "agent.sock"
        agent = AirPlayAgent(path, binary, ptp=True)
        device = AirPlayDevice("speaker", "Speaker", "127.0.0.1", 7000,
                               {"features": "0x0,0x10240", "flags": "0x8", "name": "unsafe=1"})
        async with listen(agent):
            outputs = AirPlayOutputs(str(path))
            try:
                await outputs.pair_begin(device)
                with pytest.raises(ValueError):
                    await outputs.pair_finish(device, "1234\n")
                with pytest.raises(RuntimeError):
                    await outputs.pair_finish(device, "4321")
                assert sorted(p.name for p in agent.state.iterdir()) == ["dacp"]
                await outputs.pair_begin(device)
                await outputs.pair_finish(device, "1234")
                credential, = [p for p in agent.state.iterdir() if p.name != "dacp"]
                assert credential.read_bytes() == b"ab" * 96
                assert stat.S_IMODE(credential.stat().st_mode) == 0o600

                outputs.prepare(device, sample_rate=48000, channels=1)
                await _until(lambda: outputs.status(device.id) == "ready")
                stamp = (time.time_ns() // 1_000_000 + 500) * 1000
                pcm = directory / "pcm-127.0.0.1"
                events = directory / "events-127.0.0.1"
                def offer(epoch, when, payload):
                    outputs.offer(device, epoch=epoch, at_unix_us=when, pcm=payload,
                                  sample_rate=48000, channels=1)

                offer(1, stamp, b"\x01\x00" * 480)
                offer(1, stamp + 10000, struct.pack("<960h", *range(960)))
                await _until(lambda: pcm.exists() and pcm.stat().st_size == 960 * 4)
                assert outputs.status(device.id) == "late"
                assert pcm.read_bytes() == struct.pack("<1920h", *(i for i in range(960) for _ in range(2)))

                offer(2, stamp + 50_000, b"\x02\x00" * 600)
                offer(1, stamp + 80_000, b"\x03\x00" * 600)
                await _until(lambda: pcm.stat().st_size == (960 + 120) * 4)
                assert pcm.read_bytes()[-120 * 4:] == b"\x02\x00" * 240
                assert events.read_text().splitlines() == [
                    "START_JOIN=1", f"START_UNIX_MS={stamp // 1000}", "ACTION=START",
                    "ACTION=FLUSH", "START_JOIN=1", f"START_UNIX_MS={stamp // 1000 + 50}", "ACTION=START",
                ]
                args = [json.loads(line) for line in (directory / "args").read_text().splitlines()]
                daemon, rejected, paired, stream = args
                assert daemon[0] == "--ptp-daemon"
                assert daemon[2] == rejected[4] == paired[4] == stream[5]
                assert "--ptp-shared" in stream and "--auth" in stream
                assert "unsafe=1" not in " ".join(stream)
                outputs.stop(device.id)
                assert outputs.status(device.id) == "offline"
            finally:
                await outputs.close()
        assert not path.exists()

    with TemporaryDirectory(prefix="ap-", dir="/tmp") as directory:
        asyncio.run(run(Path(directory)))


def test_compat_resamples_and_uses_bounded_socket(listen):
    async def run(directory):
        path = directory / "agent.sock"
        device = AirPlayDevice("compat", "Speaker", "127.0.0.2", 7000, {"features": "0x0,0x10240"})
        async with listen(AirPlayAgent(path, _fake(directory))):
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(_HEADER.pack(1_000_000, 0))
            await writer.drain()
            assert await asyncio.wait_for(_read(reader), 2) == ({"status": "error"}, b"")
            writer.close()
            await writer.wait_closed()

            outputs = AirPlayOutputs(str(path))
            try:
                stamp = (time.time_ns() // 1_000_000 + 500) * 1000
                for index in range(3):
                    outputs.offer(device, epoch=0, at_unix_us=stamp + index * 10_000,
                                  pcm=struct.pack("<hh", 700, -700) * 480, sample_rate=48000, channels=2)
                pcm = directory / "pcm-127.0.0.2"
                await _until(lambda: pcm.exists() and pcm.stat().st_size >= 5000)
                assert outputs.status(device.id) == "playing"
                assert 1250 <= pcm.stat().st_size // 4 <= 1323
                left, right = struct.unpack("<hh", pcm.read_bytes()[200:204])
                assert (left, right) == pytest.approx((700, -700), abs=1)
                args = json.loads((directory / "args").read_text())
                assert args[:2] == ["--protocol", "airplay2-compat"]
                assert args[args.index("--timing") + 1] == "ntp"
                assert args[args.index("--samplerate") + 1] == "44100"
                assert "--ptp-shared" not in args
                assert "--auth" not in args

                # A forced native route can use transient pairing over NTP.
                other = replace(device, id="transient", address="127.0.0.3")
                outputs.prepare(other, sample_rate=44100, channels=2, mode="native")
                await _until(lambda: outputs.status(other.id) == "ready")
                args = json.loads((directory / "args").read_text().splitlines()[-1])
                assert args[:2] == ["--protocol", "airplay2"]
                assert "--ap2-native" in args
                assert "--ptp-shared" not in args
            finally:
                await outputs.close()

    with TemporaryDirectory(prefix="ap-", dir="/tmp") as directory:
        asyncio.run(run(Path(directory)))
