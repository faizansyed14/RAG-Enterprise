import uuid

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import create_token, hash_password, verify_password


def test_password_hash_roundtrip():
    hashed = hash_password("correct horse battery")
    assert hashed != "correct horse battery"
    assert verify_password(hashed, "correct horse battery") is True


def test_password_wrong_rejected():
    assert verify_password(hash_password("correct horse battery"), "wrong") is False


def test_password_garbage_hash_rejected():
    assert verify_password("not-a-hash", "anything") is False


def test_create_token_roundtrip():
    user_id = uuid.uuid4()
    token = create_token(user_id, "user", 3)
    payload = jwt.decode(token, get_settings().auth_secret, algorithms=["HS256"])
    assert payload["sub"] == str(user_id)
    assert payload["role"] == "user"
    assert payload["tv"] == 3


def test_create_token_expired_raises_on_decode():
    token = create_token(uuid.uuid4(), "user", 0, ttl_seconds=-10)
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, get_settings().auth_secret, algorithms=["HS256"])
