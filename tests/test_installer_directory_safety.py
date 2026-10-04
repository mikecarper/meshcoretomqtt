"""Protect selected configuration directories using real files and links."""
from __future__ import annotations

from pathlib import Path

import pytest

from installer.config import (
    migrate_user_config_filename,
    copy_preset_to_config,
    import_preset_to_config,
    validate_config_directory,
    validate_install_directory,
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
