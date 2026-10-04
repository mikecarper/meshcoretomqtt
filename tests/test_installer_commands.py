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


@pytest.mark.parametrize("selector", ["MCTOMQTT_INSTALL_DIR", "MCTOMQTT_CONFIG_DIR"])
@pytest.mark.parametrize("selection", ["/", "/tmp/../..", "root-link"])
def test_python_installer_rejects_filesystem_root_before_privilege_work(tmp_path, selector, selection):
    if selection == "root-link":
        link = tmp_path / "root-link"
        link.symlink_to("/", target_is_directory=True)
        selection = str(link)
    environment = dict(os.environ)
    for variable in ("MCTOMQTT_INSTALL_DIR", "MCTOMQTT_CONFIG_DIR"):
        environment.pop(variable, None)
    environment[selector] = selection
    # Stop at privilege checks if validation ever regresses, including when the
    # suite itself runs as root. No installation or service action can occur.
    script = '''
import sys
from installer.__main__ import main
from installer.system import require_root
def trace(frame, event, arg):
    if event == "call" and frame.f_code is require_root.__code__:
        raise AssertionError("unsafe directory reached privilege work")
    return trace
sys.argv = ["installer", "install"]
sys.settrace(trace)
main()
'''
    result = subprocess.run([sys.executable, "-c", script],
                            env=environment, cwd=ROOT, capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 2, result.stderr
    assert selector in result.stderr
    assert "filesystem root" in result.stderr
    assert "root privileges" not in result.stderr


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
                            capture_output=True, text=True, start_new_session=True, timeout=5)

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


def _uninstaller_environment(tmp_path, *, fail_backup=False, remove_files=False):
    binaries = tmp_path / "fake-bin"
    binaries.mkdir()
    commands = tmp_path / "uninstall-commands.jsonl"
    sudo = binaries / "sudo"
    sudo.write_text(
        f"#!{sys.executable}\nimport json, os, shutil, sys\n"
        "from pathlib import Path\n"
        "arguments=sys.argv[1:]\n"
        "with open(os.environ['COMMAND_RECORD'], 'a') as output:\n"
        "    output.write(json.dumps(arguments) + '\\n')\n"
        "if arguments[0] == 'cp': shutil.copyfile(arguments[1], arguments[2])\n"
        "if arguments[0] == 'cat':\n"
        "    if os.environ.get('FAIL_BACKUP'): raise SystemExit(1)\n"
        "    with open(arguments[-1], 'rb') as source: sys.stdout.buffer.write(source.read())\n"
        "if arguments[0] == 'rm' and os.environ.get('TEST_REMOVAL_ROOT'):\n"
        "    allowed_root = Path(os.environ['TEST_REMOVAL_ROOT']).resolve()\n"
        "    for argument in arguments[1:]:\n"
        "        if argument.startswith('-'): continue\n"
        "        target = Path(argument)\n"
        "        assert allowed_root in target.resolve().parents, 'removal outside test files'\n"
        "        if target.is_symlink() or target.is_file(): target.unlink()\n"
        "        elif target.is_dir(): shutil.rmtree(target)\n"
    )
    sudo.chmod(0o755)
    date = binaries / "date"
    date.write_text("#!/bin/sh\nprintf '%s\\n' fixed\n")
    date.chmod(0o755)
    environment = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}",
                       COMMAND_RECORD=str(commands),
                       MCTOMQTT_CONFIG_DIR=str(tmp_path / "unused-configuration"),
                       MCTOMQTT_INSTALL_DIR=str(tmp_path / "unused-application"))
    if fail_backup:
        environment["FAIL_BACKUP"] = "1"
    if remove_files:
        environment["TEST_REMOVAL_ROOT"] = str(tmp_path.resolve())
    return environment, commands


