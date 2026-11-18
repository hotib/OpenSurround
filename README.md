# OpenSurround PoC

## Web settings

Add `--web` to the existing OpenSurround command and open
`http://127.0.0.1:8080`. The web page reads the TOML passed with `-c`
and edits its speaker routes. It supports individual mono/stereo channel
selections, gain, delay, mute, and restoring the preceding save. Live
route changes take effect at the next input pause or restart.

For browser access from another computer on a trusted LAN, set
`--web-host` to the LXC address and pass `--web-token-file` with a
32–256 character URL-safe admin token. The token file must be owned
by the service user and have mode `600`. Use HTTPS or an SSH tunnel
when the network cannot be trusted.

A deliberately small proof of concept for **different multichannel PCM pairs sent to different Sendspin clients on one synchronized timeline**.

## What this proves (once run against two real clients)

Input: a 4-channel, 48 kHz, 16-bit PCM WAV.

- channels 1/2 -> Kiosk Satellite client A
- channels 3/4 -> Kiosk Satellite client B
- both outputs are prepared as separate `aiosendspin` channel IDs
- one `PushStream.commit_audio()` commits both channels together

This is *not* an AVR, Atmos decoder, bass-management engine, or Music Assistant plugin yet.

## Status

- Routing unit tests pass; the server boots cleanly against the pinned `aiosendspin` commit.
- **Loopback end-to-end test passes** (`python tests/loopback_e2e.py`): two simulated
  Sendspin player clients connect over real WebSockets to the PoC server and receive
  byte-exact, correctly routed stereo pairs (ch 0/1 and ch 2/3) on identical
  presentation timestamps — proving grouping, auto-trust, channel routing and the
  shared timeline at the protocol level.
- **5.1 support (v0.0.2)**: a 6-channel WAV in ITU-R BS.775 / WAV order
  (L, R, C, LFE, Ls, Rs — Dolby: FL, FR, C, LFE, SL, SR) routes to three
  endpoints via `configs/6ch-demo.toml`; the 3-client loopback test
  (`python tests/loopback_e2e_6ch.py`) verifies it byte-exact without hardware.
- `tools/measure_sync.py` analyzes Audacity *Sample Data Export* recordings:
  click onsets and per-phone DAC clock skew (ppm) via phase-slope on the
  per-phone spectral components of the test WAVs.
- **Not yet tested with two physical clients.** That is the next milestone.

## Requirements

- Python 3.12+
- two Android devices running Kiosk Satellite with Sendspin enabled
- wired analog output strongly preferred for testing (3.5 mm/USB DAC, not Bluetooth)
- a machine on the same LAN to run this server

## Layout

