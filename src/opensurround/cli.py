from __future__ import annotations
import argparse
import asyncio
import logging
import signal
from .config import load_config
from .server import run


def main() -> None:
    p = argparse.ArgumentParser(description="OpenSurround Sendspin PoC")
    p.add_argument("wav", nargs="?", help="16-bit multichannel PCM WAV")
    p.add_argument("--input-pipe", help="existing named pipe containing interleaved PCM")
    p.add_argument("--sample-rate", type=int, default=48000)
    p.add_argument("--channels", type=int, default=6)
    p.add_argument("--format", choices=["s32le"], default="s32le")
    p.add_argument("-c", "--config", default="configs/4ch-demo.toml")
    p.add_argument("--state-dir", default=".opensurround")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    if bool(args.wav) == bool(args.input_pipe):
        p.error("provide either a WAV path or --input-pipe")
    if args.sample_rate <= 0 or args.channels <= 0:
        p.error("--sample-rate and --channels must be positive")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    async def serve():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await run(load_config(args.config), args.wav, args.state_dir,
                      input_pipe=args.input_pipe, sample_rate=args.sample_rate,
                      channels=args.channels, sample_format=args.format)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    try:
        asyncio.run(serve())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass

if __name__ == "__main__":
    main()
