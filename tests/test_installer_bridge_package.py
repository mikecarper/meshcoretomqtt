"""Install real package trees safely when local sources are already installed."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys

import pytest

from installer.system import install_bridge_package, validate_local_source


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


def test_package_snapshot_survives_staging_inside_replaced_destination(tmp_path):
    selected = tmp_path / 'selected'
    write_package(selected)
    destination = tmp_path / 'installed'
    (destination / 'bridge/staging').mkdir(parents=True)
    (destination / 'bridge/obsolete.py').write_text('obsolete module\n')
    staging = destination / 'bridge/staging'

    install_bridge_package(str(selected), str(destination), str(staging))

    assert package_contents(destination) == package_contents(selected)
    assert not (destination / 'bridge/obsolete.py').exists()
    assert not list(destination.glob('.bridge-source-*'))


@pytest.mark.parametrize('location', ['source', 'destination', 'venv',
                                     'source_alias', 'destination_alias', 'venv_alias'])
def test_installer_staging_selection_avoids_package_trees_without_copying(tmp_path, location):
    selected = tmp_path / 'selected'
    destination = tmp_path / 'installed'
    write_package(selected)
    write_package(destination)
    preferred = ((selected if location.startswith('source') else destination)
                 / ('venv' if location.startswith('venv') else 'bridge'))
    preferred.mkdir(exist_ok=True)
    if location.endswith('alias'):
        link = tmp_path / 'temporary-alias'
        link.symlink_to(preferred, target_is_directory=True)
        preferred = link
    script = '''
import shutil, sys
from pathlib import Path
from installer.system import create_installer_staging_dir
staging=Path(create_installer_staging_dir(sys.argv[1], sys.argv[2]))
try:
    assert staging.is_dir()
    assert not staging.resolve().is_relative_to((Path(sys.argv[1])/"bridge").resolve())
    assert not staging.resolve().is_relative_to((Path(sys.argv[1])/"venv").resolve())
    assert not staging.resolve().is_relative_to((Path(sys.argv[2])/"bridge").resolve())
    print("SAFE_STAGE")
finally:
    shutil.rmtree(staging)
'''
    environment = dict(os.environ, TMPDIR=str(preferred))
    result = subprocess.run([sys.executable, '-c', script, str(destination), str(selected)],
                            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=5)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'SAFE_STAGE' in result.stdout
    assert not list(selected.rglob('mctomqtt-*'))
    assert not list(destination.rglob('mctomqtt-*'))


@pytest.mark.parametrize('tree', ['bridge', 'venv'])
@pytest.mark.parametrize('alias', [False, True])
@pytest.mark.parametrize('entry', ['run_install', '_do_install', 'run_update', '_do_update'])
def test_install_entry_rejects_sources_inside_replaced_trees_before_mutation(tmp_path, tree, alias, entry):
    from installer import InstallerContext, install_cmd, update_cmd

    app = tmp_path / 'installed'
    source = app / tree / 'selected-source'
    source.mkdir(parents=True)
    (source / 'mctomqtt.py').write_text('__version__="source"\n')
    (app / 'mctomqtt.py').write_text('__version__="installed"\n')
    if alias:
        link = tmp_path / 'source-alias'
        link.symlink_to(source, target_is_directory=True)
        selected = link
    else:
        selected = source
    staging = tmp_path / 'staging'
    staging.mkdir()
    ctx = InstallerContext(install_dir=str(app), config_dir=str(tmp_path / 'config'),
                           local_install=str(selected), svc_user='')
    module = install_cmd if 'install' in entry else update_cmd
    operation = getattr(module, entry)

    with pytest.raises(ValueError, match='Local installation source'):
        operation(ctx, str(staging)) if entry.startswith('_') else operation(ctx)

    assert (source / 'mctomqtt.py').read_text() == '__version__="source"\n'
    assert (app / 'mctomqtt.py').read_text() == '__version__="installed"\n'
    assert list(staging.iterdir()) == []
    assert not (tmp_path / 'config').exists()


@pytest.mark.parametrize('tree', ['bridge', 'venv'])
def test_cli_rejects_replaceable_local_source_before_privilege_check(tmp_path, tree):
    app = tmp_path / 'installed'
    source = app / tree
    source.mkdir(parents=True)
    (source / 'mctomqtt.py').write_text('__version__="source"\n')
    environment = dict(os.environ, MCTOMQTT_INSTALL_DIR=str(app),
                       MCTOMQTT_CONFIG_DIR=str(tmp_path / 'config'), LOCAL_INSTALL=str(source))

    result = subprocess.run([sys.executable, '-m', 'installer', 'update'], cwd=ROOT,
                            env=environment, capture_output=True, text=True, timeout=5)

    assert result.returncode == 2
    assert 'Local installation source' in result.stderr
    assert 'must be run as root' not in result.stdout
    assert (source / 'mctomqtt.py').read_text() == '__version__="source"\n'


def test_local_source_validator_allows_installed_root_and_adjacent_sources(tmp_path):
    app = tmp_path / 'installed'
    app.mkdir()
    (app / 'bridge').mkdir()
    (app / 'venv').mkdir()
    alias = tmp_path / 'installed-alias'
    alias.symlink_to(app, target_is_directory=True)
    for source in (app, alias, tmp_path / 'selected', app / 'bridge-copy', app / 'venv-source'):
        validate_local_source(str(app), str(source))


def test_real_update_stages_all_assets_outside_tmpdir_in_destination_package(tmp_path):
    selected = tmp_path / 'selected'
    write_package(selected)
    assets = {
        'mctomqtt.py': '__version__="new-test"\n',
        'auth_token.py': '# selected auth\n', 'config_loader.py': '# selected loader\n',
        'config.toml.example': '[general]\niata="XXX"\n',
        'uninstall.sh': '#!/bin/sh\n# selected uninstall\n',
    }
    for name, content in assets.items():
        (selected / name).write_text(content)
    app = tmp_path / 'app'
    write_package(app)
    (app / 'bridge/__init__.py').write_text('VERSION = "old"\n')
    (app / 'mctomqtt.py').write_text('__version__="old-test"\n')
    (app / '.install_type').write_text('manual')
    config = tmp_path / 'config'
    (config / 'config.d').mkdir(parents=True)
    script = '''
import sys
from installer import InstallerContext
from installer.system import create_venv
from installer.update_cmd import run_update
class FilesInstalled(Exception): pass
def trace(frame, event, arg):
    if event == "call" and frame.f_code is create_venv.__code__:
        raise FilesInstalled
    return trace
ctx=InstallerContext(install_dir=sys.argv[1], config_dir=sys.argv[2],
                     local_install=sys.argv[3], svc_user="", update_mode=True)
sys.settrace(trace)
try:
    run_update(ctx)
except FilesInstalled:
    sys.settrace(None)
    print("FILES_INSTALLED")
else:
    raise AssertionError("did not reach dependency refresh")
'''
    environment = dict(os.environ, TMPDIR=str(app / 'bridge'))
    result = subprocess.run([sys.executable, '-c', script, str(app), str(config), str(selected)],
                            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=5)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FILES_INSTALLED' in result.stdout
    assert package_contents(app) == package_contents(selected)
    for name in ('mctomqtt.py', 'auth_token.py', 'config_loader.py', 'uninstall.sh'):
        assert (app / name).read_text() == assets[name]
    assert not list(app.rglob('.bridge-source-*'))
    assert not list(app.glob('mctomqtt-*'))


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
