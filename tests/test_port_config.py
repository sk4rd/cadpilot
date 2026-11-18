import importlib.util
import pathlib
import sys
import types

import pytest

from cadpilot.freecad_client import FreeCADConnection, default_port

ADDON = (
    pathlib.Path(__file__).resolve().parents[1]
    / "addon"
    / "CADPilot"
    / "cadpilot_rpc"
    / "settings.py"
)


@pytest.fixture
def addon_settings(tmp_path, monkeypatch):
    fake = types.SimpleNamespace(
        getUserAppDataDir=lambda: str(tmp_path),
        Console=types.SimpleNamespace(PrintWarning=lambda *_: None, PrintError=lambda *_: None),
    )
    monkeypatch.setitem(sys.modules, "FreeCAD", fake)
    spec = importlib.util.spec_from_file_location("cadpilot_addon_settings", ADDON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_server_port_defaults_to_9875(monkeypatch):
    monkeypatch.delenv("CADPILOT_PORT", raising=False)
    assert default_port() == 9875


def test_server_port_from_environment(monkeypatch):
    monkeypatch.setenv("CADPILOT_PORT", "9876")
    assert default_port() == 9876


@pytest.mark.parametrize("raw", ["abc", "0", "70000", "-1"])
def test_server_ignores_an_invalid_port(monkeypatch, raw):
    monkeypatch.setenv("CADPILOT_PORT", raw)
    assert default_port() == 9875


def test_addon_port_from_settings(addon_settings, monkeypatch):
    monkeypatch.delenv("CADPILOT_PORT", raising=False)
    assert addon_settings.resolve_port({"port": 9877}) == 9877
    assert addon_settings.resolve_port() == 9875


def test_addon_environment_overrides_settings(addon_settings, monkeypatch):
    monkeypatch.setenv("CADPILOT_PORT", "9878")
    assert addon_settings.resolve_port({"port": 9877}) == 9878


def test_addon_ignores_an_invalid_environment_port(addon_settings, monkeypatch):
    monkeypatch.setenv("CADPILOT_PORT", "nope")
    assert addon_settings.resolve_port({"port": 9877}) == 9877


def test_client_port_follows_the_environment(monkeypatch):
    monkeypatch.setenv("CADPILOT_PORT", "9879")
    assert FreeCADConnection()._uri.endswith(":9879")
    assert FreeCADConnection(port=9880)._uri.endswith(":9880")


def test_late_completions_are_reported_once(monkeypatch):
    c = FreeCADConnection(port=9)
    monkeypatch.setattr(
        c, "_invoke_retrying", lambda *a: {"success": True, "late_completions": [{"request": 4}]}
    )
    assert c._invoke("ping")["late_completions"] == [{"request": 4}]
    assert c.take_late_completions() == [{"request": 4}]
    assert c.take_late_completions() == []
