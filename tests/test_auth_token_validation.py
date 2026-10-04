"""Auth regressions exercising the native signer and verifier without mocks."""
from __future__ import annotations

import json
import time

import pytest
from ed25519_orlp import ed25519_sign

from auth_token import (
    _check_token_expiration,
    base64url_encode,
    create_auth_token,
    decode_token_payload,
    read_private_key_file,
    verify_auth_token,
)
from tests import test_auth_token as token_fixtures


PUBLIC_KEY = token_fixtures.TestAuthToken.public_key
PRIVATE_KEY = token_fixtures.TestAuthToken.private_key


def sign_payload(payload):
    header = base64url_encode(b'{"alg":"Ed25519","typ":"JWT"}')
    body = base64url_encode(json.dumps(payload, separators=(',', ':')).encode())
    message = f'{header}.{body}'
    signature = ed25519_sign(message.encode(), bytes.fromhex(PUBLIC_KEY),
                             bytes.fromhex(PRIVATE_KEY)).hex()
    return f'{message}.{signature}'


@pytest.mark.parametrize('extra', ['00', 'ff' * 64])
def test_valid_signature_with_appended_bytes_is_rejected(extra):
    token = create_auth_token(PUBLIC_KEY, PRIVATE_KEY)
    with pytest.raises(Exception, match='Invalid signature length'):
        verify_auth_token(token + extra, PUBLIC_KEY)


@pytest.mark.parametrize('signature', ['', '00', '00' * 63])
def test_short_signature_is_rejected_before_native_verification(signature):
    token = create_auth_token(PUBLIC_KEY, PRIVATE_KEY)
    with pytest.raises(Exception, match='Invalid signature length'):
        verify_auth_token(token.rsplit('.', 1)[0] + '.' + signature, PUBLIC_KEY)


@pytest.mark.parametrize('key', ['00', '00' * 31, PUBLIC_KEY + '00'])
def test_wrong_sized_public_key_is_rejected_before_native_verification(key):
    # Sign the complete malformed payload with a valid keypair; the C verifier
    # would otherwise ignore trailing public-key bytes.
    token = sign_payload({'publicKey': key, 'exp': time.time() + 60})
    with pytest.raises(Exception, match='Invalid public key length'):
        verify_auth_token(token)


def test_empty_expected_key_does_not_disable_identity_check():
    token = create_auth_token(PUBLIC_KEY, PRIVATE_KEY)
    with pytest.raises(Exception, match='does not match expected public key'):
        verify_auth_token(token, '')


@pytest.mark.parametrize('expires', [float('nan'), float('inf'), float('-inf'),
                                     True, False, None, 'future', []])
def test_signed_non_numeric_or_nonfinite_expiry_is_rejected(expires):
    token = sign_payload({'publicKey': PUBLIC_KEY, 'exp': expires})
    with pytest.raises(Exception, match='Invalid token expiry'):
        verify_auth_token(token, PUBLIC_KEY)


@pytest.mark.parametrize('expires', [float('nan'), float('inf'), float('-inf'),
                                     True, None, 'future'])
def test_creator_does_not_emit_an_invalid_expiry(expires):
    with pytest.raises(Exception, match='Invalid token expiry'):
        create_auth_token(PUBLIC_KEY, PRIVATE_KEY, exp=expires)


@pytest.mark.parametrize('lifetime', [float('nan'), float('inf'), float('-inf')])
def test_creator_rejects_nonfinite_lifetimes(lifetime):
    with pytest.raises(Exception, match='Invalid token expiry'):
        create_auth_token(PUBLIC_KEY, PRIVATE_KEY, expiry_seconds=lifetime)


@pytest.mark.parametrize('now', [1000, 1000.001])
def test_expiry_boundary_and_fractional_expiration_are_rejected(now):
    with pytest.raises(ValueError, match='Token has expired'):
        _check_token_expiration({'exp': 1000}, now)


def test_future_fractional_expiry_and_optional_expiry_remain_supported():
    _check_token_expiration({'exp': 1000.25}, 1000.125)
    _check_token_expiration({}, 1000)
    token = sign_payload({'publicKey': PUBLIC_KEY, 'exp': time.time() + 60.5})
    assert verify_auth_token(token, PUBLIC_KEY)['exp'] > time.time()
    assert verify_auth_token(sign_payload({'publicKey': PUBLIC_KEY}), PUBLIC_KEY) == {
        'publicKey': PUBLIC_KEY}


@pytest.mark.parametrize('payload', [[], None, True, 'text'])
def test_payload_decoder_requires_an_object(payload):
    with pytest.raises(Exception, match='Token payload must be an object'):
        decode_token_payload(sign_payload(payload))


def test_generated_public_key_is_canonical_after_hex_whitespace():
    spaced_key = ' '.join(PUBLIC_KEY[index:index + 2] for index in range(0, 64, 2))
    token = create_auth_token(spaced_key, PRIVATE_KEY)
    assert decode_token_payload(token)['publicKey'] == PUBLIC_KEY.upper()
    assert verify_auth_token(token, spaced_key)['publicKey'] == PUBLIC_KEY.upper()


@pytest.mark.parametrize('key', ['+' + '0' * 127, '-' + '0' * 127,
                               '0x' + '0' * 126, '0X' + '0' * 126,
                               'g' + '0' * 127])
def test_private_key_file_rejects_non_hexadecimal_digits(tmp_path, key):
    path = tmp_path / 'private.key'
    path.write_text(key)
    with pytest.raises(Exception, match='only hexadecimal digits'):
        read_private_key_file(str(path))


def test_private_key_file_accepts_hexadecimal_whitespace(tmp_path):
    path = tmp_path / 'private.key'
    path.write_text('\n'.join(PRIVATE_KEY[index:index + 32]
                             for index in range(0, 128, 32)) + '\n')
    assert read_private_key_file(str(path)) == PRIVATE_KEY
