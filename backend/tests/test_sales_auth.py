"""Test the OAuth resource server using real JWT signatures and a local signing key."""

import asyncio
from threading import get_ident
from time import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.exceptions import PyJWKClientConnectionError

from app.sales_auth import SalesTokenVerifier


ISSUER = "https://auth.example.com"
RESOURCE = "https://bianca.example.com/mcp"
JWKS_URL = f"{ISSUER}/oauth2/jwks"
FOUNDER = "user_founder"


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(monkeypatch, private_key):
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk["kid"] = "current-key"
    caller_thread = get_ident()

    def fetch_keys():
        assert get_ident() != caller_thread
        return {"keys": [public_jwk]}

    token_verifier = SalesTokenVerifier(
        issuer=ISSUER,
        audience=RESOURCE,
        jwks_url=JWKS_URL,
        allowed_subjects={FOUNDER},
    )
    monkeypatch.setattr(token_verifier._jwks, "fetch_data", fetch_keys)
    return token_verifier


def token_claims():
    return {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": FOUNDER,
        "exp": int(time()) + 300,
        "client_id": "claude-client",
        "scope": "openid email",
    }


def signed_token(claims, private_key):
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "current-key"})


@pytest.mark.parametrize("audience", [RESOURCE, [RESOURCE, "https://another.example.com"]])
def test_accepts_signed_token_for_founder_and_preserves_identity(verifier, private_key, audience):
    claims = token_claims()
    claims["aud"] = audience
    result = asyncio.run(verifier.verify_token(signed_token(claims, private_key)))

    assert result is not None
    assert result.subject == FOUNDER
    assert result.client_id == "claude-client"
    assert result.resource == RESOURCE
    assert result.expires_at == claims["exp"]
    assert result.scopes == ["openid", "email"]
    assert result.claims == {"iss": ISSUER}


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("aud", f"{RESOURCE}/"),
        ("aud", ["https://another.example.com"]),
        ("iss", "https://auth.example.com/other"),
        ("sub", "user_nonfounder"),
        ("sub", [FOUNDER]),
        ("exp", 1),
        ("exp", "9999999999"),
        ("exp", [9999999999]),
        ("client_id", ""),
    ],
)
def test_rejects_invalid_claims(verifier, private_key, claim, value):
    claims = token_claims()
    claims[claim] = value
    assert asyncio.run(verifier.verify_token(signed_token(claims, private_key))) is None


@pytest.mark.parametrize("claim", ["iss", "aud", "exp", "sub", "client_id"])
def test_rejects_missing_required_claim(verifier, private_key, claim):
    claims = token_claims()
    del claims[claim]
    assert asyncio.run(verifier.verify_token(signed_token(claims, private_key))) is None


def test_rejects_wrong_signature_and_symmetric_tokens(verifier):
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_signature = signed_token(token_claims(), wrong_key)
    symmetric = jwt.encode(
        token_claims(), "s" * 32, algorithm="HS256", headers={"kid": "current-key"}
    )

    assert asyncio.run(verifier.verify_token(wrong_signature)) is None
    assert asyncio.run(verifier.verify_token(symmetric)) is None


def test_rejects_unknown_signing_key_id(verifier, private_key):
    token = jwt.encode(token_claims(), private_key, algorithm="RS256", headers={"kid": "other-key"})
    assert asyncio.run(verifier.verify_token(token)) is None


def test_fails_closed_on_jwks_timeout(verifier, private_key, monkeypatch):
    def unavailable():
        raise PyJWKClientConnectionError("timeout")

    monkeypatch.setattr(verifier._jwks, "fetch_data", unavailable)
    assert asyncio.run(verifier.verify_token(signed_token(token_claims(), private_key))) is None
