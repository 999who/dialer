"""Client settings: config.toml next to run.py + per-user QSettings for UI choices."""
from __future__ import annotations

import getpass
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

CLIENT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    server_url: str = "ws://localhost:8000/ws"
    token: str = "change-me"
    agent_id: str = ""
    mic_device: str = ""        # substring of device name; empty = Windows default
    line_device: str = ""       # substring of the output Zadarma plays to; empty = default output
    mic_gain: float = 1.0
    line_gain: float = 1.0
    corner: str = "bottom-right"
    hint_seconds: float = 20.0
    summary_seconds: float = 45.0
    opacity: float = 0.96
    call_end_silence_s: float = 15.0
    no_line_warning_s: float = 12.0
    require_zadarma: bool = True
    offline_buffer_s: int = 60


def load_config(path: Path | None = None) -> Config:
    path = path or CLIENT_DIR / "config.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    known = {f.name for f in fields(Config)}
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    if not cfg.agent_id:
        cfg.agent_id = getpass.getuser()
    return cfg
