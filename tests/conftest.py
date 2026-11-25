"""Keep tests independent of the running daemon's credentials."""
import os
import sys
from pathlib import Path

import pytest


os.environ.setdefault("GIDEON_GEMINI_KEYS_FILE", "")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")


def _guard_private_config(event, args):
    if event not in ("open", "os.listdir", "os.scandir") or not args:
        return
    path = args[0]
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    parts = Path(os.path.abspath(os.fsdecode(path))).parts
    if parts[:5] == ("/", "home", "cat", ".config", "gideon"):
        raise AssertionError("Tests must not access private daemon configuration")


sys.addaudithook(_guard_private_config)


@pytest.fixture(autouse=True)
def _isolate_default_key_file(monkeypatch, tmp_path):
    import decision_router

    monkeypatch.setattr(decision_router, "DEFAULT_KEYS_FILE", tmp_path / "absent-keys.txt")
