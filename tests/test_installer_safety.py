"""Real-file and subprocess regressions for installer safety boundaries."""
from __future__ import annotations

import http.server
import grp
import inspect
import json
import os
import pathlib
import plistlib
import pwd
import shutil
import subprocess
import sys
import threading
import tomllib

import pytest

from installer import InstallerContext
from installer.config import _replace_owner_fields, _toml_dumps, toml_escape, write_private_config
from installer.install_cmd import load_config_url
from installer.migrate_cmd import prepare_migrated_config, run_migrate
from installer.system import (
    chown_recursive,
    docker_run_command, install_launchd_service, install_systemd_service,
    render_launchd_plist, render_systemd_template, stage_dockerfile,
    set_permissions,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_recursive_ownership_accepts_dangling_symlinks_without_following_them(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    link = app / "missing-interpreter"
    link.symlink_to(tmp_path / "missing-target")

    chown_recursive(str(app), pwd.getpwuid(os.getuid()).pw_name,
                    grp.getgrgid(os.getgid()).gr_name)

    assert link.is_symlink()
    assert not link.exists()


@pytest.mark.parametrize("kind", ["file", "directory", "root-directory"])
def test_recursive_ownership_changes_links_without_changing_external_targets(tmp_path, kind):
    groups = set(os.getgroups()) - {os.getgid()}
    if os.getuid() == 0:
        groups.update(entry.gr_gid for entry in grp.getgrall() if entry.gr_gid != os.getgid())
    if not groups:
        pytest.skip("requires root or a supplementary group to observe an ownership change")
    selected_group = min(groups)
    external = tmp_path / "outside"
    external.mkdir()
    target = external / "host-python"
    target.write_text("host executable must keep its ownership")
    original = {path: (path.stat().st_uid, path.stat().st_gid)
                for path in (external, target)}
    app = tmp_path / "app"
    if kind == "root-directory":
        app.symlink_to(external, target_is_directory=True)
        link = app
    else:
        app.mkdir()
        link = app / "linked-target"
        link.symlink_to(target if kind == "file" else external,
                       target_is_directory=kind == "directory")

    chown_recursive(str(app), pwd.getpwuid(os.getuid()).pw_name,
                    grp.getgrgid(selected_group).gr_name)

    assert link.lstat().st_gid == selected_group
    assert {path: (path.stat().st_uid, path.stat().st_gid)
            for path in (external, target)} == original


@pytest.mark.parametrize("kind", ["config-root", "config-drop-ins"])
def test_config_directory_links_are_rejected_before_changing_permissions(tmp_path, kind):
    app = tmp_path / "app"
    app.mkdir()
    external = tmp_path / "outside"
    external.mkdir()
    original = external.stat()
    config = tmp_path / "config"
    if kind == "config-root":
        config.symlink_to(external, target_is_directory=True)
    else:
        config.mkdir()
        (config / "config.d").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="Configuration directories must not be symlinks"):
        set_permissions(str(app), str(config), pwd.getpwuid(os.getuid()).pw_name)

    after = external.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (
        original.st_uid, original.st_gid, original.st_mode)



@pytest.mark.parametrize("script", ["install.sh", "scripts/update.sh", "scripts/migrate.sh"])
def test_bootstrap_retains_original_arguments_and_only_selected_environment(script, tmp_path):
    source = (ROOT / script).read_text()
    prefix = source[:source.index("# Ensure running as root")]
    args = ["--repo", "owner/custom-repo", "--branch", "fix/new branch",
            "--config", "https://example.invalid/config?a=1&b=2", "--update"]
    environment = dict(os.environ, LOCAL_INSTALL="/chosen source", INSTALL_REBUILD_VENV="1",
                       MCTOMQTT_REPO="owner/repo", MCTOMQTT_BRANCH="chosen",
                       MCTOMQTT_INSTALL_DIR=str(tmp_path / "app"),
                       MCTOMQTT_CONFIG_DIR=str(tmp_path / "config"),
                       UNRELATED_SECRET="not-forwarded")
    prefix += '\nprintf "%s\\0" "${ORIGINAL_ARGS[@]}" "--ENV--" "${BOOTSTRAP_ENV[@]}"\n'
    result = subprocess.run(["bash", "-s", "--", *args], input=prefix.encode(),
                            capture_output=True, env=environment, check=True)
    records = result.stdout.decode().rstrip("\0").split("\0")
    boundary = records.index("--ENV--")
    assert records[:boundary] == args
    forwarded = dict(item.split("=", 1) for item in records[boundary + 1:])
    assert set(forwarded) == {"LOCAL_INSTALL", "INSTALL_REBUILD_VENV", "MCTOMQTT_REPO",
                              "MCTOMQTT_BRANCH", "MCTOMQTT_INSTALL_DIR", "MCTOMQTT_CONFIG_DIR"}
    assert forwarded == {key: environment[key] for key in forwarded}
    assert 'exec sudo env "${BOOTSTRAP_ENV[@]}" bash "$0" "${ORIGINAL_ARGS[@]}"' in source
    assert "sudo -E" not in source


@pytest.mark.parametrize("value", ['quote"slash\\', "line1\nline2\r\t\b\f", "\0\x01\x1f\x7f"])
def test_toml_string_controls_round_trip(value):
    assert tomllib.loads('value="' + toml_escape(value) + '"')['value'] == value
    data = {"broker": [{"name": "custom", "auth": {"method": "password", "password": value}}]}
    assert tomllib.loads(_toml_dumps(data)) == data


def test_owner_updates_escape_values_and_preserve_preset_overrides(tmp_path):
    dest = tmp_path / "99-user.toml"
    content = ('[[broker]]\nname="custom"\n[broker.auth]\nmethod="token"\n'
               'owner="OLD"\nemail="old@example.com"\n'
               '[[broker]]\nname="preset"\n[broker.auth]\n'
               'owner="PRESET"\nemail="preset@example.com"\n')
    owner = 'new"owner\\1\nnext'
    email = 'quote"slash\\@example.com'
    write_private_config(dest, _replace_owner_fields(content, owner, email))
    brokers = tomllib.loads(dest.read_text())["broker"]
    assert brokers[0]["auth"] == {"method": "token", "owner": owner, "email": email}
    assert brokers[1]["auth"] == {"owner": "PRESET", "email": "preset@example.com"}
    assert dest.stat().st_mode & 0o777 == 0o640


def test_interactive_owner_update_preserves_new_preset_settings(tmp_path):
    config_d = tmp_path / "config.d"
    config_d.mkdir()
    (config_d / "10-community.toml").write_text(
        '[[broker]]\nname="community"\nserver="mqtt.invalid"\n[broker.auth]\nmethod="token"\n')
    user = config_d / "99-user.toml"
    user.write_text('[[broker]]\nname="custom"\n[broker.auth]\nmethod="token"\n'
                    'owner="' + "C" * 64 + '"\nemail="old@example.com"\n')
    email = 'quote"slash\\@example.com'
    inputs = "\n".join(["A" * 64, "preset@example.com", "n", "B" * 64, email, "n", ""])
    result = subprocess.run([sys.executable, "-c",
                            "from installer.config import update_owner_info; "
                            "import sys; update_owner_info(sys.argv[1])", str(tmp_path)],
                            input=inputs, capture_output=True, text=True, cwd=ROOT,
                            start_new_session=True, timeout=5)
    assert result.returncode == 0, result.stderr
    brokers = {broker["name"]: broker["auth"]
               for broker in tomllib.loads(user.read_text())["broker"]}
    assert brokers["community"]["owner"] == "A" * 64
    assert brokers["community"]["email"] == "preset@example.com"
    assert brokers["custom"]["owner"] == "B" * 64
    assert brokers["custom"]["email"] == email


def test_private_config_replacement_is_atomic_validated_and_preserves_ownership(tmp_path):
    dest = tmp_path / "99-user.toml"
    dest.write_text('[general]\niata="OLD"\n')
    before = dest.stat()
    write_private_config(dest, '[general]\niata="SEA"\n')
    after = dest.stat()
    assert after.st_ino != before.st_ino
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_mode & 0o777 == 0o640
    saved = dest.read_bytes()
    with pytest.raises(tomllib.TOMLDecodeError):
        write_private_config(dest, 'broken = "\n')
    assert dest.read_bytes() == saved
    assert not list(tmp_path.glob(".99-user.toml.*"))
    with pytest.raises(FileExistsError):
        write_private_config(dest, "", overwrite=False)
    assert dest.read_bytes() == saved


def test_private_config_does_not_follow_symlink(tmp_path):
    victim = tmp_path / "unrelated"
    victim.write_text("keep this")
    target = tmp_path / "99-user.toml"
    target.symlink_to(victim)
    with pytest.raises(ValueError):
        write_private_config(target, '[general]\niata="SEA"\n')
    assert victim.read_text() == "keep this"
    assert target.is_symlink()


def _legacy_settings(password='p"ass\\word\nnext'):
    return {"MCTOMQTT_MQTT1_ENABLED": "true", "MCTOMQTT_MQTT1_SERVER": "mqtt.invalid",
            "MCTOMQTT_MQTT1_USERNAME": "user", "MCTOMQTT_MQTT1_PASSWORD": password}


def test_migration_preparation_is_valid_private_and_precedes_service_retirement(tmp_path):
    settings = _legacy_settings()
    dest = prepare_migrated_config(settings, str(tmp_path))
    assert dest is not None
    parsed = tomllib.loads(dest.read_text())
    assert parsed["broker"][0]["auth"]["password"] == settings["MCTOMQTT_MQTT1_PASSWORD"]
    assert dest.stat().st_mode & 0o777 == 0o640
    source = inspect.getsource(run_migrate)
    assert source.index("prepare_migrated_config(") < source.index("_stop_old_services(")
    assert source.index("prepare_migrated_config(") < source.index("_cleanup_old_service_units(")
    assert 'print(content)' not in source


def test_migration_keeps_required_broker_settings_that_match_legacy_defaults(tmp_path):
    if pathlib.Path("/etc/systemd/system/mctomqtt.service").exists():
        pytest.skip("isolated migration test does not retire a real installed unit")
    home = tmp_path / "home"
    legacy = home / ".meshcoretomqtt"
    legacy.mkdir(parents=True)
    (legacy / "mctomqtt.py").write_text("# legacy bridge\n")
    settings = (
        'MCTOMQTT_IATA=SEA\nMCTOMQTT_SERIAL_PORTS=/dev/ttyUSB7\n'
        'MCTOMQTT_MQTT1_ENABLED=true\nMCTOMQTT_MQTT1_SERVER=mqtt.invalid\n'
        'MCTOMQTT_MQTT1_PORT=443\nMCTOMQTT_MQTT1_TRANSPORT=websockets\n'
        'MCTOMQTT_MQTT1_USE_TLS=true\nMCTOMQTT_MQTT1_USE_AUTH_TOKEN=true\n'
    )
    (legacy / ".env").write_text(settings)
    (legacy / ".env.local").write_text('MCTOMQTT_MQTT1_TOKEN_EMAIL=owner@example.com\n')
    selected = tmp_path / "selected-source"
    selected.mkdir()
    (selected / ".env").write_text(settings)
    config = tmp_path / "config"
    environment = dict(os.environ, HOME=str(home))
    environment.pop("SUDO_USER", None)
    script = (
        "import sys\nfrom installer import InstallerContext\n"
        "from installer.migrate_cmd import run_migrate\n"
        "ctx=InstallerContext(install_dir=sys.argv[1], config_dir=sys.argv[2], local_install=sys.argv[3])\n"
        "assert run_migrate(ctx)\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "app"),
                             str(config), str(selected)], input="y\n", env=environment,
                            capture_output=True, text=True, cwd=ROOT,
                            start_new_session=True, timeout=5)

    assert result.returncode == 0, result.stderr
    migrated = tomllib.loads((config / "config.d/99-user.toml").read_text())
    assert migrated["serial"]["ports"] == ["/dev/ttyUSB7"]
    broker = migrated["broker"][0]
    assert broker["server"] == "mqtt.invalid"
    assert broker["enabled"] is True
    assert broker["transport"] == "websockets"
    assert broker["port"] == 443
    assert broker["tls"] == {"enabled": True, "verify": True}
    assert broker["auth"] == {"method": "token", "email": "owner@example.com"}


