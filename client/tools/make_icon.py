"""Renders the EMANAGER logo into assets/icon.ico (exe and taskbar icon).

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
pm = T.pixmap("logo_full", T.BRAND, 256, dpr=1.0)
buf = QBuffer()
buf.open(QIODevice.OpenModeFlag.WriteOnly)
pm.save(buf, "PNG")
img = Image.open(io.BytesIO(bytes(buf.data())))
out = ROOT / "assets" / "icon.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
