# PyInstaller recipe for EmanagerDialer.exe (one file, no console window).
#   pip install -r requirements.txt pyinstaller
#   pyinstaller EmanagerDialer.spec      ->  dist\EmanagerDialer.exe
# config.toml is read from (and created next to) the exe.
from pathlib import Path

HERE = Path(SPECPATH)
ICON = HERE / "assets" / "icon.ico"

a = Analysis(
    [str(HERE / "run.py")],
    pathex=[str(HERE)],
    datas=[(str(HERE / "assets"), "assets")],
    hiddenimports=["dialer_client.demo", "dialer_client.app"],
    excludes=["tkinter", "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtWebEngineCore", "PyQt6.QtMultimedia",
              "PyQt6.QtPdf", "PyQt6.Qt3DCore", "PyQt6.QtCharts", "PyQt6.QtBluetooth"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="EmanagerDialer",
    console=False,
    upx=False,
    icon=str(ICON) if ICON.exists() else None,
)