@pytest.mark.parametrize('placement', ['nested', 'same', 'config-alias', 'app-alias', 'sibling-prefix'])
def test_uninstaller_preserves_kept_configuration_inside_application_directory(tmp_path, placement):
    environment, commands = _uninstaller_environment(tmp_path, remove_files=True)
    app = tmp_path / 'application'
    app.mkdir()
    (app / 'mctomqtt.py').write_text('# installed application\n')
    config = app if placement == 'same' else (
        tmp_path / 'application settings' if placement == 'sibling-prefix' else app / 'private settings')
    (config / 'config.d').mkdir(parents=True)
    user_config = config / 'config.d/99-user.toml'
    contents = '[general]\niata="SEA"\n'
    user_config.write_text(contents)
    selected_app, selected_config = app, config
    if placement == 'config-alias':
        selected_config = tmp_path / 'selected config'
        selected_config.symlink_to(config, target_is_directory=True)
    elif placement == 'app-alias':
        selected_app = tmp_path / 'selected app'
        selected_app.symlink_to(app, target_is_directory=True)
    environment.update(MCTOMQTT_INSTALL_DIR=str(selected_app),
                       MCTOMQTT_CONFIG_DIR=str(selected_config),
                       TEST_SERVICE_FILE=str(tmp_path / 'missing.service'),
                       TEST_LAUNCHD_FILE=str(tmp_path / 'missing.plist'))
    docker = tmp_path / 'fake-bin/docker'
    docker.write_text('#!/bin/sh\nexit 1\n')
    docker.chmod(0o755)
    source = (ROOT / 'uninstall.sh').read_text()
    script = source[:source.index('# Run main')]
    script += '\nSYSTEMD_UNIT="$TEST_SERVICE_FILE"\nLAUNCHD_PLIST="$TEST_LAUNCHD_FILE"\nmain\n'
    result = subprocess.run(['bash', '-c', script], input='\ny\nn\nn\n',
                            env=environment, capture_output=True, text=True,
                            start_new_session=True, timeout=5)

    assert result.returncode == 0, result.stderr
    assert user_config.read_text() == contents
    keep_application = placement != 'sibling-prefix'
    assert app.is_dir() is keep_application
    if keep_application:
        assert 'Application and configuration directories kept' in result.stdout
        assert str(selected_app) in result.stdout
        assert str(selected_config) in result.stdout
        assert not commands.exists()
    else:
        logged = [json.loads(line) for line in commands.read_text().splitlines()]
        assert logged == [['rm', '-rf', '--', str(selected_app)]]


@pytest.mark.parametrize("existing_backup", ["file", "symlink"])
def test_uninstaller_backups_are_private_unique_and_do_not_replace_existing_paths(tmp_path, existing_backup):
    environment, commands = _uninstaller_environment(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    victim = tmp_path / "unrelated-file"
    victim.write_text("preserve unrelated contents")
    old_backup = home / "mctomqtt-user-toml-backup-fixed.toml"
    if existing_backup == "file":
        old_backup.write_text("preserve previous backup")
    else:
        old_backup.symlink_to(victim)
    original = old_backup.read_text()
    config = tmp_path / "config"
    (config / "config.d").mkdir(parents=True)
    user_toml = config / "config.d/99-user.toml"
    content = '[broker.auth]\npassword="private-backup-value"\n'
    user_toml.write_text(content)
    environment.update(HOME=str(home), MCTOMQTT_CONFIG_DIR=str(config))
    source = (ROOT / "uninstall.sh").read_text()
    script = source[:source.index("# Run main")] + "\nremove_config\nremove_config\n"

    result = subprocess.run(["bash", "-c", script], input="y\nn\ny\nn\n", env=environment,
                            capture_output=True, text=True, start_new_session=True, timeout=5)

    assert result.returncode == 0, result.stderr
    assert old_backup.read_text() == original
    assert victim.read_text() == "preserve unrelated contents"
    backups = [path for path in home.iterdir() if path != old_backup]
    assert len(backups) == 2
    assert all(path.read_text() == content for path in backups)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in backups)
    assert "private-backup-value" not in result.stdout + result.stderr
    assert not any(json.loads(line)[0] == "rm" for line in commands.read_text().splitlines())


