"""Shared fixtures for ``tests/test_github_app``.

Generates a fresh RSA key pair once per pytest session so we can exercise
the real PyJWT RS256 signer without committing a private key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="session")
def rsa_keypair_pem() -> Iterator[tuple[str, str]]:
    """Return ``(private_pem, public_pem)`` for a session-scoped RSA key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    yield private_pem, public_pem


@pytest.fixture(scope="session")
def rsa_private_pem(rsa_keypair_pem: tuple[str, str]) -> str:
    return rsa_keypair_pem[0]


@pytest.fixture(scope="session")
def rsa_public_pem(rsa_keypair_pem: tuple[str, str]) -> str:
    return rsa_keypair_pem[1]
