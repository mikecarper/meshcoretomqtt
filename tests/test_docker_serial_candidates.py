"""Layered serial selection and deployment tests with real files and a fake CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from installer.system import docker_serial_device_args


ROOT = Path(__file__).resolve().parents[1]


def _config_with_candidates(tmp_path):
    config = tmp_path / "config"
    config_d = config / "config.d"
    config_d.mkdir(parents=True)
    missing = tmp_path / "missing-port"
    present = tmp_path / "present port"
    extra = tmp_path / "another-port"
    present.touch()
    extra.touch()
    ports = [str(missing), str(present), str(extra), str(present)]
    (config / "config.toml").write_text('[serial]\nports=["/dev/base-only"]\n')
    (config_d / "10-settings.toml").write_text('[serial]\nports=["/dev/preset-only"]\n')
    (config_d / "99-user.toml").write_text(
        "[serial]\nports = [\n" + "".join(f"  '{port}',\n" for port in ports) + "]\n"
    )
    return config, ports


def test_docker_maps_existing_fallbacks_from_layered_multiline_toml(tmp_path, capsys):
    config, ports = _config_with_candidates(tmp_path)
    assert docker_serial_device_args(str(config)) == [
        f"--device={ports[1]}", f"--device={ports[2]}",
    ]
    assert ports[0] in capsys.readouterr().out


def test_docker_uses_base_candidates_when_dropins_do_not_override_them(tmp_path):
    device = tmp_path / "serial-port"
    device.touch()
    config_d = tmp_path / "config.d"
    config_d.mkdir()
    (tmp_path / "config.toml").write_text(f"[serial]\nports=['{device}']\n")
    (config_d / "99-user.toml").write_text('[serial]\nbaud_rate=9600\n')
    assert docker_serial_device_args(str(tmp_path)) == [f"--device={device}"]
    (config_d / "99-user.toml").write_text('[serial]\nports=[]\n')
    assert docker_serial_device_args(str(tmp_path)) == []


def test_docker_default_candidate_is_used_without_explicit_ports(tmp_path, capsys):
    arguments = docker_serial_device_args(str(tmp_path))
    default = Path("/dev/ttyACM0")
    assert arguments == ([f"--device={default}"] if default.exists() else [])
    if not default.exists():
        assert str(default) in capsys.readouterr().out


def _fake_docker_environment(tmp_path, container_present=True):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "docker-calls.jsonl"
    docker = binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "with Path(os.environ['TEST_DOCKER_CALLS']).open('a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['ps']:\n"
        "    print('mctomqtt' if os.environ.get('TEST_CONTAINER_PRESENT') == '1' else 'mctomqtt-old')\n"
        "if sys.argv[1:2] == ['inspect']:\n"
        "    if os.environ.get('TEST_CONTAINER_PRESENT') != '1': sys.exit(1)\n"
        "    print('/mctomqtt true' if '{{.State.Running}}' in ' '.join(sys.argv) else '/mctomqtt')\n"
        "if sys.argv[1:2] == ['logs']: print('connected to mqtt.invalid')\n"
        "if sys.argv[1:2] == ['--version']: print('Docker test CLI')\n"
    )
    docker.chmod(0o755)
    return dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"],
                TEST_DOCKER_CALLS=str(calls), TEST_CONTAINER_PRESENT=str(int(container_present))), calls


def test_markerless_detection_ignores_unrelated_docker_container_substrings(tmp_path):
    if Path("/etc/systemd/system/mctomqtt.service").exists():
        pytest.skip("a real systemd unit determines the installation type")
    environment, calls = _fake_docker_environment(tmp_path, container_present=False)
    environment["PATH"] = str(tmp_path / "bin")
    app = tmp_path / "app"
    app.mkdir()
    result = subprocess.run([
        sys.executable, "-c", "import platform, sys; "
        "from installer.system import detect_system_type; "
        "expected = 'launchd' if platform.system() == 'Darwin' else 'unknown'; "
        "assert detect_system_type(sys.argv[1]) == expected", str(app),
    ], env=environment, cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not any(json.loads(line)[0] == "ps" for line in calls.read_text().splitlines())


def test_docker_health_ignores_unrelated_running_container_substrings(tmp_path):
    environment, calls = _fake_docker_environment(tmp_path, container_present=False)
    result = subprocess.run([
        sys.executable, "-c", "from installer.system import check_service_health; "
        "check_service_health('docker')",
    ], env=environment, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "Container started" not in result.stdout
    assert not any(json.loads(line)[0] == "ps" for line in calls.read_text().splitlines())


@pytest.mark.parametrize("operation", ["install", "update"])
def test_docker_install_and_update_pass_all_present_candidates_to_cli(tmp_path, operation):
    config, ports = _config_with_candidates(tmp_path)
    environment, calls = _fake_docker_environment(tmp_path)
    if operation == "install":
        script = (
            "import sys\nfrom installer import InstallerContext\n"
            "from installer.system import install_docker_service\n"
            "assert install_docker_service(InstallerContext(config_dir=sys.argv[1]))\n"
        )
    else:
        script = (
            "import sys\nfrom installer.update_cmd import _restart_docker_container\n"
            "_restart_docker_container(sys.argv[1], 'mctomqtt:test')\n"
        )
    result = subprocess.run([sys.executable, "-c", script, str(config)],
                            input="y\n", env=environment, capture_output=True, text=True,
                            start_new_session=True, cwd=ROOT, timeout=15)
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    run_command = next(command for command in commands if command[0] == "run")
    assert [part for part in run_command if part.startswith("--device=")] == [
        f"--device={ports[1]}", f"--device={ports[2]}",
    ]
    assert run_command[-1].startswith("mctomqtt:")


@pytest.mark.parametrize("content", ["broken='\n", "[serial]\nports=42\n"])
def test_update_rejects_invalid_serial_config_before_stopping_container(tmp_path, content):
    (tmp_path / "config.toml").write_text(content)
    environment, calls = _fake_docker_environment(tmp_path)
    result = subprocess.run([
        sys.executable, "-c",
        "from installer.update_cmd import _restart_docker_container; "
        "import sys; _restart_docker_container(sys.argv[1], 'mctomqtt:test')", str(tmp_path),
    ], env=environment, capture_output=True, text=True, cwd=ROOT, timeout=5)
    assert result.returncode != 0
    assert not calls.exists()