def test_failed_uninstaller_backup_keeps_configuration_and_removes_partial_backup(tmp_path):
    environment, commands = _uninstaller_environment(tmp_path, fail_backup=True)
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    (config / "config.d").mkdir(parents=True)
    user_toml = config / "config.d/99-user.toml"
    user_toml.write_text("value=1\n")
    environment.update(HOME=str(home), MCTOMQTT_CONFIG_DIR=str(config))
    source = (ROOT / "uninstall.sh").read_text()
    script = source[:source.index("# Run main")] + "\nremove_config\n"

    result = subprocess.run(["bash", "-c", script], input="y\ny\n", env=environment,
                            capture_output=True, text=True, start_new_session=True, timeout=5)

    assert result.returncode != 0
    assert user_toml.read_text() == "value=1\n"
    assert list(home.iterdir()) == []
    assert not any(json.loads(line)[0] == "rm" for line in commands.read_text().splitlines())


@pytest.mark.parametrize("selected", ["relative", "-option", "/", "unrelated"])
def test_uninstaller_rejects_unsafe_application_paths_before_removing_services(tmp_path, selected):
    environment, commands = _uninstaller_environment(tmp_path)
    app = tmp_path / selected if selected != "/" else Path("/")
    if selected != "/":
        app.mkdir()
        if selected != "unrelated":
            (app / "mctomqtt.py").write_text("# application marker\n")
    chosen = str(app) if selected in ("/", "unrelated") else selected
    config = tmp_path / "config"
    config.mkdir()
    environment.update(MCTOMQTT_CONFIG_DIR=str(config))

    result = subprocess.run(["bash", str(ROOT / "uninstall.sh")], input=chosen + "\ny\ny\n",
                            env=environment, cwd=tmp_path, capture_output=True, text=True,
                            start_new_session=True, timeout=5)

    assert result.returncode != 0
    assert not commands.exists()
    assert "Removing Service" not in result.stdout


