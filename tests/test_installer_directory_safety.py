"""Protect selected configuration directories using real files and links."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from installer.config import (
    migrate_user_config_filename,
    copy_preset_to_config,
    import_preset_to_config,
    validate_config_directory,
    validate_install_directory,
    validate_install_layout,
    write_private_config,
)


@pytest.mark.parametrize('linked_directory', ['root', 'drop-ins'])
@pytest.mark.parametrize('dangling', [False, True])
def test_selected_config_links_are_rejected_without_touching_external_files(
        tmp_path, linked_directory, dangling):
    external = tmp_path / 'external'
    original = None
    if not dangling:
        external.mkdir()
        original = external / 'config.toml'
        original.write_text('value="preserve external contents"\n')
        original.chmod(0o600)
    root = tmp_path / 'selected-config'
    if linked_directory == 'root':
        root.symlink_to(external, target_is_directory=True)
    else:
        root.mkdir()
        (root / 'config.d').symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match='Configuration directories must not be symlinks'):
        validate_config_directory(root)

    if original is not None:
        assert original.read_text() == 'value="preserve external contents"\n'
        assert original.stat().st_mode & 0o777 == 0o600
        assert list(external.iterdir()) == [original]
    else:
        assert not external.exists()


@pytest.mark.parametrize('file_directory', ['root', 'drop-ins'])
def test_selected_config_paths_must_be_directories(tmp_path, file_directory):
    root = tmp_path / 'selected-config'
    if file_directory == 'drop-ins':
        root.mkdir()
        selected = root / 'config.d'
    else:
        selected = root
    selected.write_text('keep this regular file\n')

    with pytest.raises(ValueError, match='Configuration path must be a directory'):
        validate_config_directory(root)

    assert selected.read_text() == 'keep this regular file\n'


def test_new_config_directories_can_be_validated_before_creation(tmp_path):
    root = tmp_path / 'not-created-yet'
    validate_config_directory(root)
    assert not root.exists()


def test_alias_in_config_parent_remains_supported(tmp_path):
    physical_parent = tmp_path / 'physical-etc'
    physical_parent.mkdir()
    alias = tmp_path / 'etc-alias'
    alias.symlink_to(physical_parent, target_is_directory=True)
    root = alias / 'mctomqtt'
    (root / 'config.d').mkdir(parents=True)

    validate_config_directory(root)
    write_private_config(root / 'config.toml', 'value="saved through parent alias"\n')

    assert (physical_parent / 'mctomqtt/config.toml').read_text() == (
        'value="saved through parent alias"\n')
    assert alias.is_symlink()


@pytest.mark.parametrize('linked_directory', ['root', 'drop-ins'])
def test_legacy_user_config_rename_rejects_directory_links_before_mutation(
        tmp_path, linked_directory):
    external = tmp_path / 'external'
    external.mkdir()
    root = tmp_path / 'selected-config'
    if linked_directory == 'root':
        (external / 'config.d').mkdir()
        root.symlink_to(external, target_is_directory=True)
    else:
        root.mkdir()
        (root / 'config.d').symlink_to(external, target_is_directory=True)
    legacy = root / 'config.d/00-user.toml'
    legacy.write_text('value="keep legacy config"\n')

    with pytest.raises(ValueError, match='Configuration directories must not be symlinks'):
        migrate_user_config_filename(root)

    assert legacy.read_text() == 'value="keep legacy config"\n'
    assert not (root / 'config.d/99-user.toml').exists()


def test_standalone_private_write_rejects_linked_destination_parent(tmp_path):
    external = tmp_path / 'external'
    external.mkdir()
    config_file = external / 'config.toml'
    config_file.write_text('value="preserve before write"\n')
    config_file.chmod(0o600)
    parent_link = tmp_path / 'linked-parent'
    parent_link.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match='Configuration directories must not be symlinks'):
        write_private_config(parent_link / 'config.toml', 'value="replacement"\n')

    assert config_file.read_text() == 'value="preserve before write"\n'
    assert config_file.stat().st_mode & 0o777 == 0o600
    assert list(external.iterdir()) == [config_file]


def test_standalone_drop_in_write_rejects_linked_selected_root(tmp_path):
    external = tmp_path / 'external'
    (external / 'config.d').mkdir(parents=True)
    config_file = external / 'config.d/99-user.toml'
    config_file.write_text('value="preserve drop-in contents"\n')
    root_link = tmp_path / 'linked-root'
    root_link.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match='Configuration directories must not be symlinks'):
        write_private_config(root_link / 'config.d/99-user.toml', 'value="replacement"\n')

    assert config_file.read_text() == 'value="preserve drop-in contents"\n'
    assert list((external / 'config.d').iterdir()) == [config_file]


@pytest.mark.parametrize('importer', [copy_preset_to_config, import_preset_to_config])
@pytest.mark.parametrize('linked_directory', ['root', 'drop-ins'])
def test_preset_import_rejects_links_before_creating_or_writing_drop_ins(
        tmp_path, importer, linked_directory):
    external = tmp_path / 'external'
    external.mkdir()
    root = tmp_path / 'selected-config'
    if linked_directory == 'root':
        root.symlink_to(external, target_is_directory=True)
    else:
        root.mkdir()
        (root / 'config.d').symlink_to(external, target_is_directory=True)
    source = tmp_path / 'preset.toml'
    source.write_text('[[broker]]\nname="example"\nserver="mqtt.invalid"\n')

    with pytest.raises(ValueError, match='Configuration directories must not be symlinks'):
        importer(str(source), root)

    assert list(external.iterdir()) == []


@pytest.mark.parametrize('validator', [validate_config_directory, validate_install_directory])
@pytest.mark.parametrize('selected', ['root', 'dot-dot', 'linked-root'])
def test_config_and_install_directories_reject_resolved_filesystem_root(
        tmp_path, validator, selected):
    if selected == 'root':
        directory = Path('/')
    elif selected == 'dot-dot':
        directory = tmp_path.joinpath(*(['..'] * len(tmp_path.parts)))
    else:
        directory = tmp_path / 'linked-root'
        directory.symlink_to('/', target_is_directory=True)

    with pytest.raises(ValueError, match='must not be the filesystem root'):
        validator(directory)


def test_install_directory_preserves_ordinary_directory_links(tmp_path):
    physical = tmp_path / 'physical-app'
    physical.mkdir()
    selected = tmp_path / 'linked-app'
    selected.symlink_to(physical, target_is_directory=True)

    validate_install_directory(selected)

    assert selected.is_symlink()
    assert list(physical.iterdir()) == []


def test_install_directory_rejects_regular_files(tmp_path):
    selected = tmp_path / 'regular-file'
    selected.write_text('keep regular file contents\n')

    with pytest.raises(ValueError, match='Installation path must be a directory'):
        validate_install_directory(selected)

    assert selected.read_text() == 'keep regular file contents\n'


@pytest.mark.parametrize('replacement', ['bridge', 'venv'])
@pytest.mark.parametrize('nested', [False, True])
def test_persistent_config_cannot_use_replaced_installation_tree(tmp_path, replacement, nested):
    app = tmp_path / 'app'
    config = app / replacement
    if nested:
        config /= 'private'
    (config / 'config.d').mkdir(parents=True)
    secret = config / 'config.d/99-user.toml'
    secret.write_text('password="preserve private config"\n')
    secret.chmod(0o640)

    with pytest.raises(ValueError, match='inside replaceable installation directory'):
        validate_install_layout(app, config)

    assert secret.read_text() == 'password="preserve private config"\n'
    assert secret.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('selected_alias', ['install', 'config-parent'])
@pytest.mark.parametrize('replacement', ['bridge', 'venv'])
def test_replaced_config_tree_is_detected_through_parent_aliases(
        tmp_path, selected_alias, replacement):
    app = tmp_path / 'app'
    config = app / replacement / 'private'
    (config / 'config.d').mkdir(parents=True)
    alias = tmp_path / 'app-alias'
    alias.symlink_to(app, target_is_directory=True)
    install_dir = alias if selected_alias == 'install' else app
    config_dir = alias / replacement / 'private' if selected_alias == 'config-parent' else config

    with pytest.raises(ValueError, match='inside replaceable installation directory'):
        validate_install_layout(install_dir, config_dir)

    assert alias.is_symlink()
    assert list((config / 'config.d').iterdir()) == []


@pytest.mark.parametrize('location', ['app-root', 'config-child', 'bridge-sibling', 'external'])
def test_persistent_config_outside_replacement_trees_remains_supported(tmp_path, location):
    app = tmp_path / 'app'
    config = {'app-root': app, 'config-child': app / 'config',
              'bridge-sibling': app / 'bridge-config', 'external': tmp_path / 'config'}[location]

    validate_install_layout(app, config)

    assert not app.exists()
    assert not config.exists()


@pytest.mark.parametrize('replacement', ['bridge', 'venv'])
def test_external_source_update_rejects_config_layout_before_replacing_files(tmp_path, replacement):
    app = tmp_path / 'app'
    config = app / replacement / 'private'
    (config / 'config.d').mkdir(parents=True)
    (app / 'bridge').mkdir(exist_ok=True)
    (app / 'bridge/__init__.py').write_text('VERSION="old"\n')
    (app / 'mctomqtt.py').write_text('__version__="old"\n')
    (app / '.install_type').write_text('manual')
    (config / 'config.toml').write_text('[general]\niata="OLD"\n')
    (config / 'config.d/99-user.toml').write_text('password="private credential"\n')
    original = {str(path.relative_to(app)): path.read_bytes()
                for path in app.rglob('*') if path.is_file()}
    selected = tmp_path / 'selected'
    (selected / 'bridge').mkdir(parents=True)
    (selected / 'bridge/__init__.py').write_text('VERSION="new"\n')
    for name, content in {'mctomqtt.py': '__version__="new"\n', 'auth_token.py': '# auth\n',
                          'config_loader.py': '# loader\n', 'uninstall.sh': '#!/bin/sh\n',
                          'config.toml.example': '[general]\niata="NEW"\n'}.items():
        (selected / name).write_text(content)
    staging = tmp_path / 'staging'
    staging.mkdir()
    # A missing layout guard may install real files, but must never proceed
    # into dependency installation or external service/account operations.
    script = '''
import sys
from installer import InstallerContext
from installer.system import create_venv
from installer.update_cmd import _do_update
def trace(frame, event, arg):
    if event == "call" and frame.f_code is create_venv.__code__:
        raise AssertionError("layout was not rejected before dependency refresh")
    return trace
ctx = InstallerContext(install_dir=sys.argv[1], config_dir=sys.argv[2],
                       local_install=sys.argv[3], svc_user="", update_mode=True)
sys.settrace(trace)
try:
    _do_update(ctx, sys.argv[4])
except ValueError as error:
    sys.settrace(None)
    assert "inside replaceable installation directory" in str(error), error
    print("LAYOUT_REJECTED")
else:
    raise AssertionError("layout was accepted")
'''
    result = subprocess.run([sys.executable, '-c', script, str(app), str(config),
                             str(selected), str(staging)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=5)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'LAYOUT_REJECTED' in result.stdout
    assert {str(path.relative_to(app)): path.read_bytes()
            for path in app.rglob('*') if path.is_file()} == original
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize('command', ['install', 'update', 'migrate'])
@pytest.mark.parametrize('replacement', ['bridge', 'venv'])
def test_invalid_persistent_config_layout_is_rejected_before_privilege_check(
        tmp_path, command, replacement):
    app = tmp_path / 'app'
    config = app / replacement / 'private'
    script = '''
import sys
from installer.__main__ import main
from installer.system import require_root
def trace(frame, event, arg):
    if event == "call" and frame.f_code is require_root.__code__:
        raise AssertionError("privilege check ran before layout rejection")
    return trace
sys.settrace(trace)
sys.argv = ["installer", sys.argv[1]]
main()
'''
    environment = dict(os.environ, MCTOMQTT_INSTALL_DIR=str(app), MCTOMQTT_CONFIG_DIR=str(config))
    result = subprocess.run([sys.executable, '-c', script, command],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            capture_output=True, text=True, timeout=5)

    assert result.returncode == 2, result.stdout + result.stderr
    assert 'inside replaceable installation directory' in result.stderr
    assert 'privilege check ran' not in result.stderr
    assert not app.exists()
