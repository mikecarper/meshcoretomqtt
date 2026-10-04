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


def test_uninstaller_uses_selected_paths_without_printing_config_credentials(tmp_path):
    app = tmp_path / "app spaces"
    config = tmp_path / "config spaces"
    app.mkdir()
    (config / "config.d").mkdir(parents=True)
    (config / "config.toml").write_text('[general]\niata="SEA"\n')
    secret = "uninstaller-must-not-display-this-password"
    user_toml = config / "config.d/99-user.toml"
    user_toml.write_text(f'[[broker]]\nname="test"\n[broker.auth]\npassword="{secret}"\n')
    commands = tmp_path / "commands.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['COMMAND_RECORD'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    sudo.chmod(0o755)
    source = (ROOT / "uninstall.sh").read_text()
    script = source[:source.index("# Run main")]
    script += '\nprintf "Selected app: %s\\n" "$DEFAULT_APP_DIR"\nremove_config\n'
    environment = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                       COMMAND_RECORD=str(commands),
                       MCTOMQTT_INSTALL_DIR=str(app), MCTOMQTT_CONFIG_DIR=str(config))

    result = subprocess.run(["bash", "-c", script], input="n\ny\n", env=environment,
                            capture_output=True, text=True, timeout=5)

    # No backup, then agree to remove only the selected configuration.
    # Prompt responses are supplied separately from the script stdin.
    assert result.returncode == 0, result.stderr
    assert str(app) in result.stdout
    assert str(user_toml) in result.stdout
    assert secret not in result.stdout + result.stderr
    logged = [json.loads(line) for line in commands.read_text().splitlines()]
    assert logged == [["rm", "-f", str(config / "config.toml")],
                      ["rm", "-rf", str(config / "config.d")],
                      ["rm", "-rf", str(config)]]
    assert user_toml.is_file()  # The fake sudo never performs removals.


@pytest.mark.parametrize("selector", ["MCTOMQTT_INSTALL_DIR", "MCTOMQTT_CONFIG_DIR"])
def test_uninstaller_rejects_relative_directory_selectors_before_prompting(selector):
    environment = dict(os.environ, **{selector: "relative/path"})
    result = subprocess.run(["bash", str(ROOT / "uninstall.sh")], env=environment,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert f"{selector} must be an absolute path" in result.stderr
    assert "This will remove" not in result.stdout
