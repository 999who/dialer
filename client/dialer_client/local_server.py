"""Local mode: the backend (Parakeet, Gemini, RAG) runs inside the client process.

The same FastAPI app as backend/ is started with uvicorn in a background thread on
127.0.0.1 with a random port and a random token, and BackendLink connects to it like
to a remote server. So there is one code path for both modes, and a big call center
can still point every operator at one shared server instead.

uvicorn runs the app's startup (loading/downloading Parakeet, checking Gemini) before
it opens the port, so `state` says what the user is waiting for meanwhile.
"""
from __future__ import annotations

import logging
import os
import secrets
import socket
import sys
import threading
from pathlib import Path

log = logging.getLogger("local")

# run.py: <repo>/backend next to <repo>/client; exe: backend files are bundled at the root
if getattr(sys, "frozen", False):
    BACKEND_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    PROMPT = BACKEND_DIR / "prompts" / "master_prompt_pl.md"
else:
    BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
    PROMPT = BACKEND_DIR.parent / "prompts" / "master_prompt_pl.md"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LastError(logging.Handler):
    """Keeps the last logged exception: uvicorn reports a failed startup only as SystemExit(3)
    after logging the real error, and that real error is what the overlay should show."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.text = ""

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info and record.exc_info[1] is not None:
            e = record.exc_info[1]
            self.text = f"{type(e).__name__}: {e}"


class LocalServer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.port = _free_port()
        self.token = secrets.token_urlsafe(16)
        self.url = f"ws://127.0.0.1:{self.port}/ws"
        self.state = "loading"   # loading | ready | error
        self.error = ""
        self._server = None
        self._thread: threading.Thread | None = None

    def _env(self) -> dict[str, str]:
        c = self.cfg
        return {
            "AUTH_TOKEN": self.token,
            "GEMINI_API_KEY": c.gemini_api_key,
            "GEMINI_MODEL": c.gemini_model,
            "GEMINI_THINKING_LEVEL": c.gemini_thinking_level,
            "DATABASE_URL": c.database_url,
            "STT_ENGINE": c.local_stt,
            "PARAKEET_THREADS": str(c.parakeet_threads),
            "PARAKEET_MODEL_PATH": c.parakeet_model_path,
            "MASTER_PROMPT_PATH": str(PROMPT),
        }

    def start(self) -> None:
        os.environ.update(self._env())  # read by the backend's pydantic Settings
        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))
        self._thread = threading.Thread(target=self._run, name="local-backend", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        last = _LastError()
        logging.getLogger().addHandler(last)
        try:
            import uvicorn

            from app import config, main  # backend package

            config.get_settings.cache_clear()
            ucfg = uvicorn.Config(main.app, host="127.0.0.1", port=self.port, log_config=None,
                                  ws="websockets", lifespan="on", loop="asyncio")
            self._server = uvicorn.Server(ucfg)
            orig_startup = self._server.startup

            async def startup(sockets=None):
                await orig_startup(sockets=sockets)
                if not self._server.should_exit:
                    self.state = "ready"
                    log.info("local backend ready on port %d", self.port)

            self._server.startup = startup
            self._server.run()
            if self.state != "ready":
                raise RuntimeError("startup failed, see the log above")
        except BaseException as e:  # SystemExit from uvicorn on startup failure too
            # uvicorn already logged the real error; SystemExit(3) / "startup failed" say nothing
            cause = last.text or f"{type(e).__name__}: {e}"
            log.exception("local backend failed: %s", cause)
            self.state, self.error = "error", cause[:200]
        finally:
            logging.getLogger().removeHandler(last)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
