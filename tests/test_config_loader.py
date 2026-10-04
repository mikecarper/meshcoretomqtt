"""Tests for config_loader: load_config, deep_merge, merge_broker_lists."""

from __future__ import annotations

import os
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest

from config_loader import _apply_override, load_config


@contextmanager
def config_directory(directory: Path):
    """Select a real config directory and restore the process environment."""
    name = "MCTOMQTT_CONFIG_DIR"
    previous = os.environ.get(name)
    try:
        os.environ[name] = str(directory)
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


class TestLoadConfigDirectory:
    def test_custom_directory_with_spaces_and_sorted_drop_ins(self, tmp_path: Path) -> None:
        directory = tmp_path / "custom config directory"
        overlays = directory / "config.d"
        overlays.mkdir(parents=True)
        (directory / "config.toml").write_text(
            '[general]\niata = "BAS"\nlog_level = "INFO"\n'
            '[[broker]]\nname = "local"\nenabled = true\n'
            'server = "base.example"\nport = 1883\n'
        )
        # Create the last override first: filenames, not creation order, win.
        (overlays / "99-user.toml").write_text(
            '[general]\niata = "USR"\n'
            '[[broker]]\nname = "local"\nenabled = false\n'
        )
        (overlays / "10-preset.toml").write_text(
            '[general]\niata = "PRE"\nlog_level = "WARNING"\n'
            '[[broker]]\nname = "local"\nport = 334\n'
        )
        with config_directory(directory):
            result = load_config()

        assert result["general"] == {"iata": "USR", "log_level": "WARNING"}
        assert result["broker"] == [{
            "name": "local", "enabled": False, "server": "base.example", "port": 334,
        }]

    def test_custom_drop_ins_load_without_base_file(self, tmp_path: Path) -> None:
        overlays = tmp_path / "config.d"
        overlays.mkdir()
        (overlays / "99-user.toml").write_text('[general]\niata = "USR"\n')
        with config_directory(tmp_path):
            assert load_config([]) == {"general": {"iata": "USR"}}

    def test_default_base_merges_repeated_broker_names(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text(
            '[[broker]]\nname = "local"\nserver = "mqtt.example"\n'
            '[[broker]]\nname = "local"\nport = 8883\n'
        )
        with config_directory(tmp_path):
            result = load_config()
        assert result["broker"] == [{"name": "local", "server": "mqtt.example", "port": 8883}]

    def test_explicit_paths_bypass_environment_base_and_drop_ins(self, tmp_path: Path) -> None:
        directory = tmp_path / "environment config"
        overlays = directory / "config.d"
        overlays.mkdir(parents=True)
        (directory / "config.toml").write_text('[general]\niata = "ENV"\nlog_level = "ERROR"\n')
        (overlays / "99-user.toml").write_text('[general]\niata = "USR"\n')
        explicit = tmp_path / "explicit.toml"
        explicit.write_text('[general]\niata = "ONE"\n')
        override = tmp_path / "explicit override.toml"
        override.write_text('[general]\niata = "TWO"\n')
        with config_directory(directory):
            result = load_config([str(explicit), str(override)])
        assert result == {"general": {"iata": "TWO"}}

    def test_missing_explicit_file_does_not_fall_back_to_environment(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text('[general]\niata = "ENV"\n')
        with config_directory(tmp_path):
            assert load_config([str(tmp_path / "missing.toml")]) == {}


class TestLoadConfigWithExplicitPaths:
    def test_single_config_file(self, tmp_path: Path) -> None:
        """A single --config file is loaded as the full config."""
        cfg = tmp_path / "my.toml"
        cfg.write_text('[general]\niata = "PDX"\n')

        result = load_config([str(cfg)])

        assert result["general"]["iata"] == "PDX"

    def test_multiple_config_files_overlay(self, tmp_path: Path) -> None:
        """Multiple --config files are merged in order."""
        base = tmp_path / "base.toml"
        base.write_text('[general]\niata = "SEA"\nlog_level = "INFO"\n')

        overlay = tmp_path / "overlay.toml"
        overlay.write_text('[general]\niata = "PDX"\n')

        result = load_config([str(base), str(overlay)])

        assert result["general"]["iata"] == "PDX"
        assert result["general"]["log_level"] == "INFO"

    def test_broker_overlay_merges_by_name(self, tmp_path: Path) -> None:
        """Broker lists in overlays merge by name, not append blindly."""
        base = tmp_path / "base.toml"
        base.write_text(
            '[[broker]]\nname = "letsmesh-us"\nenabled = true\n'
            'server = "mqtt-us-v1.letsmesh.net"\n'
        )

        overlay = tmp_path / "overlay.toml"
        overlay.write_text(
            '[[broker]]\nname = "letsmesh-us"\nenabled = false\n'
        )

        result = load_config([str(base), str(overlay)])

        assert len(result["broker"]) == 1
        assert result["broker"][0]["enabled"] is False
        assert result["broker"][0]["server"] == "mqtt-us-v1.letsmesh.net"

    @pytest.mark.parametrize("with_base_broker", [False, True])
    def test_repeated_new_broker_name_merges_within_overlay(self, tmp_path: Path,
                                                          with_base_broker: bool) -> None:
        base = tmp_path / "base.toml"
        base.write_text('[[broker]]\nname = "existing"\n' if with_base_broker else '')
        overlay = tmp_path / "overlay.toml"
        overlay.write_text(
            '[[broker]]\nname = "new"\nserver = "mqtt.example"\n'
            '[broker.tls]\nenabled = true\nca_certs = "ca.pem"\n'
            '[[broker]]\nname = "new"\nport = 8883\n'
            '[broker.tls]\ninsecure = false\n'
        )

        result = load_config([str(base), str(overlay)])

        assert len(result["broker"]) == 1 + int(with_base_broker)
        assert result["broker"][-1] == {
            "name": "new", "server": "mqtt.example", "port": 8883,
            "tls": {"enabled": True, "ca_certs": "ca.pem", "insecure": False},
        }

    def test_broker_overlay_can_be_reused_without_losing_brokers(self) -> None:
        overlay = {"general": {"iata": "PDX"}, "broker": [{"name": "shared"}]}
        first = _apply_override({}, overlay)
        second = _apply_override({}, overlay)

        assert first == second == overlay

    def test_skips_default_config_d(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When --config is provided, config.d directories are not loaded."""
        # Create a config.d next to config_loader.py's __file__ location
        # that would normally be picked up
        cfg = tmp_path / "my.toml"
        cfg.write_text('[general]\niata = "PDX"\n')

        config_d = tmp_path / "config.d"
        config_d.mkdir()
        (config_d / "99-extra.toml").write_text('[general]\niata = "OVERWRITTEN"\n')

        # Even though config.d exists, --config should bypass it entirely
        result = load_config([str(cfg)])

        assert result["general"]["iata"] == "PDX"

    def test_missing_file_skipped(self, tmp_path: Path) -> None:
        """A nonexistent --config path is skipped with a log error."""
        cfg = tmp_path / "exists.toml"
        cfg.write_text('[general]\niata = "SEA"\n')

        result = load_config(["/nonexistent/path.toml", str(cfg)])

        assert result["general"]["iata"] == "SEA"

    def test_no_config_paths_returns_defaults(self) -> None:
        """Passing None uses default config loading."""
        # Just verify it doesn't crash; actual paths may or may not exist
        result = load_config(None)
        assert isinstance(result, dict)
