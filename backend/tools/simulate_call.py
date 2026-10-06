"""Drive the backend without a softphone.

Scripted dialogue (needs only GEMINI_API_KEY on the server, any STT_ENGINE):
    python -m tools.simulate_call --script tools/demo_dialogue.txt

Stereo WAV (L = operator, R = client), real-time paced, exercises VAD + Parakeet:
    python -m tools.simulate_call --wav call.wav
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave

import numpy as np
import websockets


async def reader(ws, t0):
    async for msg in ws:
        d = json.loads(msg)
        stamp = f"{time.monotonic() - t0:6.2f}s"
        if d["type"] == "transcript":
            print(f"{stamp}  {d['speaker']:>8}: {d['text']}")
        elif d["type"] == "hint":
            print(f"{stamp}  >>> HINT [{d['category']}/{d['topic']}] {d['hint']}  ({d['latency_ms']} ms, "
                  f"match={d['match']})")
        elif d["type"] != "latency":
            print(f"{stamp}  {d}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8000/ws")
    ap.add_argument("--token", default="change-me")
    ap.add_argument("--script")
    ap.add_argument("--wav")
    ap.add_argument("--pause", type=float, default=2.5, help="seconds between scripted lines")
    a = ap.parse_args()

    async with websockets.connect(a.url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "token": a.token, "agent_id": "simulator",
                                  "sample_rate": 16000, "channels": 2, "format": "s16le"}))
        t0 = time.monotonic()
        task = asyncio.create_task(reader(ws, t0))
        await ws.send(json.dumps({"type": "call_start", "phone": "+48 *** sim"}))
        if a.script:
            for line in open(a.script, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                who, text = line.split(":", 1)
                speaker = "operator" if who.strip().lower() in ("o", "operator", "konsultant") else "client"
                await ws.send(json.dumps({"type": "text", "speaker": speaker, "text": text.strip()}))
                await asyncio.sleep(a.pause)
        if a.wav:
            with wave.open(a.wav) as w:
                assert w.getnchannels() == 2 and w.getsampwidth() == 2 and w.getframerate() == 16000, \
                    "need 16 kHz stereo 16-bit WAV (ffmpeg -i in.wav -ar 16000 -ac 2 out.wav)"
                step = 1600  # 100 ms
                while True:
                    frames = w.readframes(step)
                    if not frames:
                        break
                    await ws.send(frames)
                    await asyncio.sleep(0.1)
            await ws.send(np.zeros(16000 * 2, dtype="<i2").tobytes())  # trailing silence closes last utterance
            await asyncio.sleep(3)
        await ws.send(json.dumps({"type": "call_end"}))
        await asyncio.sleep(8)
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
