"""Import probes never access the daemon's private configuration directory."""
from pathlib import Path
import runpy


def _import_without_private_io(monkeypatch):
    original = Path.is_file

    def guarded_is_file(path):
        if path.parts[:5] == ("/", "home", "cat", ".config", "gideon"):
            raise AssertionError("Import attempted private configuration I/O")
        return original(path)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    return runpy.run_path(str(Path(__file__).parents[1] / "gideon.py"), run_name="import_probe")


def test_import_does_not_load_daemon_credentials(monkeypatch):
    module = _import_without_private_io(monkeypatch)
    assert "main" in module


def test_startup_applies_settings_loaded_after_import(monkeypatch):
    module = _import_without_private_io(monkeypatch)
    configure = module["_configure_runtime"]

    def load_settings():
        monkeypatch.setenv("GIDEON_ENABLE_HUD", "1")
        monkeypatch.setenv("GIDEON_WAKE_THRESHOLD", "0.42")

    monkeypatch.setitem(configure.__globals__, "_load_gideon_env", load_settings)
    configure()
    assert configure.__globals__["HUD_ENABLED"] is True
    assert configure.__globals__["WAKE_THRESHOLD"] == 0.42
