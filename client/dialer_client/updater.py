"""Self-update from the GitHub release "client-latest".

CI publishes every build of main as client-latest/EmanagerDialer.exe and stamps the exe with
the commit it was built from (_build.py). A newer release has a different commit.

Installing: Windows can't overwrite a running exe, but it can rename it. So the new file is
downloaded next to the exe, the running exe is renamed to *.old, the new one takes its name,
the app restarts, and the next start deletes the *.old file.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("update")

REPO = "999who/dialer"
TAG = "client-latest"
ASSET = "EmanagerDialer.exe"
API = f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}"

try:
    from ._build import BUILD  # written by CI before PyInstaller
except ImportError:  # running from source
    BUILD = ""


@dataclass
class Release:
    commit: str
    url: str
    size: int
    published: str

    @property
    def newer(self) -> bool:
        return bool(BUILD) and self.commit != BUILD


def version_label() -> str:
    return BUILD[:7] if BUILD else "wersja deweloperska"


def can_self_update() -> bool:
    return getattr(sys, "frozen", False) and bool(BUILD)


def fetch_release(timeout: float = 15) -> Release:
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "EMANAGER-Dialer"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    asset = next(a for a in data.get("assets", []) if a.get("name") == ASSET)
    return Release(data.get("target_commitish", ""), asset["browser_download_url"], int(asset.get("size", 0)),
                   asset.get("updated_at", ""))


def download(rel: Release, progress=None) -> Path:
    """Downloads next to the running exe (same disk, so the swap is a rename). progress(0..1)."""
    exe = Path(sys.executable)
    tmp = exe.with_name(exe.stem + ".download")
    req = urllib.request.Request(rel.url, headers={"User-Agent": "EMANAGER-Dialer"})
    done = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or rel.size or 0)
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    if rel.size and done != rel.size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"download incomplete: {done} of {rel.size} bytes")
    return tmp


def install(new_file: Path) -> None:
    """Swaps the running exe for new_file. The caller restarts the app afterwards."""
    exe = Path(sys.executable)
    old = exe.with_suffix(".old")
    old.unlink(missing_ok=True)
    os.replace(exe, old)          # allowed for a running exe on Windows
    try:
        os.replace(new_file, exe)
    except Exception:
        os.replace(old, exe)      # put the working version back
        raise


def cleanup() -> None:
    """Deletes the previous version left by install() (it was still running back then)."""
    if getattr(sys, "frozen", False):
        try:
            Path(sys.executable).with_suffix(".old").unlink(missing_ok=True)
        except OSError:
            pass
