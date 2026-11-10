import wave
import numpy as np
from opensurround.wav_source import WavSource


def test_wav_metadata_chunks_and_eof(tmp_path):
    path = tmp_path / 'six.wav'
    data = np.arange(1000 * 6, dtype='<i2').tobytes()
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((6, 2, 48000, 0, 'NONE', 'not compressed'))
        wav.writeframes(data)
    source = WavSource(path, 20)
    try:
        assert (source.info.sample_rate, source.info.channels, source.info.bit_depth) == (48000, 6, 16)
        assert source.frames_per_chunk == 960
        assert source.read() == data[:11520]
        assert source.read() == data[11520:]
        assert source.read() == b''
    finally:
        source.close()
