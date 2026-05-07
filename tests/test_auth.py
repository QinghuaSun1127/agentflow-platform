import pytest

from app.security.auth import (
    AuthError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", password_hash)
    assert not verify_password("wrong password", password_hash)


def test_access_token_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    token = create_access_token({"sub": "1", "username": "demo"}, expires_seconds=60)
    claims = decode_access_token(token)

    assert claims["sub"] == "1"
    assert claims["username"] == "demo"


def test_access_token_rejects_tampering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = create_access_token({"sub": "1"}, expires_seconds=60)
    tampered = f"{token[:-1]}x"

    with pytest.raises(AuthError):
        decode_access_token(tampered)


def test_refresh_token_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = create_refresh_token({"sub": "1"}, expires_seconds=60)
    claims = decode_refresh_token(token)
    assert claims["sub"] == "1"
