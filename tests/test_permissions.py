"""Tier 4: Tests for installer.system.set_permissions with real sudo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from installer.system import set_permissions

pytestmark = pytest.mark.system


class TestSetPermissions:
    @pytest.fixture()
    def dirs(self, tmp_path: Path) -> tuple[Path, Path]:
        install_dir = tmp_path / "opt" / "mctomqtt"
        config_dir = tmp_path / "etc" / "mctomqtt"
        config_d = config_dir / "config.d"
        install_dir.mkdir(parents=True)
        config_dir.mkdir(parents=True)
        config_d.mkdir()
        # Create test files
        (config_dir / "config.toml").write_text("[general]\n")
        (config_d / "99-user.toml").write_text("[general]\niata = \"SEA\"\n")
        return install_dir, config_dir

    def test_set_permissions_ownership(self, dirs: tuple[Path, Path]) -> None:
        install_dir, config_dir = dirs
        # Use current user for testing
        current_user = os.environ.get("USER", "root")
        set_permissions(str(install_dir), str(config_dir), current_user)

        # Verify install_dir is owned by current user
        stat = os.stat(install_dir)
        assert stat.st_uid == os.getuid()

    def test_set_permissions_config_mode(self, dirs: tuple[Path, Path]) -> None:
        install_dir, config_dir = dirs
        current_user = os.environ.get("USER", "root")
        set_permissions(str(install_dir), str(config_dir), current_user)

        # Verify config_dir mode is 750
        mode = oct(os.stat(config_dir).st_mode)[-3:]
        assert mode == "750"
        assert os.stat(config_dir / "config.d").st_mode & 0o777 == 0o750

    def test_set_permissions_config_file_mode(self, dirs: tuple[Path, Path]) -> None:
        install_dir, config_dir = dirs
        current_user = os.environ.get("USER", "root")
        set_permissions(str(install_dir), str(config_dir), current_user)

        # Verify config.toml mode is 640
        config_toml = config_dir / "config.toml"
        mode = oct(os.stat(config_toml).st_mode)[-3:]
        assert mode == "640"
        assert os.stat(config_dir / "config.d" / "99-user.toml").st_mode & 0o777 == 0o640

    @pytest.mark.parametrize("name", ["config.toml", "config.d/99-user.toml", "config.d/99-backup.toml.backup"])
    def test_config_links_do_not_change_external_file_permissions(self, dirs, tmp_path, name):
        install_dir, config_dir = dirs
        external = tmp_path / "external-config"
        external.write_text("unrelated file")
        external.chmod(0o600)
        before = external.stat()
        link = config_dir / name
        link.unlink(missing_ok=True)
        link.symlink_to(external)

        set_permissions(str(install_dir), str(config_dir), "root")

        after = external.stat()
        assert (after.st_uid, after.st_gid, after.st_mode) == (
            before.st_uid, before.st_gid, before.st_mode)