@pytest.mark.parametrize("filename", ["99-user.toml", "00-user.toml"])
def test_migration_refuses_existing_config(tmp_path, filename):
    config_d = tmp_path / "config.d"
    config_d.mkdir()
    original = config_d / filename
    original.write_text('value="keep"\n')
    with pytest.raises(FileExistsError):
        prepare_migrated_config(_legacy_settings(), str(tmp_path))
    assert original.read_text() == 'value="keep"\n'
    assert sorted(path.name for path in config_d.iterdir()) == [filename]


def test_bad_or_absent_migration_input_creates_no_config(tmp_path):
    assert prepare_migrated_config({}, str(tmp_path)) is None
    bad = _legacy_settings()
    bad["MCTOMQTT_MQTT1_PORT"] = "not-a-number"
    with pytest.raises(tomllib.TOMLDecodeError):
        prepare_migrated_config(bad, str(tmp_path))
    assert not (tmp_path / "config.d").exists()


def test_docker_recipe_always_comes_from_selected_sources(tmp_path):
    installed = tmp_path / "installed"
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    for directory in (installed, selected, other):
        directory.mkdir()
    (installed / "Dockerfile").write_text("FROM obsolete\n")
    (selected / "Dockerfile").write_text("FROM selected\n")
    (other / "Dockerfile").write_text("FROM not-selected\n")
    ctx = InstallerContext(install_dir=str(installed), local_install=str(selected), repo_dir=str(other))
    assert stage_dockerfile(ctx)
    assert (installed / "Dockerfile").read_text() == "FROM selected\n"
    (selected / "Dockerfile").unlink()
    assert not stage_dockerfile(ctx)
    assert (installed / "Dockerfile").read_text() == "FROM selected\n"
    assert not list(installed.glob(".Dockerfile.*"))