def test_uninstaller_eof_does_not_authorize_default_configuration_removal(tmp_path):
    environment, commands = _uninstaller_environment(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    environment["MCTOMQTT_CONFIG_DIR"] = str(config)
    source = (ROOT / "uninstall.sh").read_text()
    script = source[:source.index("# Run main")] + "\nremove_config\n"

    result = subprocess.run(["bash", "-c", script], input="", env=environment,
                            capture_output=True, text=True, start_new_session=True, timeout=5)

    assert result.returncode == 0, result.stderr
    assert not commands.exists()
    assert "Keeping configuration" in result.stdout


@pytest.mark.parametrize("selected", ["/", "/tmp/../..", "symlink-root"])
def test_uninstaller_rejects_configuration_directory_resolving_to_root(tmp_path, selected):
    environment, commands = _uninstaller_environment(tmp_path)
    config = selected
    if selected == "symlink-root":
        link = tmp_path / "config-link"
        link.symlink_to("/", target_is_directory=True)
        config = str(link)
    app = tmp_path / "app"
    app.mkdir()
    (app / "mctomqtt.py").write_text("# installed app\n")
    environment.update(MCTOMQTT_CONFIG_DIR=config, MCTOMQTT_INSTALL_DIR=str(app))

    result = subprocess.run(["bash", str(ROOT / "uninstall.sh")], input="\ny\ny\n",
                            env=environment, cwd=tmp_path, capture_output=True, text=True,
                            start_new_session=True, timeout=5)

    assert result.returncode != 0
    assert not commands.exists()
    assert "Removing Service" not in result.stdout


@pytest.mark.skipif(os.getuid() == 0, reason="root can traverse a mode-000 test directory")
def test_uninstaller_refuses_inaccessible_configuration_before_skipping_private_backup(tmp_path):
    environment, commands = _uninstaller_environment(tmp_path)
    config = tmp_path / "private-config"
    (config / "config.d").mkdir(parents=True)
    (config / "config.d/99-user.toml").write_text('password="private"\n')
    config.chmod(0o000)
    app = tmp_path / "app"
    app.mkdir()
    (app / "mctomqtt.py").write_text("# installed app\n")
    environment.update(MCTOMQTT_CONFIG_DIR=str(config), MCTOMQTT_INSTALL_DIR=str(app))
    try:
        result = subprocess.run(["bash", str(ROOT / "uninstall.sh")], input="\ny\ny\n",
                                env=environment, cwd=tmp_path, capture_output=True, text=True,
                                start_new_session=True, timeout=5)
        assert result.returncode != 0
        assert not commands.exists()
        assert "sudo" in result.stdout + result.stderr
    finally:
        config.chmod(0o700)


@pytest.mark.parametrize("exists", [False, True])
def test_uninstaller_matches_exact_docker_container_and_image_names(tmp_path, exists):
    environment, commands = _uninstaller_environment(tmp_path)
    docker_commands = tmp_path / "docker-commands.jsonl"
    docker = tmp_path / "fake-bin/docker"
    docker.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "arguments=sys.argv[1:]\n"
        "with open(os.environ['DOCKER_COMMAND_RECORD'], 'a') as output:\n"
        "    output.write(json.dumps(arguments) + '\\n')\n"
        "exists=os.environ.get('EXACT_CONTAINER') == '1'\n"
        "if arguments[0] == 'ps':\n"
        "    print('abc mctomqtt-preview:latest mctomqtt-old')\n"
        "    if exists: print('def image:latest mctomqtt')\n"
        "elif arguments[0] == 'images': print('mctomqtt-old latest')\n"
        "elif arguments[0] == 'inspect':\n"
        "    if not exists: raise SystemExit(1)\n"
        "    print('true' if '{{.State.Running}}' in arguments else '/mctomqtt')\n"
        "elif arguments[:2] == ['image', 'inspect']: raise SystemExit(1)\n"
    )
    docker.chmod(0o755)
    environment.update(DOCKER_COMMAND_RECORD=str(docker_commands),
                       EXACT_CONTAINER="1" if exists else "0")
    source = (ROOT / "uninstall.sh").read_text()
    script = source[:source.index("# Run main")]
    script += '\nprintf "Detected: %s\\n" "$(detect_system_type)"\nremove_docker\n'

    result = subprocess.run(["bash", "-c", script], input="y\n", env=environment,
                            capture_output=True, text=True, start_new_session=True, timeout=5)

    assert result.returncode == 0, result.stderr
    assert ("Detected: docker" in result.stdout) == exists
    logged = [json.loads(line) for line in docker_commands.read_text().splitlines()]
    mutations = [arguments for arguments in logged if arguments[0] in ("stop", "rm", "rmi")]
    assert mutations == ([["stop", "mctomqtt"], ["rm", "mctomqtt"]] if exists else [])


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX controlling terminals")
def test_piped_uninstaller_prompt_reads_controlling_terminal_instead_of_script_input(tmp_path):
    import fcntl
    import pty
    import select
    import termios

    master, slave = pty.openpty()
    def select_terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    source = (ROOT / "uninstall.sh").read_text()
    script = source[:source.index("# Run main")]
    script += ('\nprintf "PROMPT_READY\\n"\n'
               'if prompt_yes_no "Remove configuration?" "y"; then\n'
               '    printf "AUTHORIZED\\n"\nelse\n    printf "DECLINED\\n"\nfi\n')
    environment = dict(os.environ, MCTOMQTT_CONFIG_DIR=str(tmp_path / "unused-configuration"))
    process = subprocess.Popen(["bash", "-s"], stdin=subprocess.PIPE, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, preexec_fn=select_terminal, pass_fds=(slave,))
    os.close(slave)
    try:
        process.stdin.write(script)
        process.stdin.close()
        process.stdin = None
        assert select.select([process.stdout], [], [], 3)[0]
        assert process.stdout.readline() == "PROMPT_READY\n"
        os.write(master, b"n\n")
        output, errors = process.communicate(timeout=3)
        assert process.returncode == 0, errors
        assert "DECLINED" in output
        assert "AUTHORIZED" not in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
