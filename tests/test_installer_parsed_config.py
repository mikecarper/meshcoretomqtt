"""TOML updates respect parsed tables rather than spelling or quoted content."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from config_loader import load_config
from installer.config import (
    _next_custom_broker_number,
    _read_existing_iata,
    _replace_owner_fields,
    _update_iata_in_file,
    _toml_dumps,
    append_custom_broker_toml,
    has_token_auth_brokers,
    token_auth_owner_defaults,
)


ROOT = Path(__file__).resolve().parents[1]


def test_custom_broker_number_never_reuses_an_existing_higher_number(tmp_path):
    drops = tmp_path / 'config.d'
    drops.mkdir()
    (drops / '99-user.toml').write_text('[[broker]]\nname="custom-2"\n')
    assert _next_custom_broker_number(str(tmp_path)) == 3


def test_new_custom_broker_preserves_existing_base_and_drop_in_connections(tmp_path):
    drops = tmp_path / 'config.d'
    drops.mkdir()
    base = tmp_path / 'config.toml'
    preset = drops / '10-community.toml'
    user = drops / '99-user.toml'
    base.write_text("[[ broker ]]\nname='custom-7'\nserver='base.invalid'\n")
    preset.write_text("broker=[{name='community',server='community.invalid'}]\n")
    user.write_text("[[broker]]\nname='custom-8'\nserver='existing.invalid'\n")
    number = _next_custom_broker_number(str(tmp_path))
    assert number == 9

    append_custom_broker_toml(str(user), f'custom-{number}', 'new.invalid', '1883',
                             'tcp', 'false', 'true', 'none')

    config = load_config([str(base), str(preset), str(user)])
    assert {broker['name']: broker['server'] for broker in config['broker']} == {
        'custom-7': 'base.invalid', 'community': 'community.invalid',
        'custom-8': 'existing.invalid', 'custom-9': 'new.invalid',
    }


def test_custom_broker_number_counts_tables_instead_of_comments_or_backup_files(tmp_path):
    drops = tmp_path / 'config.d'
    drops.mkdir()
    (drops / '99-user.toml').write_text(
        "# [[broker]] is a comment\n[[ broker ]]\nname='community'\n")
    (drops / '99-user.toml.backup').write_text("[[broker]]\nname='custom-100'\n")
    assert _next_custom_broker_number(str(tmp_path)) == 2


@pytest.mark.parametrize('method', ['method="token"', "method='token'", 'method = "token"'])
def test_token_auth_detection_accepts_valid_toml_spellings(method):
    content = f"[[ broker ]]\nname='signed'\n[broker.auth]\n{method}\n"
    assert has_token_auth_brokers(content)


def test_token_auth_detection_ignores_comments_and_unrelated_method_fields():
    content = ('# method = "token"\n[general]\nmethod="token"\n'
               '[[broker]]\nname="password"\n[broker.auth]\nmethod="password"\n')
    assert not has_token_auth_brokers(content)


def test_owner_update_adds_fields_only_to_explicit_token_auth_with_spaced_headers(tmp_path):
    content = (
        "[general]\nowner='GENERAL'\nemail='general@example.com'\n"
        "[[ broker ]]\nname='signed'\nowner='BROKER'\n[broker.auth]\nmethod='token'\n"
        "[broker.topics]\nowner='TOPIC'\n"
        "[[broker]]\nname='preset'\n[broker.auth]\nowner='PRESET'\nemail='preset@example.com'\n"
        "[[broker]]\nname='password'\n[broker.auth]\nmethod='password'\nowner='PASSWORD'\n"
    )
    path = tmp_path / '99-user.toml'
    path.write_text(_replace_owner_fields(content, 'NEW', 'new@example.com'))
    data = tomllib.loads(path.read_text())

    assert data['general'] == {'owner': 'GENERAL', 'email': 'general@example.com'}
    assert data['broker'][0]['owner'] == 'BROKER'
    assert data['broker'][0]['topics'] == {'owner': 'TOPIC'}
    assert data['broker'][0]['auth'] == {
        'method': 'token', 'owner': 'NEW', 'email': 'new@example.com'}
    assert data['broker'][1]['auth'] == {'owner': 'PRESET', 'email': 'preset@example.com'}
    assert data['broker'][2]['auth'] == {'method': 'password', 'owner': 'PASSWORD'}


def test_owner_defaults_only_use_explicit_token_auth_fields():
    content = (
        "[general]\nowner='GENERAL'\nemail='general@example.com'\n"
        "[[broker]]\nname='password'\n[broker.auth]\nmethod='password'\nowner='PASSWORD'\n"
        "[[broker]]\nname='signed'\n[broker.auth]\nmethod='token'\nowner='SIGNED'\n"
        "email='signed@example.com'\n"
    )
    assert token_auth_owner_defaults(content) == ('SIGNED', 'signed@example.com')


def test_conflicting_token_owner_values_do_not_become_shared_defaults():
    content = ("[[broker]]\nname='first'\n[broker.auth]\nmethod='token'\nowner='FIRST'\n"
               "[[broker]]\nname='second'\n[broker.auth]\nmethod='token'\nowner='SECOND'\n")
    assert token_auth_owner_defaults(content) == ('', '')


def test_real_interactive_owner_update_accepts_compact_token_and_adds_missing_fields(tmp_path):
    drops = tmp_path / 'config.d'
    drops.mkdir()
    user = drops / '99-user.toml'
    user.write_text("[[ broker ]]\nname='signed'\n[broker.auth]\nmethod='token'\n")
    owner = 'AB' * 32
    script = 'import sys; from installer.config import update_owner_info; update_owner_info(sys.argv[1])'
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            cwd=ROOT, input=f'{owner}\nnew@example.com\nn\n',
                            capture_output=True, text=True, start_new_session=True, timeout=5)

    assert result.returncode == 0, result.stdout + result.stderr
    assert tomllib.loads(user.read_text())['broker'][0]['auth'] == {
        'method': 'token', 'owner': owner, 'email': 'new@example.com'}


@pytest.mark.parametrize('content', [
    "[general]\n iata='SEA'\n", "[ general ]\niata=\"SEA\"\n",
    "general={iata='SEA'}\n",
])
def test_iata_reader_accepts_all_valid_toml_forms(tmp_path, content):
    path = tmp_path / '99-user.toml'
    path.write_text(content)
    assert _read_existing_iata(str(path)) == 'SEA'


def test_iata_reader_ignores_broker_local_iata(tmp_path):
    path = tmp_path / '99-user.toml'
    path.write_text("[[broker]]\nname='example'\n[broker.topics]\niata='LAX'\n")
    assert _read_existing_iata(str(path)) == ''


@pytest.mark.parametrize('with_general', [False, True])
def test_iata_update_creates_or_updates_general_and_preserves_broker_override(tmp_path, with_general):
    path = tmp_path / '99-user.toml'
    content = "[[broker]]\nname='example'\n[broker.topics]\niata='LOCAL'\n"
    if with_general:
        content = "[general]\n iata='SEA'\nsync_time=false\n" + content
    path.write_text(content)

    _update_iata_in_file(str(path), 'LAX')

    data = tomllib.loads(path.read_text())
    assert data['general']['iata'] == 'LAX'
    assert data['broker'][0]['topics']['iata'] == 'LOCAL'
    if with_general:
        assert data['general']['sync_time'] is False


def test_toml_date_time_values_round_trip_and_survive_owner_and_iata_edits(tmp_path):
    content = (
        '[metadata]\ncreated=2026-10-03T12:34:56.123456Z\n'
        'local_timestamp=2026-10-03T12:34:56\n'
        'date=2026-10-03\ntime=12:34:56.5\n'
        'dates=[2026-10-03,2026-10-04]\n'
        '[[broker]]\nname="signed"\n[broker.auth]\nmethod="token"\n'
    )
    expected = tomllib.loads(content)
    assert tomllib.loads(_toml_dumps(expected)) == expected
    path = tmp_path / '99-user.toml'
    path.write_text(_replace_owner_fields(content, 'NEW', 'new@example.com'))

    _update_iata_in_file(str(path), 'SEA')

    edited = tomllib.loads(path.read_text())
    assert edited['metadata'] == expected['metadata']
    assert edited['broker'][0]['auth'] == {
        'method': 'token', 'owner': 'NEW', 'email': 'new@example.com'}
    assert edited['general']['iata'] == 'SEA'