def test_private_docker_config_has_explicit_numeric_group_access(tmp_path):
    config_d = tmp_path / "config.d"
    config_d.mkdir()
    write_private_config(config_d / "99-user.toml", '[general]\niata="SEA"\n')
    os.chmod(tmp_path, 0o750)
    os.chmod(config_d, 0o750)
    command = docker_run_command(str(tmp_path), "mctomqtt:test")
    assert f"--group-add={tmp_path.stat().st_gid}" in command
    assert command[-2:] == [f"{tmp_path}:/etc/mctomqtt:ro", "mctomqtt:test"]
    assert docker_run_command(str(tmp_path), "test", config_gid=456).count("--group-add=456") == 1
    for invalid in (True, -1, "root"):
        with pytest.raises(ValueError):
            docker_run_command(str(tmp_path), "test", config_gid=invalid)


def test_custom_native_paths_and_config_directory_are_preserved():
    app = '/chosen app/percent%$"quote'
    config = '/private config/percent%"quote'
    unit = render_systemd_template((ROOT / "mctomqtt.service").read_text(), app, config, "chosen")
    assert "User=chosen\n" in unit and "Group=chosen\n" in unit
    assert "WorkingDirectory=/chosen app/percent%%$\"quote/\n" in unit
    assert 'Environment="MCTOMQTT_CONFIG_DIR=/private config/percent%%\\"quote"' in unit
    assert 'ExecStart=/usr/bin/env "/chosen app/percent%%$$\\"quote/venv/bin/python3"' in unit
    assert "--config" not in unit
    plist = plistlib.loads(render_launchd_plist(app, config, "chosen",
                          (ROOT / "com.meshcore.mctomqtt.plist").read_bytes()))
    assert plist["ProgramArguments"] == [app + "/venv/bin/python3", app + "/mctomqtt.py"]
    assert plist["WorkingDirectory"] == app
    assert plist["EnvironmentVariables"]["MCTOMQTT_CONFIG_DIR"] == config
    assert "PATH" in plist["EnvironmentVariables"]


