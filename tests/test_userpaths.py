"""nagasim.settings.userpaths: user data directory resolution. Every test redirects
APPDATA/XDG_DATA_HOME to tmp_path -- never the real profile."""
import sys

import pytest

from nagasim.settings import userpaths


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv(userpaths.ENV_VAR, raising=False)


def _appdata(monkeypatch, tmp_path):
    """Windows resolution: %APPDATA%."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))


def test_user_data_dir_on_windows(tmp_path, monkeypatch):
    _appdata(monkeypatch, tmp_path)
    assert userpaths.user_data_dir() == tmp_path / "Naga"
    assert not (tmp_path / "Naga").exists()   # never created by a read-only call


def test_env_override_wins(tmp_path, monkeypatch):
    _appdata(monkeypatch, tmp_path)
    override = tmp_path / "elsewhere"
    monkeypatch.setenv(userpaths.ENV_VAR, str(override))

    assert userpaths.user_data_dir() == override
    assert not override.exists()          # never created by a read-only call


def test_xdg_data_home_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert userpaths.user_data_dir() == tmp_path / "Naga"
