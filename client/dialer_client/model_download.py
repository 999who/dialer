"""How far the first-run download of the Parakeet model has got, for the loading card.

onnx-asr downloads the model with huggingface_hub and reports nothing, so this counts
the bytes already in the model's Hugging Face cache folder: finished files and the
*.incomplete file being written. The total comes from the Hugging Face API, or the
known size of the int8 model when that call fails.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger("model")

REPO = "istupakov/parakeet-tdt-0.6b-v3-onnx"   # onnx-asr's repo for nemo-parakeet-tdt-0.6b-v3
FILES = ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt", "config.json")
EXPECTED_BYTES = 670 * 1024 * 1024
MB = 1024 * 1024


def cache_dir() -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE) / ("models--" + REPO.replace("/", "--"))


class DownloadProgress:
    def __init__(self, folder: Path | None = None, fetch_total: bool = True):
        self.folder = folder or cache_dir()
        self.total = 0
        self.start_bytes = self.done_bytes()
        if fetch_total:
            threading.Thread(target=self._fetch_total, name="model-size", daemon=True).start()

    def _fetch_total(self) -> None:
        try:
            from huggingface_hub import HfApi

            info = HfApi().model_info(REPO, files_metadata=True, timeout=15)
            self.total = sum(s.size or 0 for s in info.siblings
                             if s.rfilename in FILES or s.rfilename.endswith(".int8.onnx.data"))
        except Exception as e:
            log.info("model size unknown (%s), assuming %d MB", e, EXPECTED_BYTES // MB)

    def done_bytes(self) -> int:
        """Bytes on disk. Symlinks are skipped: with symlinks the cache keeps each file once in
        blobs/, without them (Windows without developer mode) it moves the file to snapshots/."""
        n = 0
        try:
            for p in self.folder.rglob("*"):
                if not p.is_symlink() and p.is_file():
                    n += p.stat().st_size
        except OSError:
            pass
        return n

    def status(self) -> tuple[float, str] | None:
        """(0..1, "312 / 670 MB") while downloading; None when nothing is being downloaded
        (the model was already there and is only being loaded)."""
        total = self.total or EXPECTED_BYTES
        done = self.done_bytes()
        if done >= total * 0.98 or (done == self.start_bytes and self.start_bytes > 0):
            return None
        return min(done / total, 0.99), f"{done // MB} / {total // MB} MB"
