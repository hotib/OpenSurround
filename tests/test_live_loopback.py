import asyncio
from loopback_live_fifo import main


def test_live_fifo_loopback():
    asyncio.run(asyncio.wait_for(main(), 65))
