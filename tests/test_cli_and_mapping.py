import sys
from types import SimpleNamespace

import pytest

from opensurround.cli import main
from opensurround.config import Config, EndpointConfig, load_config
from opensurround.server import _match_live_clients, _validate_audio


def config(*endpoints):
    return Config('test', '127.0.0.1', 1, 20, 300, False, False, endpoints)


def test_duplicate_matches_and_overlapping_routes(caplog):
    front = SimpleNamespace(name='Front', client_id='a')
    rear = SimpleNamespace(name='Rear', client_id='b')
    server = SimpleNamespace(connected_clients=[front, rear])
    cfg = config(EndpointConfig('front', 'Front', (0, 1)), EndpointConfig('rear', 'Rear', (4, 5)))
    assert _match_live_clients(server, cfg) == {'front': front, 'rear': rear}
    server.connected_clients.append(SimpleNamespace(name='Front duplicate', client_id='c'))
    assert _match_live_clients(server, cfg) == {'rear': rear}
    assert 'Ambiguous' in caplog.text
    cfg = config(EndpointConfig('a', 'Rear', (0, 1)), EndpointConfig('b', 'Rear', (4, 5)))
    assert _match_live_clients(server, cfg) == {}


@pytest.mark.parametrize('argv', [[], ['x.wav', '--input-pipe', '/tmp/pcm'], ['--input-pipe', '/tmp/pcm', '--channels', '0'], ['--input-pipe', '/tmp/pcm', '--format', 'float32']])
def test_cli_rejects_invalid_input(monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', ['opensurround', *argv])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_live_config_and_validation():
    cfg = load_config('configs/surround-live.toml')
    assert len(cfg.endpoints) == 1
    assert cfg.endpoints[0].source_channels == (0, 1)
    _validate_audio(cfg, 6)
    with pytest.raises(ValueError):
        _validate_audio(cfg, 1)


@pytest.mark.parametrize('signal_name', ['SIGINT', 'SIGTERM'])
def test_cli_signal_cleanup(monkeypatch, tmp_path, signal_name):
    import asyncio
    import signal
    import opensurround.cli as cli
    from opensurround.pipe_source import PipePCMSource
    import os
    path = tmp_path / 'pcm'
    os.mkfifo(path)
    source = PipePCMSource(path)
    async def serve(*args, **kwargs):
        assert kwargs['sample_rate'] == 48000
        assert kwargs['channels'] == 6
        assert kwargs['sample_format'] == 's32le'
        source.start()
        loop = asyncio.get_running_loop()
        # Invoke the handler registered by the real CLI, without signaling pytest.
        loop.call_soon(loop._signal_handlers[getattr(signal, signal_name)]._run)
        try:
            await asyncio.wait_for(source.read(), 1)
        finally:
            source.close()
    monkeypatch.setattr(cli, 'run', serve)
    monkeypatch.setattr(sys, 'argv', ['opensurround', '--input-pipe', str(path), '-c', 'configs/surround-live.toml'])
    cli.main()
    assert source._fd is None
    assert source._retry is None