```text
src/opensurround/    package code (cli, config, routing, server, wav_source)
configs/             TOML endpoint/routing configuration (4ch-demo, 6ch-demo)
tools/               make_test_wav.py, make_test_wav_6ch.py, measure_sync.py
tests/               unit tests + loopback E2E (2ch-pair and 5.1)
```

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e . pytest
python tools/make_test_wav.py            # 4-ch test tone (stereo pair per client)
python tools/make_test_wav_6ch.py       # 5.1 test tone (three clients)
pytest                                  # routing unit tests
python tests/loopback_e2e.py             # full loopback E2E (no hardware needed)
python tests/loopback_e2e_6ch.py         # 5.1 loopback E2E, three clients
```

`aiosendspin` is pinned to a specific commit (see `pyproject.toml`) so builds are
reproducible while Sendspin is still a Technical Preview. The pin is an archive
URL, so `git` is not required for installation.

## Configure clients

Edit `configs/4ch-demo.toml` so each `match_name` uniquely matches the device
name shown by Kiosk Satellite.

In each Kiosk Satellite:

1. Settings -> Media Player -> Enable Sendspin player.
2. For the test, set **Server** manually to the IP of the OpenSurround host, e.g. `192.168.1.20:8927`. This avoids the device choosing an existing Music Assistant Sendspin server via mDNS.
3. Prefer **PCM** while testing to remove codec buffering from the equation.
4. Leave Audio sync offset at 0 ms for the first measurement.

If port 8927 is already occupied by Music Assistant on the same host, run OpenSurround on another machine or edit the config port and point Kiosk Satellite to that port explicitly.

## Run

```bash
opensurround test_4ch.wav -c configs/4ch-demo.toml -v   # stereo pair per client
opensurround test_6ch.wav -c configs/6ch-demo.toml -v   # 5.1, three clients
```

### 5.1 channel order

`make_test_wav_6ch.py` and `configs/6ch-demo.toml` follow the ITU-R BS.775 /
WAV channel order:

| index | ITU | Dolby | endpoint (6ch-demo.toml) |
|-------|-----|-------|--------------------------|
| 0     | L   | FL    | front (L) |
| 1     | R   | FR    | front (R) |
| 2     | C   | C     | center-sub (C) |
| 3     | LFE | LFE   | center-sub (LFE) |
| 4     | Ls  | SL    | rear (Ls) |
| 5     | Rs  | SR    | rear (Rs) |

The LFE signal is line-level: a phone speaker cannot reproduce it. Real sub
deployment needs bass management (see roadmap); for the sync test only the
per-second impulse on the LFE channel matters.

The server auto-trusts the specifically selected clients for the `player` role in this PoC. Do not expose this test server to an untrusted network.

## Channel routing per endpoint (0.2/0.3 features)

Each endpoint takes 1 (mono) or 2 (stereo) source channels, plus calibration:

```toml
[endpoints.surround]
match_name = "Nokia Q"
source_channels = [4, 5]
gain_db = -3.0    # per-endpoint gain, clips at 0 dBFS
delay_ms = 2.5    # shifts this endpoint later on the shared timeline
```

- `gain_db` matches levels between different amps/phones.
- `delay_ms` compensates a measured fixed offset (silence-insertion on a shared
  timeline, so sync with other endpoints is preserved). Measured offsets of a
  few ms are normal; calibrate per endpoint, positive values only.
- Mono endpoints (`source_channels = [2]` for a center speaker) are supported.

## Tuning playback smoothness

If playback stutters, tune `[audio]` in the config:

- `buffer_lead_ms` (default 1000): how far ahead of real time the server streams.
  Higher absorbs more Wi-Fi jitter at the cost of end-to-end latency.
- `chunk_ms` (default 20): PCM chunk size. 40–100 ms can help on weak Wi-Fi.

The server paces sends in real time (one chunk per `chunk_ms`, sent
`buffer_lead_ms` early) instead of burst-filling the buffer, which caused
stutter on hardware clients.

## Expected result

Client `front` receives source channels 0/1. Client `rear` receives source channels 2/3. The generated WAV puts a different tone on every channel and a short impulse on all channels at exactly the same source sample once per second.

For the best sync test, put all speakers close together and record the impulses with one microphone. Compare arrival-time differences at startup, after 10 minutes, and after a long run. A constant offset is calibratable; a changing offset indicates drift.

## Measuring (first experiment checklist)

1. Does device A receive only source channels 0/1 and device B only 2/3?
2. Do simultaneous impulses arrive acoustically at the same time (speakers close together)?
3. Is any offset between devices constant, or does it drift over time?
4. Any audible dropouts, pops, underruns or reconnects?
5. What is the total buffer / end-to-end latency (important for video use)?

Use one recording with one clock (one microphone or one multichannel interface),
never two independent phone recordings.

## Next milestones

1. Measure fixed offset and drift with two Android clients.
2. Add per-endpoint static delay/gain.
3. 5.1 routing shipped in v0.0.2; extend to 5.1.2 / 7.1.2 / 7.1.4 layouts (only needs more endpoints + a wider WAV).
4. Add live multichannel PipeWire/ALSA input.
5. Add DSP/bass management (likely CamillaDSP rather than reinventing it).
6. Explore a Music Assistant provider/virtual-player integration only after transport is proven.

## Security / compatibility note

This prototype enables transitional/non-compliant Sendspin compatibility and automatically approves the selected clients for unpaired playback. That is convenient for a LAN lab test, not appropriate as a finished security model. Never commit `.opensurround/` state (it contains your private server identity and pairing store).

## Live Shairport Sync PCM FIFO input

The live input accepts an **existing POSIX named pipe** containing interleaved
signed S32LE PCM. Defaults are 48000 Hz and six channels in FL, FR, FC, LFE,
SL, SR order. It shifts each signed sample right by 16 bits into S16LE, without
resampling, dithering, channel remapping, or decoding AAC. Shairport Sync remains
responsible for decoding and producing the agreed format.

On the Linux host already running Shairport Sync 5.6 with its output configured
to `/tmp/surround.pcm`, use these commands from the project directory:

```bash
source .venv/bin/activate
# Create once, before either process opens it; preserve an existing FIFO.
if [ ! -e /tmp/surround.pcm ]; then mkfifo -m 600 /tmp/surround.pcm; fi
test -p /tmp/surround.pcm
opensurround --input-pipe /tmp/surround.pcm \
  --sample-rate 48000 --channels 6 --format s32le \
  -c configs/surround-live.toml
