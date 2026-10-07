# PyInstaller recipe for EmanagerDialer.exe (one file, no console window).
#   pip install -r requirements.txt pyinstaller
#   pyinstaller EmanagerDialer.spec      ->  dist\EmanagerDialer.exe
# config.toml is read from (and created next to) the exe.
# The backend (backend/app) is bundled too, for the built-in "everything on this PC" mode.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

HERE = Path(SPECPATH)
REPO = HERE.parent
ICON = HERE / "assets" / "icon.ico"

a = Analysis(
    [str(HERE / "run.py")],
    pathex=[str(HERE), str(REPO / "backend")],
    datas=[
        (str(HERE / "assets"), "assets"),
        (str(REPO / "backend" / "models"), "models"),     # silero_vad.onnx
        (str(REPO / "prompts"), "prompts"),                # master_prompt_pl.md
        *collect_data_files("onnx_asr"),                   # its preprocessor ONNX files
        # packages that read their own version via importlib.metadata at import time
        *copy_metadata("onnx-asr"), *copy_metadata("onnxruntime"), *copy_metadata("huggingface_hub"),
        *copy_metadata("google-genai"),
    ],
    hiddenimports=[
        "dialer_client.demo", "dialer_client.app", "dialer_client.selftest", "dialer_client._build",
        "app.main", "app.config", "app.stt", "app.llm", "app.session", "app.rag_store", "app.langfilter",
        *collect_submodules("uvicorn"), *collect_submodules("websockets"), *collect_submodules("onnx_asr"),
    ],
    excludes=["tkinter", "torch", "sentence_transformers", "PyQt6.QtQml", "PyQt6.QtQuick",
              "PyQt6.QtWebEngineCore", "PyQt6.QtMultimedia", "PyQt6.QtPdf", "PyQt6.Qt3DCore", "PyQt6.QtCharts",
              "PyQt6.QtBluetooth"],
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
