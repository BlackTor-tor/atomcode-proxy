from pathlib import Path

import atomcode_proxy.__main__ as main_module
import atomcode_proxy.config as config_module
from atomcode_proxy.config import Config, _resolve_working_dir, read_user_config, write_user_config
from atomcode_proxy.workdir import normalize_working_directory


def test_config_does_not_silently_fallback_to_user_home(monkeypatch):
    monkeypatch.delenv("ATOMCODE_PROXY_WORKDIR", raising=False)
    monkeypatch.setattr(config_module, "_user_config", {})

    assert _resolve_working_dir() == ""

    cfg = Config(working_dir=_resolve_working_dir())
    assert cfg.working_dir == ""


def test_normalize_working_directory_requires_existing_directory(tmp_path):
    selected = normalize_working_directory(str(tmp_path))

    assert selected == str(Path(tmp_path).resolve())


def test_normalize_working_directory_rejects_missing_directory(tmp_path):
    assert normalize_working_directory(str(tmp_path / "missing")) is None


def test_startup_falls_back_to_user_home_when_directory_is_missing():
    cfg = Config(working_dir="")

    main_module._ensure_working_directory(cfg)

    assert cfg.working_dir == str(Path.home())


def test_startup_keeps_configured_directory(tmp_path):
    cfg = Config(working_dir=str(tmp_path))

    main_module._ensure_working_directory(cfg)

    assert cfg.working_dir == str(Path(tmp_path).resolve())


def test_user_config_roundtrip_and_empty_value_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "user_config_path", lambda: tmp_path / "config.json")

    write_user_config({"ATOMCODE_PROXY_WORKDIR": str(tmp_path), "ATOMCODE_DEFAULT_PROVIDER": "p1"})
    stored = read_user_config()
    assert stored["ATOMCODE_PROXY_WORKDIR"] == str(tmp_path)
    assert stored["ATOMCODE_DEFAULT_PROVIDER"] == "p1"

    # 空值表示删除该项
    write_user_config({"ATOMCODE_DEFAULT_PROVIDER": ""})
    stored = read_user_config()
    assert "ATOMCODE_DEFAULT_PROVIDER" not in stored
    assert stored["ATOMCODE_PROXY_WORKDIR"] == str(tmp_path)


def test_read_user_config_ignores_unknown_keys_and_corrupt_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "user_config_path", lambda: cfg_path)

    cfg_path.write_text('{"ATOMCODE_PROXY_WORKDIR": "/w", "EVIL_KEY": "x"}', encoding="utf-8")
    assert read_user_config() == {"ATOMCODE_PROXY_WORKDIR": "/w"}

    cfg_path.write_text("{not json", encoding="utf-8")
    assert read_user_config() == {}


def test_config_resolves_user_config_over_defaults(monkeypatch):
    monkeypatch.setattr(config_module, "_user_config", {"ATOMCODE_DEFAULT_PROVIDER": "from-json"})
    monkeypatch.delenv("ATOMCODE_DEFAULT_PROVIDER", raising=False)

    cfg = Config.from_env()

    assert cfg.default_provider == "from-json"
