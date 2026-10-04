"""Exercise standalone token generation as a real process with actual keys."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from auth_token import verify_auth_token
from tests import test_auth_token as token_fixtures


SCRIPT = Path(__file__).resolve().parents[1] / 'auth_token.py'
PUBLIC_KEY = token_fixtures.TestAuthToken.public_key
PRIVATE_KEY = token_fixtures.TestAuthToken.private_key


def run_cli(private_input, cwd):
    return subprocess.run([sys.executable, str(SCRIPT), PUBLIC_KEY, private_input],
                          cwd=cwd, capture_output=True, text=True, timeout=10)


def assert_generated_token(result):
    assert result.returncode == 0, result.stdout + result.stderr
    token_lines = [line for line in result.stdout.splitlines()
                   if line.startswith('Generated token: ')]
    assert len(token_lines) == 1
    token = token_lines[0].removeprefix('Generated token: ')
    assert verify_auth_token(token, PUBLIC_KEY)['publicKey'] == PUBLIC_KEY.upper()


@pytest.mark.parametrize('relative', [False, True])
def test_cli_reads_private_key_from_long_file_path(tmp_path, relative):
    path = tmp_path / ('private-' + 'k' * 160 + '.key')
    path.write_text(PRIVATE_KEY + '\n')
    private_input = path.name if relative else str(path)
    assert len(private_input) >= 128

    result = run_cli(private_input, tmp_path)

    assert_generated_token(result)
    assert f'Loaded private key from: {private_input}' in result.stdout


def test_cli_reads_short_relative_key_file(tmp_path):
    path = tmp_path / 'private.key'
    path.write_text(PRIVATE_KEY)
    result = run_cli(path.name, tmp_path)
    assert_generated_token(result)
    assert 'Loaded private key from: private.key' in result.stdout


@pytest.mark.parametrize('separator', ['', ' ', '\n', '\t'])
def test_cli_normalizes_inline_hexadecimal_whitespace(tmp_path, separator):
    private_input = separator.join(PRIVATE_KEY)
    result = run_cli(private_input, tmp_path)
    assert_generated_token(result)
    assert 'Loaded private key from:' not in result.stdout


def test_invalid_inline_key_reports_error_without_echoing_key(tmp_path):
    invalid_key = 'g' + PRIVATE_KEY[1:]
    result = run_cli(invalid_key, tmp_path)
    assert result.returncode == 1
    assert 'Failed to generate auth token' in result.stdout
    assert invalid_key not in result.stdout + result.stderr


@pytest.mark.parametrize('private_input', [PRIVATE_KEY[:-2], 'g' + PRIVATE_KEY[1:-2]],
                         ids=['truncated-hex', 'malformed-truncated'])
def test_truncated_inline_key_reports_error_without_echoing_key(tmp_path, private_input):
    assert len(private_input) == 126
    result = run_cli(private_input, tmp_path)
    assert result.returncode == 1
    assert 'Error:' in result.stdout
    assert private_input not in result.stdout + result.stderr
