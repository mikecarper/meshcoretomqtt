"""Offline source contracts for optional Nix serial candidates, not evaluation."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def test_nix_service_does_not_require_candidate_devices_or_mount_their_paths():
    module = (ROOT / "nix/nixos-module.nix").read_text()
    service = module.split("systemd.services.mctomqtt = {", 1)[1]
    assert "cfg.serialPorts" not in service
    assert ".device" not in service
    assert "requires =" not in service
    assert 'wants = ["network-online.target"];' in service
    assert 'after = ["network-online.target"];' in service

    writable = re.search(r"ReadWritePaths\s*=\s*\[(.*?)\]\s*;", service, re.S)
    assert writable is not None
    assert re.findall(r'"([^"]+)"', writable.group(1)) == [
        "/var/lib/mctomqtt", "/var/cache/mctomqtt", "/var/log/mctomqtt",
    ]


def test_nix_application_config_still_receives_all_serial_candidates():
    module = (ROOT / "nix/nixos-module.nix").read_text()
    serial_config = re.search(r"serial\s*=\s*\{(.*?)\};", module, re.S)
    assert serial_config is not None
    assert "ports = cfg.serialPorts;" in serial_config.group(1)
    assert "Optional serial connection candidates" in module
    assert 'extraGroups = ["dialout"];' in module


def test_nix_vm_fixture_covers_missing_by_id_candidate_without_mandatory_paths():
    fixture = (ROOT / "nix/nixos-test.nix").read_text()
    assert 'serialPorts = ["/dev/serial/by-id/usb-Missing-if00" "/dev/ttyS1"];' in fixture
    assert 'test ! -e /dev/serial/by-id/usb-Missing-if00' in fixture
    assert 'config["serial"]["ports"] == ["/dev/serial/by-id/usb-Missing-if00", "/dev/ttyS1"]' in fixture
    assert "device_unit not in requires" in fixture
    assert "device_unit not in after" in fixture
    assert "port not in writable" in fixture
    assert 'path.startswith("/dev/")' in fixture

    script = re.search(r"testScript = ''\n(.*?)\n      '';", fixture, re.S)
    assert script is not None
    ast.parse(textwrap.dedent(script.group(1)))
