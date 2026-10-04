"""Run the commands emitted by installers against real temporary files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from installer import InstallerContext
from installer.install_cmd import _install_new_service, _print_install_summary


ROOT = Path(__file__).resolve().parents[1]


def test_manual_run_instructions_load_layered_config_and_quote_paths(tmp_path, capsys):
    app = tmp_path / 'app spaces $"quote'
    config = tmp_path / 'config spaces $"quote'
    (app / "venv/bin").mkdir(parents=True)
    (app / "venv/bin/python3").symlink_to(sys.executable)
    shutil.copy2(ROOT / "config_loader.py", app / "config_loader.py")
    (app / "mctomqtt.py").write_text(
        "import json\nfrom config_loader import load_config\n"
        "print(json.dumps(load_config()))\n"
    )
    (config / "config.d").mkdir(parents=True)
    (config / "config.toml").write_text('[general]\niata="XXX"\n')
    (config / "config.d/10-community.toml").write_text(
        '[[broker]]\nname="community"\nserver="mqtt.invalid"\n'
    )
    (config / "config.d/99-user.toml").write_text('[general]\niata="SEA"\n')
    ctx = InstallerContext(install_dir=str(app), config_dir=str(config), install_method="3")
    _install_new_service(ctx)
    install_output = capsys.readouterr().out
    _print_install_summary(ctx, False)
    summary_output = capsys.readouterr().out
    command = next(line.removeprefix("Manual run: ") for line in summary_output.splitlines()
                   if line.startswith("Manual run: "))
    assert command in install_output
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                            check=True, timeout=5)
    loaded = json.loads(result.stdout)
    assert loaded["general"]["iata"] == "SEA"
    assert loaded["broker"][0]["server"] == "mqtt.invalid"


@pytest.mark.parametrize("script", ["install.sh", "scripts/update.sh", "scripts/migrate.sh"])
def test_bootstrap_removes_temporary_directory_when_tmpdir_contains_spaces(script, tmp_path):
    temporary_parent = tmp_path / "temporary directory with spaces"
    temporary_parent.mkdir()
    environment = dict(os.environ, LOCAL_INSTALL=str(ROOT), TMPDIR=str(temporary_parent))
    for selector in ("MCTOMQTT_INSTALL_DIR", "MCTOMQTT_CONFIG_DIR"):
        environment.pop(selector, None)
    result = subprocess.run(["bash", str(ROOT / script), "--help"],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert list(temporary_parent.iterdir()) == []
