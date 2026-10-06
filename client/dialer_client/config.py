"""Client settings: config.toml next to the app + per-user QSettings for UI choices."""
from __future__ import annotations

import getpass
import json
import logging
import re
import sys
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("config")

CLIENT_DIR = Path(__file__).resolve().parent.parent
# run.py: config.toml lives in client/; EmanagerDialer.exe: next to the exe
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else CLIENT_DIR
CONFIG_PATH = APP_DIR / "config.toml"
DEFAULT_PORT = 8000
PLACEHOLDER_TOKEN = "change-me"


@dataclass
class Config:
    server_url: str = f"ws://localhost:{DEFAULT_PORT}/ws"
    token: str = PLACEHOLDER_TOKEN
    agent_id: str = ""
    mic_device: str = ""        # substring of device name; empty = Windows default
    line_device: str = ""       # substring of the output Zadarma plays to; empty = default output
    mic_gain: float = 1.0
    line_gain: float = 1.0
    corner: str = "bottom-right"
    hint_seconds: float = 20.0
    summary_seconds: float = 45.0
    opacity: float = 0.96
    show_in_taskbar: bool = True
    call_detect: str = "zadarma"   # zadarma = by Zadarma's own audio streams (Windows); voice = by any sound
    call_end_silence_s: float = 15.0
    no_line_warning_s: float = 12.0
    require_zadarma: bool = True
    offline_buffer_s: int = 60


def normalize_server_url(raw: str) -> str:
    """Accepts what people actually type and returns ws://host:port/ws.

    "localhost", " http://0.0.0.0:8000", "192.168.1.50:8000/" and "wss://dialer.example.com/ws"
    all work: http(s) becomes ws(s), 0.0.0.0 (a listen address) becomes localhost, the port
    defaults to 8000 for plain ws and the path to /ws.
    """
    s = raw.strip().strip("\"'").strip()
    if not s:
        return Config.server_url
    if "://" not in s:
        s = "ws://" + s
    parts = urlsplit(s)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme.lower(), parts.scheme.lower())
    if scheme not in ("ws", "wss"):
        scheme = "ws"
    host = parts.hostname or "localhost"
    if host in ("0.0.0.0", "::", "*"):
        host = "localhost"
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None and scheme == "ws":
        port = DEFAULT_PORT
    path = parts.path.rstrip("/") or "/ws"
    return urlunsplit((scheme, f"{host}:{port}" if port else host, path, parts.query, ""))


def needs_setup(cfg: Config, path: Path = CONFIG_PATH) -> bool:
    """First run, or config.toml still has the example server address. A wrong token is caught by
    the server instead ("auth" error card), since AUTH_TOKEN may legitimately be anything."""
    return not path.exists() or "your-server" in cfg.server_url.lower()


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    data = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:  # a typo must not stop the app
            log.error("config.toml is broken, using defaults: %s", e)
    known = {f.name for f in fields(Config)}
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    cfg.server_url = normalize_server_url(cfg.server_url)
    cfg.token = str(cfg.token).strip()
    if not cfg.agent_id:
        cfg.agent_id = getpass.getuser()
    return cfg


def save_values(values: dict, path: Path | None = None) -> None:
    """Sets top-level keys in config.toml, keeping the other lines and comments as they are."""
    path = path or CONFIG_PATH
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    for key, value in values.items():
        line = f"{key} = {_toml(value)}"
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i, old in enumerate(lines):
            if pat.match(old):
                lines[i] = line
                break
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)  # TOML basic strings use JSON-compatible escapes
