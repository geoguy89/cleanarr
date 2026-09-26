"""Shared fixtures: an isolated config directory, database and cache per test."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

# Module-level paths in the app are read from the environment at import time.
_BOOT = Path(tempfile.mkdtemp(prefix="cleanarr-test-"))
os.environ.setdefault("CLEANARR_CONFIG", str(_BOOT))
os.environ.setdefault("CLEANARR_DB", str(_BOOT / "cleanarr.sqlite"))
os.environ.setdefault("CLEANARR_CACHE", str(_BOOT / "cache"))
os.environ.setdefault("CLEANARR_WEB", str(ROOT / "web"))

from cleanarr import config, db, media  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    skip = pytest.mark.skip(reason="ffmpeg/ffprobe not installed")
    for item in items:
        if "ffmpeg" in item.keywords and not HAVE_FFMPEG:
            item.add_marker(skip)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh /config: settings file, database and cache, all empty."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.yaml")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cleanarr.sqlite")
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(db, "_schema_ready", False)
    media.set_clean_title(media.DEFAULT_CLEAN_TITLE)
    yield tmp_path
    media.set_clean_title(media.DEFAULT_CLEAN_TITLE)


@pytest.fixture
def settings(home):
    """Saved settings that tests can change and save again."""
    s = config.Settings()
    config.save(s)
    return s


@pytest.fixture
def stub():
    from stubs import Stub
    server = Stub().start()
    yield server
    server.stop()


@pytest.fixture
def client(home, monkeypatch):
    """The API, with no worker thread running and the cache in the temp dir."""
    from fastapi.testclient import TestClient

    from cleanarr import main
    monkeypatch.setattr(main, "CACHE_DIR", home / "cache")
    (home / "cache").mkdir(exist_ok=True)
    return TestClient(main.app)
