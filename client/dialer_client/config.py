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

log = logging.getLogger("config")

CLIENT_DIR = Path(__file__).resolve().parent.parent
# run.py: config.toml lives in client/; EmanagerDialer.exe: next to the exe
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else CLIENT_DIR
CONFIG_PATH = APP_DIR / "config.toml"


@dataclass
class Config:
    # speech recognition, RAG and Gemini all run inside this app (see local_server.py)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_thinking_level: str = "low"
    database_url: str = ""          # Supabase Postgres for the knowledge base; empty = no RAG
    local_stt: str = "parakeet"     # parakeet | none
    parakeet_threads: int = 3
    parakeet_model_path: str = ""   # folder with the int8 ONNX files; empty = download (~670 MB) once
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
    # EMANAGER CRM (Supabase) for the client card; the operator signs in with their CRM account
    crm_url: str = ""               # https://<project>.supabase.co
    crm_key: str = ""               # publishable (anon) key: public by design, access is the operator's
    crm_sip: str = ""               # Zadarma extension of this PC (e.g. "100"); empty = any
    own_numbers: str = "609037902, 525275052"  # the company's own numbers: never a client card


def needs_setup(cfg: Config) -> bool:
    """No Gemini key yet (first run)."""
    return not cfg.gemini_api_key


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
    cfg.gemini_api_key = str(cfg.gemini_api_key).strip().strip("\"'")
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
