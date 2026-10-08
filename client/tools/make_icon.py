"""Renders the EMANAGER app tile (red logo on a dark tile) into assets/icon.ico (exe and taskbar icon).

    python tools/make_icon.py      # needs PyQt6 and Pillow
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from PyQt6.QtCore import QBuffer, QIODevice  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402

from dialer_client import theme as T  # noqa: E402

app = QGuiApplication(sys.argv)
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def png(size: int) -> Image.Image:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    T.app_tile(size).save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


# each size drawn on its own (the small ones use the simpler marks), not scaled down from 256
imgs = [png(s) for s in SIZES]
out = ROOT / "assets" / "icon.ico"
imgs[-1].save(out, sizes=[(s, s) for s in SIZES], append_images=imgs[:-1])
print(out)
