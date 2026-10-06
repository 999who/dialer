"""`EmanagerDialer.exe --selftest`: checks the bundled backend without UI or audio (used by CI).

Starts the built-in backend with speech recognition off, waits until it accepts a WebSocket
"hello", and checks that the pieces local mode needs are inside the build: the Silero VAD
model, the master prompt and onnx-asr. Exit code 0 = OK; details go to the log.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

log = logging.getLogger("selftest")


def main() -> int:
    from PyQt6.QtCore import QCoreApplication

    from .app import _setup_logging
    from .config import Config
    from .local_server import PROMPT, LocalServer

    QCoreApplication.setOrganizationName("EMANAGER")
    QCoreApplication.setApplicationName("EMANAGER Dialer")
    log.info("log: %s", _setup_logging())
    try:
        srv = LocalServer(Config(gemini_api_key="", local_stt="none", agent_id="selftest"))
        srv.start()
        t0 = time.monotonic()
        while srv.state == "loading" and time.monotonic() - t0 < 120:
            time.sleep(0.2)
        assert srv.state == "ready", f"backend state {srv.state}: {srv.error}"

        async def hello():
            import websockets

            async with websockets.connect(srv.url) as ws:
                await ws.send(json.dumps({"type": "hello", "token": srv.token, "agent_id": "selftest",
                                          "sample_rate": 16000, "channels": 2, "format": "s16le"}))
                return json.loads(await asyncio.wait_for(ws.recv(), 10))

        msg = asyncio.run(hello())
        assert msg.get("type") == "ready", msg

        import onnx_asr  # noqa: F401  (Parakeet loader is bundled)
        from app.stt import _SileroVAD

        _SileroVAD().prob(__import__("numpy").zeros(512, dtype="float32"))
        assert PROMPT.exists(), f"missing {PROMPT}"
        log.info("SELFTEST OK")
        srv.stop()
        return 0
    except BaseException:
        log.exception("SELFTEST FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
