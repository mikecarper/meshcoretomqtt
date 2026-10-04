"""Install real package trees safely when local sources are already installed."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from installer.system import install_bridge_package


ROOT = Path(__file__).resolve().parents[1]


def write_package(root):
    (root / 'bridge/nested').mkdir(parents=True)
    (root / 'bridge/__init__.py').write_text('VERSION = "selected"\n')
    (root / 'bridge/nested/module.py').write_text('VALUE = 42\n')


def package_contents(root):
    return {str(path.relative_to(root / 'bridge')): path.read_bytes()
            for path in (root / 'bridge').rglob('*') if path.is_file()}


@pytest.mark.parametrize('alias', ['none', 'source', 'destination'])
def test_package_install_preserves_in_place_sources_and_aliases(tmp_path, alias):
    app = tmp_path / 'app'
    write_package(app)
    original = package_contents(app)
    staging = tmp_path / 'staging'
    staging.mkdir()
    selected = app
    destination = app
    if alias != 'none':
        link = tmp_path / 'alias'
        link.symlink_to(app, target_is_directory=True)
        if alias == 'source':
            selected = link
        else:
            destination = link

    install_bridge_package(str(selected), str(destination), str(staging))

    assert package_contents(app) == original
    assert list(staging.iterdir()) == []
    if alias != 'none':
        assert link.is_symlink()


def test_package_install_replaces_old_destination_with_selected_source(tmp_path):
    selected = tmp_path / 'selected'
    write_package(selected)
    destination = tmp_path / 'installed'
    (destination / 'bridge').mkdir(parents=True)
    (destination / 'bridge/obsolete.py').write_text('obsolete module\n')
    staging = tmp_path / 'staging'
    staging.mkdir()

    install_bridge_package(str(selected), str(destination), str(staging))

    assert package_contents(destination) == package_contents(selected)
    assert not (destination / 'bridge/obsolete.py').exists()
    assert list(staging.iterdir()) == []


def test_real_in_place_update_preserves_package_before_dependency_refresh(tmp_path):
    app = tmp_path / 'app'
    write_package(app)
    original = package_contents(app)
    (app / '.install_type').write_text('manual')
    for name, content in {
        'mctomqtt.py': '__version__="test"\n',
        'auth_token.py': '# auth\n', 'config_loader.py': '# loader\n',
        'config.toml.example': '[general]\niata="XXX"\n',
        'uninstall.sh': '#!/bin/sh\n',
    }.items():
        (app / name).write_text(content)
    config = tmp_path / 'config'
    (config / 'config.d').mkdir(parents=True)
    staging = tmp_path / 'staging'
    staging.mkdir()
    # Stop before dependency creation; all source staging and package file
    # replacement run normally, including the real Python syntax check.
    script = '''
import sys
from installer import InstallerContext
from installer.system import create_venv
from installer.update_cmd import _do_update
class FilesInstalled(Exception): pass
def trace(frame, event, arg):
    if event == "call" and frame.f_code is create_venv.__code__:
        raise FilesInstalled
    return trace
ctx=InstallerContext(install_dir=sys.argv[1], config_dir=sys.argv[2],
                     local_install=sys.argv[1], svc_user="", update_mode=True)
sys.settrace(trace)
try:
    _do_update(ctx, sys.argv[3])
except FilesInstalled:
    sys.settrace(None)
    print("FILES_INSTALLED")
else:
    raise AssertionError("did not reach dependency refresh")
'''
    result = subprocess.run([sys.executable, '-c', script, str(app), str(config), str(staging)],
                            cwd=ROOT, capture_output=True, text=True, timeout=5)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FILES_INSTALLED' in result.stdout
    assert package_contents(app) == original
    assert not list(staging.glob('.bridge-source-*'))
