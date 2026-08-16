from pathlib import Path

import atomcode_proxy.__main__ as main_module
from atomcode_proxy.config import Config, _resolve_working_dir
from atomcode_proxy.workdir import normalize_working_directory


def test_config_does_not_silently_fallback_to_user_home(monkeypatch):
    monkeypatch.delenv("ATOMCODE_PROXY_WORKDIR", raising=False)

    assert _resolve_working_dir() == ""

    cfg = Config(working_dir=_resolve_working_dir())
    assert cfg.working_dir == ""


def test_normalize_working_directory_requires_existing_directory(tmp_path):
    selected = normalize_working_directory(str(tmp_path))

    assert selected == str(Path(tmp_path).resolve())


def test_normalize_working_directory_rejects_missing_directory(tmp_path):
    assert normalize_working_directory(str(tmp_path / "missing")) is None


def test_startup_requires_selection_when_directory_is_missing(monkeypatch):
    monkeypatch.setattr(main_module, "choose_working_directory", lambda _initial: None)

    assert main_module._ensure_working_directory(Config(working_dir="")) is False
