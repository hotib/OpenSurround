from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import tomllib

@dataclass(frozen=True)
class EndpointConfig:
    name: str
    match_name: str
    source_channels: tuple[int, ...]
    gain_db: float = 0.0
    delay_ms: float = 0.0
    client_id: str = ""
    muted: bool = False
    label: str | None = None

@dataclass(frozen=True)
class Config:
    server_name: str
    host: str
    port: int
    chunk_ms: int
    buffer_lead_ms: int
    auto_trust_unpaired: bool
    persist_pairings: bool
    endpoints: tuple[EndpointConfig, ...]


def load_config(path: str | Path, *, allow_empty: bool = False) -> Config:
    return parse_config(Path(path).read_text(encoding="utf-8"), allow_empty=allow_empty)


def parse_config(text: str, *, allow_empty: bool = False) -> Config:
    data = tomllib.loads(text)
    server = data.get("server", {})
    audio = data.get("audio", {})
    endpoints = []
    for name, cfg in data.get("endpoints", {}).items():
        ch = tuple(int(x) for x in cfg["source_channels"])
        if len(ch) not in (1, 2):
            raise ValueError(f"endpoint {name}: source_channels must contain 1 (mono) or 2 (stereo) channels")
        endpoints.append(EndpointConfig(
            name,
            str(cfg.get("match_name", "")) if cfg.get("client_id") else str(cfg["match_name"]),
            ch,
            gain_db=float(cfg.get("gain_db", 0.0)),
            delay_ms=float(cfg.get("delay_ms", 0.0)),
            client_id=str(cfg.get("client_id", "")),
            muted=bool(cfg.get("muted", False)),
            label=str(cfg["label"]) if "label" in cfg else None,
        ))
    if not endpoints and not allow_empty:
        raise ValueError("configure at least one endpoint")
    return Config(
        server_name=str(server.get("name", "OpenSurround PoC")),
        host=str(server.get("host", "0.0.0.0")),
        port=int(server.get("port", 8927)),
        chunk_ms=int(audio.get("chunk_ms", 20)),
        buffer_lead_ms=int(audio.get("buffer_lead_ms", 1000)),
        auto_trust_unpaired=bool(server.get("auto_trust_unpaired", True)),
        persist_pairings=bool(server.get("persist_pairings", False)),
        endpoints=tuple(endpoints),
    )