```

The pipe must be readable by OpenSurround and writable by the Shairport service
account; arrange ownership/access for those actual accounts. In another terminal,
if Shairport Sync is managed by the existing `shairport-sync` systemd service:

```bash
sudo systemctl start shairport-sync
```

Either process can start first. A blocking FIFO writer started first waits until
OpenSurround opens the FIFO. These commands use the existing Shairport setup and
do not change its installation or configuration. Select that Shairport receiver
on the Apple TV. Point the front ESP32 Sendspin receiver to this host, port 8928.
The supplied configuration includes only `ESP32 AirPlay Nappali` on channels 0/1;
no other receiver is required. Additional routes can be enabled explicitly:

```toml
[endpoints.center-sub]
match_name = "ESP32 Center"
source_channels = [2, 3]
gain_db = 0.0
delay_ms = 0.0

[endpoints.rear]
match_name = "ESP32 Rear"
source_channels = [4, 5]
gain_db = 0.0
delay_ms = 0.0
```

### Live timeline and recovery policy

- The reader starts before Sendspin discovery completes. It drains even with no
  receivers. Nonblocking descriptor readiness callbacks do bounded work, with no
  blocking reader threads. At 20 ms, each input chunk is 960 frames / 23040 bytes,
  converted to 11520 output bytes. Only three completed chunks plus less than one
  partial input chunk are retained, in addition to the bounded kernel FIFO.
- Each available, uniquely matched endpoint gets its configured channels. An
  offline endpoint does not hold up other endpoints. Multiple matching clients,
  or one client matching multiple endpoints, are withheld instead of assigning
  another route. Names must remain stable and unique.
- Start uses fresh audio after grouping and approval. There is no WAV-style
  preroll burst or per-chunk pacing sleep. The first shared presentation timestamp
  is the Sendspin clock plus `buffer_lead_ms` (300 ms here), raised when an active
  role reports a larger minimum buffer/output delay. Subsequent timestamps advance
  by exact frame counts; all endpoint payloads commit together. Sendspin may
  repacketize 20 ms input into 25 ms output. Gain and delay apply per route;
  configured delay buffers are reset with each stream.
- PCM zero samples are ordinary audio. A short input gap keeps the timeline if
  no data was lost. No replacement silence is generated. Missing input for three
  chunks (at least 50 ms; 60 ms with this config), observed EOF, dropped chunks,
  or timeline drift beyond that threshold causes a restart. Partial chunks are
  discarded on interruption; a stalled writer's remaining incomplete frame bytes
  are skipped to retain frame alignment.
- Writer reconnect uses the same open FIFO descriptor. EOF readiness is disabled
  and retried every 100 ms to avoid CPU spin. Recovery drops old queued audio.
  The next stream calls the existing group stop/start lifecycle and anchors a
  fresh future timeline; it never continues the pre-interruption timestamp cursor.
- Receiver transport changes immediately stop the old group transport and clear
  its replay caches. Available endpoints regroup with stable per-endpoint channel
  IDs. Reconnect or membership changes restart all currently available outputs
  together, with a brief audible gap, to preserve one shared timeline. Unknown
  players resolve to an unused channel rather than another endpoint's route.
- SIGINT and SIGTERM cancel playback, stop the group, close the descriptor,
  unregister callbacks/timers and close the server. There are no blocked worker
  threads to leave behind.

### FIFO limitations and verification

Raw PCM has no format headers, capture timestamps, writer identity or framing
markers. All writers must honor the same format and start on a complete frame;
use exactly one writer and one reader. If writers hand over without an observable
EOF or time gap, the reader cannot detect the boundary. Replacing/unlinking the
FIFO inode while it is open is unsupported: restart OpenSurround against the new
pipe. A writer which holds the FIFO open while idle is handled by the interruption
timeout. Audio already buffered inside Shairport/kernel transport cannot be dated
reliably; recovery guarantees apply to observed bytes, not original capture time.
A receiver slower than real time or a non-real-time producer may cause drops and
restarts. This favors current audio over lossless delivery during overload.

This mode requires POSIX `add_reader` support (Linux target; also exercised on
macOS). It does not resample source clock drift; prolonged drift can trigger a
fresh stream. It cannot guarantee uninterrupted sound during source/network
outages. S32LE is the only raw input format currently supported. Outgoing codec
selection remains with `aiosendspin` and receiver capability negotiation; software
loopback covers PCM rather than physical ESP32 FLAC playback.

```bash
pytest -v
python tests/loopback_e2e_6ch.py
python tests/loopback_live_fifo.py
```

The live loopback uses real filesystem FIFOs and three real Sendspin WebSocket
clients, with bounded timeouts. It checks both startup orders, active input with
receivers absent, receiver reconnect, writer reconnect after a prolonged outage,
channel values and shared timestamps. WAV remains available with
`opensurround test_6ch.wav -c configs/6ch-demo.toml`, retaining its preroll, pacing
and EOF drain behavior. Software tests do **not** verify acoustic speaker
synchronization, actual Shairport/Apple TV decoding, or Apple TV lip-sync.