def test_native_systemd_paths_parse_without_starting_service(tmp_path):
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd parser is not available")
    app = tmp_path / 'app spaces%$"slash\\'
    executable = app / "venv/bin/python3"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    unit = tmp_path / "installer-paths.service"
    unit.write_text(render_systemd_template((ROOT / "mctomqtt.service").read_text(),
                                           str(app), str(tmp_path / 'private config%"'), "chosen"))
    result = subprocess.run(["systemd-analyze", "verify", "--man=no", str(unit)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_replacement_validation_precedes_retiring_existing_services():
    with pytest.raises(ValueError, match="control"):
        render_systemd_template((ROOT / "mctomqtt.service").read_text(),
                                "/invalid\napp", "/private/config", "chosen")
    with pytest.raises(plistlib.InvalidFileException):
        render_launchd_plist("/chosen/app", "/private/config", "chosen", b"not a plist")
    systemd = inspect.getsource(install_systemd_service)
    assert '["systemctl", "stop", unit_file]' not in systemd
    assert systemd.index("render_systemd_template(") < systemd.index("unit_path.write_text(")
    assert systemd.index("unit_path.write_text(") < systemd.index('["systemctl", "restart", unit_file]')
    launchd = inspect.getsource(install_launchd_service)
    assert launchd.index("render_launchd_plist(") < launchd.index("write_bytes(content)")
    assert launchd.index("write_bytes(content)") < launchd.index('["launchctl", "unload", plist_dest]')


@pytest.mark.parametrize("empty", [False, True])
def test_installer_python_receives_directory_selectors(tmp_path, empty):
    environment = dict(os.environ, MCTOMQTT_INSTALL_DIR="" if empty else str(tmp_path / "app"),
                       MCTOMQTT_CONFIG_DIR="" if empty else str(tmp_path / "config"))
    check = '''
import argparse, ast, json, os, pathlib
from installer import InstallerContext
source = ast.parse(pathlib.Path("installer/__main__.py").read_text())
assignment = next(node for node in ast.walk(source) if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "ctx" for target in node.targets))
args = argparse.Namespace(repo="owner/repo", branch="chosen")
ctx = eval(compile(ast.Expression(assignment.value), "actual-context-constructor", "eval"))
print(json.dumps([ctx.install_dir, ctx.config_dir]))
'''
    result = subprocess.run([sys.executable, "-c", check], env=environment, cwd=ROOT,
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [environment["MCTOMQTT_INSTALL_DIR"] or "/opt/mctomqtt",
                                       environment["MCTOMQTT_CONFIG_DIR"] or "/etc/mctomqtt"]


@pytest.mark.parametrize("selector", ["MCTOMQTT_INSTALL_DIR", "MCTOMQTT_CONFIG_DIR"])
def test_relative_directory_selectors_are_rejected_before_privilege_or_download(selector):
    environment = dict(os.environ, **{selector: "relative/path"})
    for script in ("install.sh", "scripts/update.sh", "scripts/migrate.sh"):
        result = subprocess.run(["bash", str(ROOT / script)], env=environment,
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1
        assert f"{selector} must be an absolute path" in result.stderr
        assert "sudo" not in result.stdout and "Downloading" not in result.stdout
    result = subprocess.run([sys.executable, "-m", "installer", "install"],
                            env=environment, cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert f"{selector} must be an absolute path" in result.stderr


@pytest.mark.parametrize("override", ["stored", "environment", "cli"])
def test_update_bootstrap_reads_quoted_config_paths_and_selector_precedence(tmp_path, override):
    config = tmp_path / "config's directory"
    (config / "config.d").mkdir(parents=True)
    (config / "config.d/99-user.toml").write_text(
        '[update]\nrepo="owner/stored"\nbranch="stored/branch"\n')
    environment = dict(os.environ, MCTOMQTT_CONFIG_DIR=str(config))
    environment.pop("MCTOMQTT_REPO", None)
    environment.pop("MCTOMQTT_BRANCH", None)
    args = []
    expected = ["owner/stored", "stored/branch"]
    if override != "stored":
        environment.update(MCTOMQTT_REPO="owner/environment", MCTOMQTT_BRANCH="env/branch")
        expected = ["owner/environment", "env/branch"]
    if override == "cli":
        args = ["--repo", "owner/cli", "--branch", "cli/branch"]
        expected = ["owner/cli", "cli/branch"]
    source = (ROOT / "scripts/update.sh").read_text()
    prefix = source[:source.index("# Ensure running as root")]
    prefix += '\nprintf "%s\\0" "$REPO" "$BRANCH"\n'
    result = subprocess.run(["bash", "-s", "--", *args], input=prefix,
                            capture_output=True, text=True, env=environment, check=True)
    assert result.stdout.rstrip("\0").split("\0") == expected


def test_downloaded_config_is_validated_without_touching_active_config(tmp_path):
    if shutil.which("curl") is None:
        pytest.skip("curl is required by the production downloader")
    active = tmp_path / "99-user.toml"
    active.write_text('value="keep"\n')
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'[general]\niata="SEA"\n' if self.path == "/valid" else b'broken="\n'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert tomllib.loads(load_config_url(url + "/valid"))["general"]["iata"] == "SEA"
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config_url(url + "/invalid")
        assert active.read_text() == 'value="keep"\n'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()
