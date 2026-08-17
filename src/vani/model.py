"""Fetching the wake-word model.

The Vosk small English model is ~40 MB and can't be shipped in the package, so
this downloads and unpacks it into ~/.local/share/vani on request. It is only
needed for wake words — hotkey dictation runs without it.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import paths


class ModelError(Exception):
    pass


def download(force: bool = False) -> Path:
    target = paths.model_dir()
    if target.is_dir() and not force:
        print(f"model already present at {target}")
        return target
    if target.is_dir():
        shutil.rmtree(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {paths.VOSK_MODEL_URL}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "model.zip"
        try:
            _fetch(paths.VOSK_MODEL_URL, archive)
        except (urllib.error.URLError, OSError) as exc:
            raise ModelError(f"download failed: {exc}") from None
        print("\nunpacking...")
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp)
        except zipfile.BadZipFile:
            raise ModelError("the downloaded file is not a valid zip") from None
        extracted = Path(tmp) / paths.VOSK_MODEL_NAME
        if not extracted.is_dir():
            raise ModelError(f"archive did not contain {paths.VOSK_MODEL_NAME}")
        shutil.move(str(extracted), str(target))
    print(f"model installed at {target}")
    return target


def _fetch(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with dest.open("wb") as out:
            while True:
                block = response.read(1 << 16)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if total and sys.stdout.isatty():
                    pct = 100 * done / total
                    print(f"\r  {done >> 20} / {total >> 20} MiB ({pct:.0f}%)",
                          end="", flush=True)
